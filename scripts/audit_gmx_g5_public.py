#!/usr/bin/env python3
"""Recompute the GMX Arbitrum position-coverage and cold-start audit.

All wallet addresses remain in memory for the trade-feed join. The script
prints aggregate counts and ratios only; it does not write raw rows to disk.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = "https://gmx.squids.live/gmx-synthetics-arbitrum:prod/api/graphql"
POSITION_FIELDS = "id account market isLong sizeInUsd openedAt snapshotTimestamp"
OI_FIELDS = (
    "marketAddress snapshotTimestamp blockTimestamp blockNumber "
    "longOpenInterestUsd shortOpenInterestUsd"
)
EVENT_FIELDS = "account timestamp"
PAGE_DELAY_SECONDS = 0.25
REQUEST_LOG: list[dict[str, Any]] = []


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


def decimal_value(value: Any) -> Decimal:
    return Decimal(str(value))


def dollars(value: Decimal) -> float:
    return float(value / Decimal(10**30))


def analyze(start_day: date, end_day: date) -> dict[str, Any]:
    start_ts = utc_midnight(start_day)
    end_ts = utc_midnight(end_day)
    if end_ts <= start_ts:
        raise ValueError("--end must be later than --start")
    if (end_day - start_day).days > 31:
        raise ValueError("The audit window may not exceed 31 days")

    snapshot_days = [
        start_day + timedelta(days=offset)
        for offset in range(1, (end_day - start_day).days + 1)
    ]
    daily: list[dict[str, Any]] = []
    active_market_sets: list[set[str]] = []
    end_positions: list[dict[str, Any]] = []
    all_market_coverages: list[float] = []
    all_side_coverages: list[float] = []
    end_oi_raw = Decimal(0)
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
        if len(set(position_ids)) != len(position_ids):
            raise RuntimeError(f"Duplicate position IDs on {snapshot_day}")
        if any(int(row["snapshotTimestamp"]) != snapshot_ts for row in positions):
            raise RuntimeError(f"Position timestamp mismatch on {snapshot_day}")

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
        if len(set(oi_markets)) != len(oi_markets):
            raise RuntimeError(f"Duplicate OI markets on {snapshot_day}")
        if any(int(row["snapshotTimestamp"]) != snapshot_ts for row in oi_rows):
            raise RuntimeError(f"OI snapshot timestamp mismatch on {snapshot_day}")

        positions_by_market: dict[str, dict[str, Decimal]] = {}
        for row in positions:
            market = row["market"].lower()
            side = "long" if row["isLong"] else "short"
            bucket = positions_by_market.setdefault(
                market, {"long": Decimal(0), "short": Decimal(0)}
            )
            bucket[side] += decimal_value(row["sizeInUsd"])

        active_market_ratios: list[float] = []
        active_markets: set[str] = set()
        side_coverage_ratios: list[float] = []
        total_position_usd = Decimal(0)
        total_oi_usd = Decimal(0)
        unmatched_position_markets = 0
        for market in positions_by_market:
            if market not in oi_market_set:
                unmatched_position_markets += 1
        for row in oi_rows:
            market = row["marketAddress"].lower()
            long_oi = decimal_value(row["longOpenInterestUsd"])
            short_oi = decimal_value(row["shortOpenInterestUsd"])
            oi = long_oi + short_oi
            if oi <= 0:
                sides = positions_by_market.get(market)
                if sides and sides["long"] + sides["short"] > 0:
                    raise RuntimeError(
                        f"Positive positions with non-positive OI on {snapshot_day}: {market}"
                    )
                continue
            active_markets.add(market)
            sides = positions_by_market.get(
                market, {"long": Decimal(0), "short": Decimal(0)}
            )
            position_size = sides["long"] + sides["short"]
            total_position_usd += position_size
            total_oi_usd += oi
            active_market_ratios.append(float(position_size / oi))
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
                "position_rows": len(positions),
                "position_total_count": position_count,
                "oi_rows": len(oi_rows),
                "oi_total_count": oi_count,
                "positive_oi_markets": len(active_market_ratios),
                "venue_coverage": float(total_position_usd / total_oi_usd),
                "market_coverage_median": statistics.median(active_market_ratios),
                "market_coverage_minimum": min(active_market_ratios),
                "side_coverage_median": statistics.median(side_coverage_ratios),
                "side_coverage_minimum": min(side_coverage_ratios),
                "position_notional_usd": dollars(total_position_usd),
                "venue_oi_usd": dollars(total_oi_usd),
                "unmatched_position_markets": unmatched_position_markets,
                "oi_block_count": len({int(row["blockNumber"]) for row in oi_rows}),
                "max_oi_block_delay_seconds": max(
                    int(row["blockTimestamp"]) - snapshot_ts for row in oi_rows
                ),
            }
        )

    if not end_positions:
        raise RuntimeError("No end-of-window position snapshot was found")

    trade_where = (
        f"{{timestamp_gte:{start_ts},timestamp_lt:{end_ts},account_isNull:false}}"
    )
    trade_rows, trade_count, trade_pages = fetch_connection(
        "tradeActionsConnection",
        trade_where,
        "timestamp_ASC",
        EVENT_FIELDS,
        5000,
    )
    total_pages += trade_pages
    discovered_accounts = {row["account"].lower() for row in trade_rows}

    cold_rows = [
        row
        for row in end_positions
        if int(row["openedAt"]) < start_ts
        and row["account"].lower() not in discovered_accounts
    ]
    cold_notional = sum(
        (decimal_value(row["sizeInUsd"]) for row in cold_rows), Decimal(0)
    )
    if end_oi_raw <= 0:
        raise RuntimeError("End-of-window OI is not positive")

    daily_coverages = [row["venue_coverage"] for row in daily]
    coverage_median = statistics.median(daily_coverages)
    cold_gap = float(cold_notional / end_oi_raw)
    return {
        "status": "G5_MET_ON_REVISED_GMX_AUDIT"
        if coverage_median >= 0.70
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
        "trade_action_rows": trade_count,
        "trade_action_rows_fetched": len(trade_rows),
        "distinct_trade_action_accounts": len(discovered_accounts),
        "end_snapshot_position_rows": len(end_positions),
        "end_snapshot_distinct_accounts": len(
            {row["account"].lower() for row in end_positions}
        ),
        "cold_position_rows": len(cold_rows),
        "cold_distinct_accounts": len(
            {row["account"].lower() for row in cold_rows}
        ),
        "cold_notional_usd": dollars(cold_notional),
        "end_snapshot_oi_usd": dollars(end_oi_raw),
        "cold_start_gap_ratio": cold_gap,
        "trade_discovery_coverage_ratio": 1 - cold_gap,
        "position_addresses_persisted": False,
        "api_requests_counted": total_pages,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    print(json.dumps(analyze(arguments.start, arguments.end), indent=2))
