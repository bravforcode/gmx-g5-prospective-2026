#!/usr/bin/env python3
"""Collect the frozen GMX G5 UTC window; persist aggregates, never wallet rows.

The public GitHub Actions job may be delayed. Every query uses the exact
preregistered snapshotTimestamp, not the runner's current block/time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from scripts.audit_gmx_g5_public import (
    ENDPOINT,
    OI_FIELDS,
    POSITION_FIELDS,
    REQUEST_LOG,
    EventIntervalIncomplete,
    analyze,
    fetch_connection,
    market_side_sha256,
    utc_midnight,
)

PROTOCOL_SHA256 = "efa241ab8bb57be1ae582c15a0bdef4932e14005613e3dd5375c96892e95fee0"
PROTOCOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "reports"
    / "g5_gmx_protocol_frozen_v1_2026-09-26.md"
)
PROTOCOL_URL = "https://gist.github.com/bravforcode/d139701fb92b4dad22e89756e9e14fa8"
BASELINE = date(2026, 9, 27)
END = date(2026, 10, 4)
STOP = datetime(2026, 10, 11, tzinfo=UTC)
RPC_URLS = (
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum-one.public.blastapi.io",
)
INDEPENDENT_RPC_TIMEOUT_SECONDS = 360
INDEPENDENT_CHECKS = (
    "full_position_key_set",
    "market_universe",
    "oi_raw_unit_tolerance",
    "sampled_position_sizes",
    "count_alignment",
    "primary_amount_alignment",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def check_protocol() -> None:
    actual = hashlib.sha256(PROTOCOL_PATH.read_bytes()).hexdigest()
    if actual != PROTOCOL_SHA256:
        raise RuntimeError(f"frozen protocol SHA-256 mismatch: {actual}")


def code_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class InvalidReceiptShape(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(value, dict):
        raise InvalidReceiptShape("JSON receipt is not an object")
    return value


def ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        raise ValueError("coverage denominator must be positive")
    with localcontext() as context:
        context.prec = 80
        return str(Decimal(numerator) / Decimal(denominator))


def primary_receipt_matches(day: date, receipt: dict[str, Any]) -> bool:
    return (
        receipt.get("status") == "primary_complete"
        and receipt.get("protocol_sha256") == PROTOCOL_SHA256
        and receipt.get("source") == ENDPOINT
        and receipt.get("snapshot_utc")
        == datetime.fromtimestamp(utc_midnight(day), UTC).isoformat()
    )


def independently_validated(day: date, receipt: dict[str, Any]) -> bool:
    if not primary_receipt_matches(day, receipt):
        return False
    independent = receipt.get("independent")
    if not isinstance(independent, dict) or independent.get("status") != "complete":
        return False
    checks = independent.get("checks")
    proof = independent.get("proof")
    if not isinstance(checks, dict) or not isinstance(proof, dict):
        return False
    if any(checks.get(name) is not True for name in INDEPENDENT_CHECKS):
        return False
    if independent.get("rpc_url") not in RPC_URLS:
        return False
    if not isinstance(proof.get("block_hash"), str) or not proof["block_hash"]:
        return False
    try:
        return (
            proof["block"] == receipt["oi_block"]
            and proof["block_time_utc"] == receipt["snapshot_utc"]
            and proof["chain_id"] == 42161
            and proof["complete_set_read"] is True
            and proof["onchain_only_keys"] == proof["indexer_only_keys"] == 0
            and proof["indexer_only_markets"] == 0
            and proof["chain_only_positive_oi_markets"] == 0
            and proof["oi_max_abs_difference_raw"] <= 1
            and proof["oi_exact_sides"] <= 2 * proof["oi_market_rows"]
            and proof["oi_market_rows"] == proof["indexer_oi_market_count"]
            == receipt["oi_market_count"]
            and proof["indexer_position_count"] == receipt["position_count"]
            and proof["position_values_sampled"] == 7
            and proof["position_value_mismatches"] == 0
            and str(proof["oi_total_indexer_raw"]) == str(receipt["indexer_oi_raw"])
            and str(proof["indexer_position_size_total_raw"])
            == str(receipt["position_notional_raw"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def collect_primary(day: date) -> dict[str, Any]:
    request_log_start = len(REQUEST_LOG)
    timestamp = utc_midnight(day)
    where = "{isSnapshot_eq:true," + f'snapshotTimestamp_eq:{timestamp},sizeInUsd_gt:"0"' + "}"
    positions, position_count, position_pages = fetch_connection(
        "positionsConnection", where, "id_ASC", POSITION_FIELDS, 1000
    )
    oi_rows, oi_count, oi_pages = fetch_connection(
        "fundingBalanceOiSnapshotsConnection",
        f"{{snapshotTimestamp_eq:{timestamp}}}",
        "marketAddress_ASC",
        OI_FIELDS,
        500,
    )
    if not oi_rows:
        raise RuntimeError("fixed-time OI snapshot absent")
    ids = [str(row["id"]) for row in positions]
    if len(set(ids)) != position_count:
        raise RuntimeError("duplicate or missing position IDs")
    if any(int(row["snapshotTimestamp"]) != timestamp for row in positions):
        raise RuntimeError("position snapshotTimestamp mismatch")
    if any(not row.get("account") for row in positions):
        raise RuntimeError("null or empty position account")
    markets = [str(row["marketAddress"]).lower() for row in oi_rows]
    if len(set(markets)) != oi_count:
        raise RuntimeError("duplicate or missing OI markets")
    if any(int(row["snapshotTimestamp"]) != timestamp for row in oi_rows):
        raise RuntimeError("OI snapshotTimestamp mismatch")
    blocks = {int(row["blockNumber"]) for row in oi_rows}
    if len(blocks) != 1 or any(int(row["blockTimestamp"]) != timestamp for row in oi_rows):
        raise RuntimeError("OI rows do not share the fixed-time block")

    sizes: dict[tuple[str, bool], int] = {}
    for row in positions:
        size = int(row["sizeInUsd"])
        if size < 0:
            raise RuntimeError("negative position size")
        market = str(row["market"]).lower()
        key = (market, bool(row["isLong"]))
        sizes[key] = sizes.get(key, 0) + size
    oi_market_set = set(markets)
    unmatched = {market for market, _ in sizes if market not in oi_market_set}
    if unmatched:
        raise RuntimeError(f"positive positions in {len(unmatched)} unmatched markets")

    venue_position = 0
    venue_oi = 0
    active = 0
    zero_oi = 0
    market_ratios: list[Decimal] = []
    market_sides: list[tuple[str, int, int, int, int]] = []
    for row in oi_rows:
        market = str(row["marketAddress"]).lower()
        long_oi = int(row["longOpenInterestUsd"])
        short_oi = int(row["shortOpenInterestUsd"])
        long_size = sizes.get((market, True), 0)
        short_size = sizes.get((market, False), 0)
        if min(long_oi, short_oi, long_size, short_size) < 0:
            raise RuntimeError("negative OI or position size")
        market_sides.append((market, long_size, short_size, long_oi, short_oi))
        denominator = long_oi + short_oi
        numerator = long_size + short_size
        if denominator == 0:
            zero_oi += 1
            if numerator:
                raise RuntimeError("positive position in zero-OI market")
            continue
        active += 1
        venue_position += numerator
        venue_oi += denominator
        with localcontext() as context:
            context.prec = 80
            market_ratios.append(Decimal(numerator) / Decimal(denominator))
    if not active or venue_oi <= 0:
        raise RuntimeError("no positive-OI markets")

    return {
        "status": "primary_complete",
        "snapshot_utc": datetime.fromtimestamp(timestamp, UTC).isoformat(),
        "retrieved_at_utc": now_iso(),
        "source": ENDPOINT,
        "collector_code_sha256": code_hash(Path(__file__)),
        "audit_code_sha256": code_hash(Path(__file__).with_name("audit_gmx_g5_public.py")),
        "position_fields": POSITION_FIELDS,
        "oi_fields": OI_FIELDS,
        "position_count": position_count,
        "position_pages": position_pages,
        "graphql_request_attempts": REQUEST_LOG[request_log_start:],
        "oi_market_count": oi_count,
        "oi_pages": oi_pages,
        "oi_block": blocks.pop(),
        "positive_oi_markets": active,
        "zero_oi_markets": zero_oi,
        "position_notional_raw": str(venue_position),
        "indexer_oi_raw": str(venue_oi),
        "venue_coverage": ratio(venue_position, venue_oi),
        "market_ratio_minimum": str(min(market_ratios)),
        "position_ids_sha256": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest(),
        "market_side_sha256": market_side_sha256(market_sides),
    }


def collect_independent(
    day: date,
    block: int,
    expected: int,
    expected_oi_raw: str,
    expected_position_raw: str,
) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    command = [
        sys.executable,
        str(Path(__file__).with_name("probe_gmx_onchain_position_set.py")),
        "--block",
        str(block),
        "--expected",
        str(expected),
        "--all-keys",
        "--compare-indexer",
        "--sample-values",
        "7",
        "--all-markets",
        "--market-list",
    ]
    for rpc_url in RPC_URLS:
        environment = {**os.environ, "GMX_RPC_URL": rpc_url}
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=INDEPENDENT_RPC_TIMEOUT_SECONDS,
                check=False,
                env=environment,
            )
            if process.returncode:
                raise RuntimeError(process.stderr.strip()[-500:])
            proof = json.loads(process.stdout)
            expected_time = datetime.fromtimestamp(utc_midnight(day), UTC).isoformat()
            if proof["block_time_utc"] != expected_time:
                raise RuntimeError("on-chain block time differs from scheduled time")
            expected_sides = 2 * proof["oi_market_rows"]
            checks = {
                "full_position_key_set": proof["complete_set_read"]
                and proof["onchain_only_keys"] == 0
                and proof["indexer_only_keys"] == 0,
                "market_universe": proof["indexer_only_markets"] == 0
                and proof["chain_only_positive_oi_markets"] == 0,
                "oi_raw_unit_tolerance": proof["oi_max_abs_difference_raw"] <= 1
                and proof["oi_exact_sides"] <= expected_sides,
                "sampled_position_sizes": proof["position_values_sampled"] == 7
                and proof["position_value_mismatches"] == 0,
                "count_alignment": proof["oi_market_rows"] == proof["indexer_oi_market_count"]
                and proof["indexer_position_count"] == expected,
                "primary_amount_alignment": str(proof["oi_total_indexer_raw"])
                == expected_oi_raw
                and str(proof["indexer_position_size_total_raw"])
                == expected_position_raw,
            }
            return {
                "status": "complete" if all(checks.values()) else "failed_checks",
                "checked_at_utc": now_iso(),
                "rpc_url": rpc_url,
                "probe_code_sha256": code_hash(
                    Path(__file__).with_name("probe_gmx_onchain_position_set.py")
                ),
                "checks": checks,
                "proof": proof,
                "prior_rpc_failures": failures,
            }
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.TimeoutExpired,
        ) as error:
            failures.append({"rpc_url": rpc_url, "error": str(error)[-500:]})
    return {"status": "unavailable", "checked_at_utc": now_iso(), "rpc_failures": failures}


def process_day(day: date, output_dir: Path, *, defer_independent: bool = False) -> dict[str, Any]:
    path = output_dir / f"{day.isoformat()}.json"
    previous = read_json(path)
    attempts = list(previous.get("attempts", []))
    if previous.get("status") == "primary_complete":
        if defer_independent or independently_validated(day, previous):
            return previous
        independent = collect_independent(
            day,
            previous["oi_block"],
            previous["position_count"],
            previous["indexer_oi_raw"],
            previous["position_notional_raw"],
        )
        attempts.append(
            {"at_utc": now_iso(), "stage": "independent", "status": independent["status"]}
        )
        previous["attempts"] = attempts
        previous["independent"] = independent
        write_json(path, previous)
        return previous
    prior_requests = list(previous.get("graphql_request_attempts", []))
    request_log_start = len(REQUEST_LOG)
    try:
        primary = collect_primary(day)
    except Exception as error:
        attempts.append(
            {
                "at_utc": now_iso(),
                "stage": "primary",
                "status": "incomplete",
                "error": str(error)[-500:],
            }
        )
        receipt: dict[str, Any] = {
            "status": "primary_incomplete",
            "snapshot_date_utc": day.isoformat(),
            "protocol_sha256": PROTOCOL_SHA256,
            "attempts": attempts,
            "graphql_request_attempts": prior_requests + REQUEST_LOG[request_log_start:],
        }
        write_json(path, receipt)
        return receipt
    receipt = {
        **primary,
        "protocol_sha256": PROTOCOL_SHA256,
        "protocol_url": PROTOCOL_URL,
        "first_primary_success_at_utc": previous.get("first_primary_success_at_utc")
        or primary["retrieved_at_utc"],
        "attempts": attempts,
        "graphql_request_attempts": prior_requests + primary["graphql_request_attempts"],
        "independent": {"status": "pending"},
    }
    write_json(path, receipt)
    if defer_independent:
        return receipt
    independent = collect_independent(
        day,
        primary["oi_block"],
        primary["position_count"],
        primary["indexer_oi_raw"],
        primary["position_notional_raw"],
    )
    receipt["independent"] = independent
    attempts.append({"at_utc": now_iso(), "stage": "independent", "status": independent["status"]})
    write_json(path, receipt)
    return receipt


def verify_receipt_provenance(day: date, receipt: dict[str, Any]) -> None:
    expected_time = datetime.fromtimestamp(utc_midnight(day), UTC).isoformat()
    if receipt.get("snapshot_utc") != expected_time:
        raise RuntimeError(f"fixed date mismatch in receipt for {day}")
    if receipt.get("protocol_sha256") != PROTOCOL_SHA256:
        raise RuntimeError(f"protocol_sha256 mismatch in receipt for {day}")
    if receipt.get("source") != ENDPOINT:
        raise RuntimeError(f"source mismatch in receipt for {day}")


def verify_refetched_day(
    day: date, observed: dict[str, Any], receipt: dict[str, Any]
) -> None:
    """Require the refetched aggregate to reconcile with the saved fixed-time receipt."""
    verify_receipt_provenance(day, receipt)
    if observed.get("date_utc") != day.isoformat():
        raise RuntimeError(f"refetch fixed date mismatch on {day}")
    for field in (
        "snapshot_utc", "position_count", "oi_market_count", "oi_block",
        "positive_oi_markets", "zero_oi_markets", "position_notional_raw",
        "indexer_oi_raw", "position_ids_sha256",
    ):
        if str(observed.get(field)) != str(receipt.get(field)):
            raise RuntimeError(f"refetch receipt mismatch on {day}: {field}")
    with localcontext() as context:
        context.prec = 80
        expected_coverage = (
            Decimal(observed["position_notional_raw"])
            / Decimal(observed["indexer_oi_raw"])
        )
    for field, expected in (
        ("venue_coverage", expected_coverage),
        ("market_ratio_minimum", Decimal(observed["market_ratio_minimum"])),
    ):
        if field not in receipt or Decimal(str(receipt[field])) != expected:
            raise RuntimeError(f"refetch receipt mismatch on {day}: {field}")
    if "market_side_sha256" not in receipt:
        raise RuntimeError(
            f"primary receipt missing market_side_sha256 on {day}; "
            "fixed-time recollection required"
        )
    if observed["market_side_sha256"] != receipt["market_side_sha256"]:
        raise RuntimeError(f"refetch receipt mismatch on {day}: market_side_sha256")


def reconcile_refetched_days(
    dates: list[date], observed_days: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    if len(observed_days) != len(dates) or len(receipts) != len(dates):
        raise RuntimeError("refetch daily count mismatch")
    first_verified = "not achieved"
    errors: list[str] = []
    for day, observed, receipt in zip(dates, observed_days, receipts, strict=True):
        try:
            verify_refetched_day(day, observed, receipt)
        except Exception as error:
            errors.append(str(error))
        else:
            if first_verified == "not achieved":
                first_verified = datetime.fromtimestamp(utc_midnight(day), UTC).isoformat()
    return first_verified, errors


def maybe_finalize(output_dir: Path, result_dir: Path) -> None:
    if datetime.now(UTC) < datetime(2026, 10, 4, tzinfo=UTC):
        return
    final_path = result_dir / "g5_final.json"
    previous_attempts = read_json(final_path).get("finalization_attempts", [])
    if not isinstance(previous_attempts, list):
        raise RuntimeError("invalid prior finalization_attempts history")
    request_log_start = len(REQUEST_LOG)
    dates = [BASELINE + timedelta(days=offset) for offset in range(8)]
    receipts: list[dict[str, Any]] = []
    receipt_load_errors: list[dict[str, str]] = []
    for day in dates:
        try:
            receipt = read_json(output_dir / f"{day.isoformat()}.json")
            if not isinstance(receipt, dict):
                raise ValueError("daily receipt is not a JSON object")
        except (OSError, ValueError, UnicodeError) as error:
            receipt_load_errors.append(
                {"date_utc": day.isoformat(), "error_type": type(error).__name__}
            )
            receipt = {"status": "primary_incomplete"}
        receipts.append(receipt)
    independent_days = [
        day for day, receipt in zip(dates, receipts, strict=True)
        if independently_validated(day, receipt)
    ]
    independent_complete = len(independent_days)
    independent_status = (
        "complete" if independent_complete == 8
        else "partial" if independent_complete else "unavailable"
    )
    first_independent = next(
        (
            datetime.fromtimestamp(utc_midnight(day), UTC).isoformat()
            for day in independent_days
            if day > BASELINE
        ),
        "not achieved",
    )
    if any(receipt.get("status") != "primary_complete" for receipt in receipts):
        first_primary = "not achieved"
        partial_refetch_errors: list[dict[str, str]] = []
        for day, receipt in zip(dates[1:], receipts[1:], strict=True):
            if receipt.get("status") != "primary_complete":
                continue
            try:
                verify_receipt_provenance(day, receipt)
                observed = {"date_utc": day.isoformat(), **collect_primary(day)}
                verify_refetched_day(day, observed, receipt)
            except Exception as error:
                partial_refetch_errors.append({
                    "date_utc": day.isoformat(),
                    "error_type": type(error).__name__,
                })
            else:
                if first_primary == "not achieved":
                    first_primary = datetime.fromtimestamp(
                        utc_midnight(day), UTC
                    ).isoformat()
        write_json(
            final_path,
            {
                "primary_status": "inconclusive",
                "independent_chain_validation": independent_status,
                "time_to_stable_primary_utc": first_primary,
                "time_to_stable_independent_utc": first_independent,
                "reason": "one or more fixed-time receipts incomplete",
                "complete_receipts": sum(r.get("status") == "primary_complete" for r in receipts),
                "required_receipts": 8,
                "receipt_load_errors": receipt_load_errors,
                "partial_refetch_errors": partial_refetch_errors,
                "protocol_sha256": PROTOCOL_SHA256,
                "checked_at_utc": now_iso(),
                "finalization_attempts": previous_attempts + [{
                    "status": "inconclusive",
                    "reason": "one or more fixed-time receipts incomplete",
                    "checked_at_utc": now_iso(),
                    "receipt_load_errors": receipt_load_errors,
                    "graphql_request_attempts": REQUEST_LOG[request_log_start:],
                }],
            },
        )
        return
    first_primary_verified = "not achieved"
    baseline_error: str | None = None
    callback_errors: list[str] = []
    scanned_days = 0

    def observe_daily(observed: dict[str, Any]) -> None:
        nonlocal first_primary_verified, scanned_days
        if scanned_days >= len(dates) - 1:
            raise RuntimeError("refetch returned too many daily states")
        day = dates[scanned_days + 1]
        receipt = receipts[scanned_days + 1]
        scanned_days += 1
        try:
            verify_refetched_day(day, observed, receipt)
        except Exception as verification_error:
            callback_errors.append(str(verification_error))
        else:
            if first_primary_verified == "not achieved":
                first_primary_verified = datetime.fromtimestamp(
                    utc_midnight(day), UTC
                ).isoformat()

    try:
        try:
            verify_receipt_provenance(dates[0], receipts[0])
        except RuntimeError as error:
            baseline_error = str(error)
        metrics = analyze(BASELINE, END, on_daily=observe_daily)
        metrics.pop("status", None)
        metrics.pop("analysis_class", None)
        primary_days = receipts[1:]
        first_primary_verified, daily_errors = reconcile_refetched_days(
            dates[1:], metrics.get("daily", []), primary_days
        )
        if daily_errors:
            raise RuntimeError(
                f"daily receipt reconciliation failed ({len(daily_errors)} days): "
                f"{daily_errors[0]}"
            )
        if baseline_error is not None:
            raise RuntimeError(baseline_error)
        with localcontext() as context:
            context.prec = 80
            median = statistics.median(
                Decimal(day["position_notional_raw"]) / Decimal(day["indexer_oi_raw"])
                for day in metrics["daily"]
            )
        primary_status = "pass" if median >= Decimal("0.70") else "not_pass"
        write_json(
            final_path,
            {
                "analysis_class": "prospectively_deposited_GMX_v1_not_Hyperliquid",
                "primary_status": primary_status,
                "independent_chain_validation": independent_status,
                "time_to_stable_primary_utc": first_primary_verified,
                "time_to_stable_independent_utc": first_independent,
                "protocol_sha256": PROTOCOL_SHA256,
                "protocol_url": PROTOCOL_URL,
                "computed_at_utc": now_iso(),
                "metrics": metrics,
                "finalization_attempts": previous_attempts + [{
                    "status": primary_status,
                    "checked_at_utc": now_iso(),
                    "graphql_request_attempts": REQUEST_LOG[request_log_start:],
                }],
            },
        )
    except Exception as error:
        recovery_refetch_errors: list[dict[str, str]] = []
        for day, receipt in zip(
            dates[scanned_days + 1:], receipts[scanned_days + 1:], strict=True
        ):
            try:
                verify_receipt_provenance(day, receipt)
                observed = {"date_utc": day.isoformat(), **collect_primary(day)}
                verify_refetched_day(day, observed, receipt)
            except Exception as recovery_error:
                recovery_refetch_errors.append({
                    "date_utc": day.isoformat(),
                    "error_type": type(recovery_error).__name__,
                })
            else:
                if first_primary_verified == "not achieved":
                    first_primary_verified = datetime.fromtimestamp(
                        utc_midnight(day), UTC
                    ).isoformat()
        if isinstance(error, EventIntervalIncomplete):
            try:
                first_primary_verified, daily_errors = reconcile_refetched_days(
                    dates[1:], error.daily, receipts[1:]
                )
                if daily_errors:
                    error = RuntimeError(
                        f"{error}; daily receipt reconciliation failed "
                        f"({len(daily_errors)} days): {daily_errors[0]}"
                    )
            except Exception as verification_error:
                error = RuntimeError(
                    f"{error}; daily receipt reconciliation failed: {verification_error}"
                )
        elif callback_errors:
            error = RuntimeError(
                f"{error}; daily receipt reconciliation failed "
                f"({len(callback_errors)} days): {callback_errors[0]}"
            )
        if baseline_error is not None and baseline_error not in str(error):
            error = RuntimeError(f"{error}; {baseline_error}")
        write_json(
            final_path,
            {
                "primary_status": "inconclusive",
                "independent_chain_validation": independent_status,
                "time_to_stable_primary_utc": first_primary_verified,
                "time_to_stable_independent_utc": first_independent,
                "reason": str(error)[-500:],
                "protocol_sha256": PROTOCOL_SHA256,
                "checked_at_utc": now_iso(),
                "graphql_request_attempts": REQUEST_LOG[request_log_start:],
                "recovery_refetch_errors": recovery_refetch_errors,
                "finalization_attempts": previous_attempts + [{
                    "status": "inconclusive",
                    "reason": str(error)[-500:],
                    "checked_at_utc": now_iso(),
                    "graphql_request_attempts": REQUEST_LOG[request_log_start:],
                }],
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("receipts"))
    parser.add_argument("--result-dir", type=Path, default=Path("results"))
    parser.add_argument("--pilot-day", type=date.fromisoformat)
    parser.add_argument(
        "--phase", choices=("primary", "independent", "finalize", "all"), default="all"
    )
    args = parser.parse_args()
    check_protocol()
    if args.pilot_day:
        if args.pilot_day != date(2026, 9, 26):
            raise SystemExit("only the disclosed Sep 26 pilot is accepted for a test run")
        if args.phase == "finalize":
            raise SystemExit("pilot cannot enter the final-window phase")
        pilot_dir = args.output_dir / "pilot"
        if args.phase in {"primary", "all"}:
            process_day(args.pilot_day, pilot_dir, defer_independent=True)
        if args.phase in {"independent", "all"}:
            existing = read_json(pilot_dir / f"{args.pilot_day.isoformat()}.json")
            if existing.get("status") != "primary_complete":
                raise SystemExit("pilot primary receipt missing or incomplete")
            process_day(args.pilot_day, pilot_dir)
        receipt = read_json(pilot_dir / f"{args.pilot_day.isoformat()}.json")
        print(json.dumps({"date": args.pilot_day.isoformat(), "status": receipt["status"]}))
        return
    now = datetime.now(UTC)
    if now > STOP:
        print(json.dumps({"status": "collection_window_expired", "at_utc": now.isoformat()}))
        return
    due = [
        BASELINE + timedelta(days=offset)
        for offset in range(8)
        if utc_midnight(BASELINE + timedelta(days=offset)) <= now.timestamp()
    ]
    receipt_load_errors: list[dict[str, str]] = []
    corrupt_days: set[date] = set()
    try:
        if args.phase in {"primary", "all"}:
            for day in due:
                try:
                    process_day(day, args.output_dir, defer_independent=True)
                except (json.JSONDecodeError, UnicodeDecodeError, InvalidReceiptShape) as error:
                    corrupt_days.add(day)
                    receipt_load_errors.append(
                        {"date_utc": day.isoformat(), "error_type": type(error).__name__}
                    )
        if args.phase in {"independent", "all"}:
            for day in due:
                if day in corrupt_days:
                    continue
                try:
                    existing = read_json(args.output_dir / f"{day.isoformat()}.json")
                    if existing.get("status") == "primary_complete":
                        process_day(day, args.output_dir)
                except (json.JSONDecodeError, UnicodeDecodeError, InvalidReceiptShape) as error:
                    corrupt_days.add(day)
                    receipt_load_errors.append(
                        {"date_utc": day.isoformat(), "error_type": type(error).__name__}
                    )
    finally:
        if args.phase in {"finalize", "all"}:
            maybe_finalize(args.output_dir, args.result_dir)
    statuses: dict[str, object] = {}
    for day in due:
        if day in corrupt_days:
            statuses[day.isoformat()] = "corrupt_receipt"
            continue
        try:
            statuses[day.isoformat()] = read_json(
                args.output_dir / f"{day.isoformat()}.json"
            ).get("status")
        except (json.JSONDecodeError, UnicodeDecodeError, InvalidReceiptShape) as error:
            statuses[day.isoformat()] = "corrupt_receipt"
            receipt_load_errors.append(
                {"date_utc": day.isoformat(), "error_type": type(error).__name__}
            )
    summary: dict[str, object] = {"at_utc": now_iso(), "daily_statuses": statuses}
    if receipt_load_errors:
        summary["receipt_load_errors"] = receipt_load_errors
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
