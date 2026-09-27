#!/usr/bin/env python3
"""Recompute the GMX Arbitrum position-coverage and cold-start audit.

All wallet addresses remain in memory for the trade-feed join. The script
prints aggregate counts and ratios only; it does not write raw rows to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal, localcontext
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = "https://gmx.squids.live/gmx-synthetics-arbitrum:prod/api/graphql"
POSITION_FIELDS = "id account market isLong sizeInUsd openedAt snapshotTimestamp"
OI_FIELDS = (
    "marketAddress snapshotTimestamp blockTimestamp blockNumber "
    "longOpenInterestUsd shortOpenInterestUsd"
)
EVENT_FIELDS = "id account timestamp"
PAGE_DELAY_SECONDS = 0.25
REQUEST_LOG: list[dict[str, Any]] = []


class EventIntervalIncomplete(RuntimeError):
    """Event scan failed after the fixed daily position/OI snapshots passed."""

    def __init__(self, first_daily: dict[str, Any], reason: str) -> None:
        super().__init__(reason)
        self.first_daily = first_daily


def gql(query: str) -> dict[str, Any]:
    body = json.dumps({"query": query}).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(4):
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        request = Request(
            ENDPOINT,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "quant-paper-g5-audit/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read())
                http_status = response.status
            if payload.get("errors"):
                REQUEST_LOG.append(
                    {
                        "started_at_utc": started_at,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000),
                        "http_status": http_status,
                        "outcome": "graphql_error",
                        "attempt": attempt + 1,
                    }
                )
                raise RuntimeError(json.dumps(payload["errors"]))
            if "data" not in payload:
                REQUEST_LOG.append(
                    {
                        "started_at_utc": started_at,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000),
                        "http_status": http_status,
                        "outcome": "missing_data",
                        "attempt": attempt + 1,
                    }
                )
                raise RuntimeError("GraphQL response omitted data")
            REQUEST_LOG.append(
                {
                    "started_at_utc": started_at,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "http_status": http_status,
                    "outcome": "success",
                    "attempt": attempt + 1,
                }
            )
            time.sleep(PAGE_DELAY_SECONDS)
            return payload["data"]
        except HTTPError as error:
            REQUEST_LOG.append(
                {
                    "started_at_utc": started_at,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "http_status": error.code,
                    "outcome": "http_error",
                    "attempt": attempt + 1,
                }
            )
            last_error = error
            if error.code != 429 and error.code < 500:
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2**attempt
        except (URLError, TimeoutError) as error:
            REQUEST_LOG.append(
                {
                    "started_at_utc": started_at,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "http_status": None,
                    "outcome": type(error).__name__,
                    "attempt": attempt + 1,
                }
            )
            last_error = error
            delay = 2**attempt
        if attempt < 3:
            time.sleep(min(delay, 30))
    raise RuntimeError("GraphQL request failed after retries") from last_error


def fetch_connection(
    field: str,
    where: str,
    order_by: str,
    selected_fields: str,
    page_size: int,
) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    expected_count: int | None = None
    pages = 0
    for _ in range(1000):
        after = ""
        if cursor is not None:
            escaped = cursor.replace("\\", "\\\\").replace('"', '\\"')
            after = f',after:"{escaped}"'
        query = (
            "{"
            + f"{field}(first:{page_size}{after},orderBy:{order_by},where:{where})"
            + "{totalCount pageInfo{hasNextPage endCursor} edges{node{"
            + selected_fields
            + "}}}"
            + "}"
        )
        connection = gql(query)[field]
        reported_count = int(connection["totalCount"])
        if expected_count is None:
            expected_count = reported_count
        elif reported_count != expected_count:
            raise RuntimeError(
                f"{field} totalCount changed during pagination: "
                f"{expected_count} -> {reported_count}"
            )
        page_rows = [edge["node"] for edge in connection["edges"]]
        rows.extend(page_rows)
        pages += 1
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        next_cursor = page_info.get("endCursor")
        if next_cursor is None or next_cursor == cursor:
            raise RuntimeError(f"{field} pagination cursor did not advance")
        cursor = str(next_cursor)
    else:
        raise RuntimeError(f"{field} exceeded 1000 pages")
    if expected_count is None or len(rows) != expected_count:
        raise RuntimeError(
            f"{field} incomplete: returned {len(rows)}, expected {expected_count}"
        )
    return rows, expected_count, pages


def utc_midnight(day: date) -> int:
    stamp = datetime.combine(day, day_time.min, tzinfo=UTC)
    return int(stamp.timestamp())


def dollars(value: Decimal) -> float:
    return float(value / Decimal(10**30))


def market_side_sha256(rows: list[tuple[str, int, int, int, int]]) -> str:
    """Hash sorted market and side aggregates without retaining source rows."""
    canonical = "\n".join("|".join(map(str, row)) for row in sorted(rows))
    return hashlib.sha256(canonical.encode()).hexdigest()


def fetch_reconciled_trade_actions(
    start_ts: int, end_ts: int, end_oi_block: int
) -> tuple[set[str], dict[str, Any], int]:
    """Check source-internal event freshness and two complete interval scans."""
    where = f"{{timestamp_gte:{start_ts},timestamp_lt:{end_ts},account_isNull:false}}"
    status_checks: list[dict[str, int]] = []
    scan_pages: list[int] = []
    scans: list[tuple[int, str]] = []
    discovered_accounts: set[str] = set()

    def check_status() -> None:
        status = gql("{squidStatus{height finalizedHeight}}").get("squidStatus")
        if not isinstance(status, dict) or any(
            type(status.get(field)) is not int for field in ("height", "finalizedHeight")
        ):
            raise RuntimeError("missing or malformed squidStatus")
        height = status["height"]
        finalized = status["finalizedHeight"]
        if height < finalized or finalized < end_oi_block:
            raise RuntimeError("squidStatus finalizedHeight below end OI block")
        if status_checks and (
            height < status_checks[-1]["height"]
            or finalized < status_checks[-1]["finalizedHeight"]
        ):
            raise RuntimeError("squidStatus regressed during TradeAction scans")
        status_checks.append({"height": height, "finalizedHeight": finalized})

    for cycle in range(2):
        check_status()
        rows, count, pages = fetch_connection(
            "tradeActionsConnection", where, "timestamp_ASC", EVENT_FIELDS, 5000
        )
        seen_ids: set[str] = set()
        tuples: list[tuple[str, str, int]] = []
        for row in rows:
            event_id = row.get("id")
            account = row.get("account")
            if not isinstance(event_id, str) or not event_id.strip():
                raise RuntimeError("TradeAction missing or empty ID")
            if event_id in seen_ids:
                raise RuntimeError("TradeAction duplicate ID")
            seen_ids.add(event_id)
            if not isinstance(account, str) or not account.strip():
                raise RuntimeError("TradeAction null or empty account")
            try:
                timestamp = int(str(row["timestamp"]))
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("TradeAction missing or malformed timestamp") from error
            if not start_ts <= timestamp < end_ts:
                raise RuntimeError("TradeAction timestamp outside fixed interval")
            tuples.append((event_id, account, timestamp))
            if cycle == 0:
                discovered_accounts.add(account.lower())
        digest = hashlib.sha256(
            json.dumps(sorted(tuples), separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        scans.append((count, digest))
        scan_pages.append(pages)
        check_status()
    if scans[0] != scans[1]:
        raise RuntimeError("TradeAction interval scans changed count or digest")
    return discovered_accounts, {
        "status": "source_internal_two_scan_consistent",
        "trade_action_count": scans[0][0],
        "trade_action_sha256": scans[0][1],
        "scan_pages": scan_pages,
        "status_checks": status_checks,
    }, sum(scan_pages) + len(status_checks)


def analyze(start_day: date, end_day: date) -> dict[str, Any]:
    start_ts = utc_midnight(start_day)
    end_ts = utc_midnight(end_day)
    if end_ts <= start_ts:
        raise ValueError("--end must be later than --start")
    if (end_day - start_day).days > 31:
        raise ValueError("The audit window may not exceed 31 days")
    request_log_start = len(REQUEST_LOG)

    snapshot_days = [
        start_day + timedelta(days=offset)
        for offset in range(1, (end_day - start_day).days + 1)
    ]
    daily: list[dict[str, Any]] = []
    active_market_sets: list[set[str]] = []
    end_positions: list[dict[str, Any]] = []
    all_market_coverages: list[float] = []
    all_side_coverages: list[float] = []
    end_oi_raw = 0
    total_pages = 0

    for snapshot_day in snapshot_days:
        snapshot_ts = utc_midnight(snapshot_day)
        position_where = (
            "{isSnapshot_eq:true,"
            f"snapshotTimestamp_eq:{snapshot_ts},sizeInUsd_gt:\"0\"}}"
        )
        positions, position_count, position_pages = fetch_connection(
            "positionsConnection",
            position_where,
            "id_ASC",
            POSITION_FIELDS,
            1000,
        )
        total_pages += position_pages
        position_ids = [row["id"] for row in positions]
        if len(set(position_ids)) != position_count:
            raise RuntimeError(f"Duplicate position IDs on {snapshot_day}")
        if any(int(row["snapshotTimestamp"]) != snapshot_ts for row in positions):
            raise RuntimeError(f"Position timestamp mismatch on {snapshot_day}")
        if any(not row.get("account") for row in positions):
            raise RuntimeError(f"Null or empty position account on {snapshot_day}")

        oi_where = f"{{snapshotTimestamp_eq:{snapshot_ts}}}"
        oi_rows, oi_count, oi_pages = fetch_connection(
            "fundingBalanceOiSnapshotsConnection",
            oi_where,
            "marketAddress_ASC",
            OI_FIELDS,
            500,
        )
        total_pages += oi_pages
        oi_markets = [row["marketAddress"].lower() for row in oi_rows]
        oi_market_set = set(oi_markets)
        if not oi_rows:
            raise RuntimeError(f"Fixed-time OI snapshot absent on {snapshot_day}")
        if len(oi_market_set) != oi_count:
            raise RuntimeError(f"Duplicate OI markets on {snapshot_day}")
        if any(int(row["snapshotTimestamp"]) != snapshot_ts for row in oi_rows):
            raise RuntimeError(f"OI snapshot timestamp mismatch on {snapshot_day}")
        blocks = {int(row["blockNumber"]) for row in oi_rows}
        if len(blocks) != 1 or any(int(row["blockTimestamp"]) != snapshot_ts for row in oi_rows):
            raise RuntimeError(f"OI rows do not share fixed-time block on {snapshot_day}")

        positions_by_market: dict[str, dict[str, int]] = {}
        for row in positions:
            size = int(str(row["sizeInUsd"]))
            if size < 0:
                raise RuntimeError(f"Negative position size on {snapshot_day}")
            market = row["market"].lower()
            side = "long" if row["isLong"] else "short"
            bucket = positions_by_market.setdefault(
                market, {"long": 0, "short": 0}
            )
            bucket[side] += size

        active_market_ratios: list[float] = []
        active_market_raw_ratios: list[Decimal] = []
        active_markets: set[str] = set()
        side_coverage_ratios: list[float] = []
        total_position_usd = 0
        total_oi_usd = 0
        market_sides: list[tuple[str, int, int, int, int]] = []
        unmatched_position_markets = 0
        for market in positions_by_market:
            if market not in oi_market_set:
                unmatched_position_markets += 1
        if unmatched_position_markets:
            raise RuntimeError(
                f"Positive positions in {unmatched_position_markets} "
                f"unmatched markets on {snapshot_day}"
            )
        zero_oi_markets = 0
        for row in oi_rows:
            market = row["marketAddress"].lower()
            long_oi = int(str(row["longOpenInterestUsd"]))
            short_oi = int(str(row["shortOpenInterestUsd"]))
            oi = long_oi + short_oi
            if min(long_oi, short_oi) < 0:
                raise RuntimeError(f"Negative OI on {snapshot_day}")
            sides = positions_by_market.get(market, {"long": 0, "short": 0})
            market_sides.append((market, sides["long"], sides["short"], long_oi, short_oi))
            if oi == 0:
                zero_oi_markets += 1
                if sides["long"] + sides["short"] > 0:
                    raise RuntimeError(f"Positive position in zero-OI market on {snapshot_day}")
                continue
            active_markets.add(market)
            position_size = sides["long"] + sides["short"]
            total_position_usd += position_size
            total_oi_usd += oi
            with localcontext() as context:
                context.prec = 80
                market_ratio = Decimal(position_size) / Decimal(oi)
            active_market_raw_ratios.append(market_ratio)
            active_market_ratios.append(float(market_ratio))
            long_position = sides["long"]
            short_position = sides["short"]
            if long_oi > 0:
                side_coverage_ratios.append(float(long_position / long_oi))
            if short_oi > 0:
                side_coverage_ratios.append(float(short_position / short_oi))

        if not active_market_ratios:
            raise RuntimeError(f"No positive-OI markets on {snapshot_day}")
        all_market_coverages.extend(active_market_ratios)
        all_side_coverages.extend(side_coverage_ratios)
        active_market_sets.append(active_markets)
        if snapshot_ts == end_ts:
            end_oi_raw = total_oi_usd
        if snapshot_ts == end_ts:
            end_positions = positions
        daily.append(
            {
                "date_utc": snapshot_day.isoformat(),
                "snapshot_utc": datetime.fromtimestamp(snapshot_ts, UTC).isoformat(),
                "position_rows": len(positions),
                "position_count": position_count,
                "oi_rows": len(oi_rows),
                "oi_market_count": oi_count,
                "oi_block": blocks.pop(),
                "positive_oi_markets": len(active_market_ratios),
                "zero_oi_markets": zero_oi_markets,
                "position_notional_raw": total_position_usd,
                "indexer_oi_raw": total_oi_usd,
                "position_ids_sha256": hashlib.sha256(
                    "\n".join(sorted(position_ids)).encode()
                ).hexdigest(),
                "market_side_sha256": market_side_sha256(market_sides),
                "market_ratio_minimum": str(min(active_market_raw_ratios)),
                "venue_coverage": float(total_position_usd / total_oi_usd),
                "market_coverage_median": statistics.median(active_market_ratios),
                "market_coverage_minimum": min(active_market_ratios),
                "side_coverage_median": statistics.median(side_coverage_ratios),
                "side_coverage_minimum": min(side_coverage_ratios),
                "position_notional_usd": dollars(Decimal(total_position_usd)),
                "venue_oi_usd": dollars(Decimal(total_oi_usd)),
                "unmatched_position_markets": unmatched_position_markets,
                "oi_block_count": len({int(row["blockNumber"]) for row in oi_rows}),
                "max_oi_block_delay_seconds": max(
                    int(row["blockTimestamp"]) - snapshot_ts for row in oi_rows
                ),
            }
        )

    try:
        discovered_accounts, event_reconciliation, event_requests = (
            fetch_reconciled_trade_actions(start_ts, end_ts, daily[-1]["oi_block"])
        )
    except Exception as error:
        raise EventIntervalIncomplete(daily[0], str(error)) from error
    total_pages += event_requests

    cold_rows = [
        row
        for row in end_positions
        if int(row["openedAt"]) < start_ts
        and row["account"].lower() not in discovered_accounts
    ]
    cold_notional = sum(int(str(row["sizeInUsd"])) for row in cold_rows)
    if end_oi_raw <= 0:
        raise RuntimeError("End-of-window OI is not positive")

    daily_coverages = [row["venue_coverage"] for row in daily]
    with localcontext() as context:
        context.prec = 80
        raw_coverages = [
            Decimal(row["position_notional_raw"]) / Decimal(row["indexer_oi_raw"])
            for row in daily
        ]
        exact_median = statistics.median(raw_coverages)
    coverage_median = float(exact_median)
    cold_gap = float(cold_notional / end_oi_raw)
    return {
        "status": "G5_MET_ON_REVISED_GMX_AUDIT"
        if exact_median >= Decimal("0.70")
        else "G5_NOT_MET",
        "analysis_class": "retrospective_exploratory_not_preregistered",
        "venue": "GMX Arbitrum One",
        "endpoint": ENDPOINT,
        "window_start_utc": datetime.fromtimestamp(
            start_ts, UTC
        ).isoformat(),
        "window_end_utc": datetime.fromtimestamp(
            end_ts, UTC
        ).isoformat(),
        "coverage_definition": (
            "sum of positive open-position USD sizes divided by same-timestamp "
            "long plus short venue-reported OI"
        ),
        "daily_snapshot_count": len(daily),
        "daily_venue_coverage_median": coverage_median,
        "daily_venue_coverage_minimum": min(daily_coverages),
        "market_day_coverage_median": statistics.median(all_market_coverages),
        "market_day_coverage_minimum": min(all_market_coverages),
        "side_coverage_median": statistics.median(all_side_coverages),
        "side_coverage_minimum": min(all_side_coverages),
        "active_market_day_count": sum(
            row["positive_oi_markets"] for row in daily
        ),
        "active_market_union_count": len(set.union(*active_market_sets)),
        "active_market_intersection_count": len(set.intersection(*active_market_sets)),
        "daily": daily,
        "cold_start_definition": (
            "end-snapshot positions opened before window start whose account "
            "does not appear in any TradeAction during [start,end)"
        ),
        "trade_action_rows": event_reconciliation["trade_action_count"],
        "trade_action_rows_fetched": event_reconciliation["trade_action_count"],
        "event_source_internal_reconciliation": event_reconciliation,
        "distinct_trade_action_accounts": len(discovered_accounts),
        "end_snapshot_position_rows": len(end_positions),
        "end_snapshot_distinct_accounts": len(
            {row["account"].lower() for row in end_positions}
        ),
        "cold_position_rows": len(cold_rows),
        "cold_distinct_accounts": len(
            {row["account"].lower() for row in cold_rows}
        ),
        "cold_notional_usd": dollars(Decimal(cold_notional)),
        "end_snapshot_oi_usd": dollars(Decimal(end_oi_raw)),
        "cold_start_gap_ratio": cold_gap,
        "trade_discovery_coverage_ratio": 1 - cold_gap,
        "position_addresses_persisted": False,
        "api_requests_counted": total_pages,
        "graphql_request_attempts": REQUEST_LOG[request_log_start:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    print(json.dumps(analyze(arguments.start, arguments.end), indent=2))
