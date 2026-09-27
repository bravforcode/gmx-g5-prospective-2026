"""Offline tests for the frozen-window collection gates."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest
from scripts import audit_gmx_g5_public as audit
from scripts import collect_gmx_g5_window as collector


@pytest.mark.parametrize(
    ("median", "expected"),
    [
        (Decimal("0.70"), "RETROSPECTIVE_NUMERICAL_THRESHOLD_MET"),
        (Decimal("0.699999999999"), "RETROSPECTIVE_NUMERICAL_THRESHOLD_NOT_MET"),
    ],
)
def test_retrospective_threshold_label_is_not_a_g5_claim(median, expected):
    assert audit.retrospective_threshold_status(median) == expected


def test_collect_primary_uses_fixed_timestamp_and_two_sided_oi(monkeypatch):
    day = date(2026, 9, 26)
    stamp = collector.utc_midnight(day)
    market = "0x" + "11" * 20
    observed: list[str] = []

    def fake_connection(field, where, *_args):
        observed.append(where)
        if field == "positionsConnection":
            return (
                [
                    {
                        "id": "a",
                        "account": "0x" + "aa" * 20,
                        "market": market,
                        "isLong": True,
                        "sizeInUsd": "60",
                        "snapshotTimestamp": stamp,
                    },
                    {
                        "id": "b",
                        "account": "0x" + "bb" * 20,
                        "market": market,
                        "isLong": False,
                        "sizeInUsd": "40",
                        "snapshotTimestamp": stamp,
                    },
                ],
                2,
                1,
            )
        return (
            [
                {
                    "marketAddress": market,
                    "snapshotTimestamp": stamp,
                    "blockTimestamp": stamp,
                    "blockNumber": 508920820,
                    "longOpenInterestUsd": "60",
                    "shortOpenInterestUsd": "40",
                }
            ],
            1,
            1,
        )

    monkeypatch.setattr(collector, "fetch_connection", fake_connection)
    result = collector.collect_primary(day)

    assert all(str(stamp) in where for where in observed)
    assert result["status"] == "primary_complete"
    assert result["venue_coverage"] == "1"
    assert result["position_notional_raw"] == result["indexer_oi_raw"] == "100"
    assert result["positive_oi_markets"] == 1
    assert result["market_side_sha256"] == hashlib.sha256(
        f"{market}|60|40|60|40".encode()
    ).hexdigest()


@pytest.mark.parametrize("prior_independent", [{"status": "unavailable"}, None, "complete"])
def test_independent_failure_does_not_erase_primary_receipt(
    monkeypatch, tmp_path, prior_independent
):
    day = date(2026, 9, 27)
    path = tmp_path / f"{day.isoformat()}.json"
    collector.write_json(
        path,
        {
            "status": "primary_complete",
            "oi_block": 1,
            "position_count": 2,
            "indexer_oi_raw": "100",
            "position_notional_raw": "100",
            "retrieved_at_utc": datetime(2026, 9, 27, tzinfo=UTC).isoformat(),
            "independent": prior_independent,
            "attempts": [],
        },
    )
    monkeypatch.setattr(
        collector,
        "collect_independent",
        lambda *_: {"status": "unavailable", "rpc_failures": []},
    )
    monkeypatch.setattr(
        collector,
        "collect_primary",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not refetch primary")),
    )

    result = collector.process_day(day, tmp_path)

    assert result["status"] == "primary_complete"
    assert collector.read_json(path)["status"] == "primary_complete"
    assert result["attempts"][-1]["stage"] == "independent"


def test_primary_phase_persists_before_chain_probe(monkeypatch, tmp_path):
    day = date(2026, 9, 27)
    monkeypatch.setattr(
        collector,
        "collect_primary",
        lambda *_: {
            "status": "primary_complete",
            "oi_block": 1,
            "position_count": 2,
            "indexer_oi_raw": "100",
            "position_notional_raw": "100",
            "retrieved_at_utc": datetime(2026, 9, 27, tzinfo=UTC).isoformat(),
            "graphql_request_attempts": [],
        },
    )
    monkeypatch.setattr(
        collector,
        "collect_independent",
        lambda *_: (_ for _ in ()).throw(AssertionError("chain must be deferred")),
    )

    receipt = collector.process_day(day, tmp_path, defer_independent=True)

    assert receipt["status"] == "primary_complete"
    assert receipt["independent"]["status"] == "pending"
    assert collector.read_json(tmp_path / f"{day.isoformat()}.json")["status"] == "primary_complete"


def test_ratio_has_no_float_rounding():
    assert collector.ratio(1, 2) == "0.5"
    assert collector.ratio(100, 100) == "1"


def test_final_pass_is_retracted_if_fixed_time_receipt_missing(monkeypatch, tmp_path):
    class AfterWindow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 4, 1, tzinfo=tz)

    monkeypatch.setattr(collector, "datetime", AfterWindow)
    result_dir = tmp_path / "results"
    collector.write_json(result_dir / "g5_final.json", {"primary_status": "pass"})

    collector.maybe_finalize(tmp_path / "receipts", result_dir)

    result = collector.read_json(result_dir / "g5_final.json")
    assert result["primary_status"] == "inconclusive"
    assert result["complete_receipts"] == 0


def test_collect_primary_accepts_empty_position_set_with_positive_oi(monkeypatch):
    day = collector.BASELINE
    stamp = collector.utc_midnight(day)

    def connection(field, *_args):
        if field == "positionsConnection":
            return [], 0, 1
        return [{
            "marketAddress": "0xmarket", "snapshotTimestamp": stamp,
            "blockTimestamp": stamp, "blockNumber": 123,
            "longOpenInterestUsd": "100", "shortOpenInterestUsd": "0",
        }], 1, 1

    monkeypatch.setattr(collector, "fetch_connection", connection)
    result = collector.collect_primary(day)
    assert result["venue_coverage"] == "0"
    assert result["position_count"] == 0
    assert result["position_ids_sha256"] == hashlib.sha256(b"").hexdigest()


def _window(monkeypatch, tmp_path, *, position=70, refetch_position=None,
            mismatch=None, empty_day=None, event_scans=None, statuses=None):
    class AfterWindow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 4, 1, tzinfo=tz)

    monkeypatch.setattr(collector, "datetime", AfterWindow)
    receipts = tmp_path / "receipts"
    results = tmp_path / "results"
    end_block = collector.utc_midnight(collector.END)
    status_values = iter(statuses if statuses is not None else [
        {"height": end_block + 1, "finalizedHeight": end_block}
        for _ in range(4)
    ])
    scans = iter(event_scans if event_scans is not None else [[], []])

    def status_query(query):
        assert "squidStatus" in query
        return {"squidStatus": next(status_values)}

    monkeypatch.setattr(audit, "gql", status_query)
    for offset in range(8):
        day = collector.BASELINE + collector.timedelta(days=offset)
        stamp = collector.utc_midnight(day)
        size = 0 if day == empty_day else position
        receipt = {
            "status": "primary_complete",
            "snapshot_utc": datetime.fromtimestamp(stamp, UTC).isoformat(),
            "protocol_sha256": collector.PROTOCOL_SHA256,
            "source": audit.ENDPOINT,
            "position_count": int(size > 0), "oi_market_count": 1, "oi_block": stamp,
            "positive_oi_markets": 1, "zero_oi_markets": 0,
            "position_notional_raw": str(size), "indexer_oi_raw": "100",
            "venue_coverage": str(Decimal(size) / Decimal(100)),
            "market_ratio_minimum": str(Decimal(size) / Decimal(100)),
            "position_ids_sha256": hashlib.sha256(("p" if size else "").encode()).hexdigest(),
            "independent": {
                "status": "complete",
                "rpc_url": "https://arb1.arbitrum.io/rpc",
                "probe_code_sha256": "0" * 64,
                "checks": {
                    "full_position_key_set": True,
                    "market_universe": True,
                    "oi_raw_unit_tolerance": True,
                    "sampled_position_sizes": True,
                    "count_alignment": True,
                    "primary_amount_alignment": True,
                },
                "proof": {
                    "block": stamp,
                    "block_hash": "0x" + "1" * 64,
                    "block_time_utc": datetime.fromtimestamp(stamp, UTC).isoformat(),
                    "chain_id": 42161,
                    "complete_set_read": True,
                    "onchain_only_keys": 0,
                    "indexer_only_keys": 0,
                    "indexer_only_markets": 0,
                    "chain_only_positive_oi_markets": 0,
                    "oi_max_abs_difference_raw": 0,
                    "oi_exact_sides": 2,
                    "oi_market_rows": 1,
                    "indexer_oi_market_count": 1,
                    "indexer_position_count": int(size > 0),
                    "indexer_position_size_total_raw": size,
                    "oi_total_indexer_raw": 100,
                    "position_values_sampled": 7,
                    "position_value_mismatches": 0,
                },
            },
        }
        if offset:
            receipt["market_side_sha256"] = hashlib.sha256(
                f"0xmarket|{size}|0|100|0".encode()
            ).hexdigest()
        collector.write_json(receipts / f"{day}.json", receipt)

    def connection(field, where, *_args):
        if field == "tradeActionsConnection":
            rows = next(scans)
            if isinstance(rows, Exception):
                raise rows
            return rows, len(rows), 1
        stamp = int(where.split("snapshotTimestamp_eq:")[1].split(",")[0].split("}")[0])
        day = datetime.fromtimestamp(stamp, UTC).date()
        size = 0 if day == empty_day else (
            refetch_position if refetch_position is not None else position
        )
        if field == "positionsConnection":
            rows = [] if size == 0 else [{
                "id": "p", "account": "0xaccount", "market": "0xmarket",
                "isLong": True, "sizeInUsd": str(size), "openedAt": stamp - 86400,
                "snapshotTimestamp": stamp,
            }]
            if mismatch == "unmatched":
                rows[0]["market"] = "0xother"
            if mismatch == "different_id":
                rows[0]["id"] = "q"
            if mismatch == "position_time":
                rows[0]["snapshotTimestamp"] += 1
            if mismatch == "empty_account":
                rows[0]["account"] = ""
            if mismatch == "negative_size":
                rows[0]["sizeInUsd"] = "-1"
            if mismatch == "fractional_size":
                rows[0]["sizeInUsd"] = "70.5"
            if mismatch == "duplicate_position":
                rows.append(dict(rows[0]))
            return rows, len(rows), 1
        if field == "fundingBalanceOiSnapshotsConnection":
            row = {
                "marketAddress": "0xmarket", "snapshotTimestamp": stamp,
                "blockTimestamp": stamp + (1 if mismatch == "block_time" else 0),
                "blockNumber": stamp, "longOpenInterestUsd": "100",
                "shortOpenInterestUsd": "0",
            }
            if mismatch == "negative_oi":
                row["shortOpenInterestUsd"] = "-1"
            if mismatch == "zero_oi":
                row["longOpenInterestUsd"] = "0"
            rows = [row]
            if mismatch in {"duplicate_market", "multiple_blocks"}:
                rows.append(dict(row))
                if mismatch == "multiple_blocks":
                    rows[1]["marketAddress"] = "0xsecond"
                    rows[1]["blockNumber"] += 1
            return rows, len(rows), 1
        raise AssertionError(field)

    monkeypatch.setattr(audit, "fetch_connection", connection)
    monkeypatch.setattr(collector, "fetch_connection", connection)
    return receipts, results


@pytest.mark.parametrize(
    "complete_offsets, other_status, expected_axis, expected_time",
    [
        (tuple(range(8)), "unavailable", "complete", "2026-09-28T00:00:00+00:00"),
        ((0, 2, 5), "failed_checks", "partial", "2026-09-29T00:00:00+00:00"),
        ((0,), "unavailable", "partial", "not achieved"),
        ((), "unavailable", "unavailable", "not achieved"),
        (tuple(range(7)), "pending", "partial", "2026-09-28T00:00:00+00:00"),
    ],
)
def test_independent_axis_uses_all_eight_receipts_and_primary_day_time(
    monkeypatch, tmp_path, complete_offsets, other_status, expected_axis, expected_time
):
    receipts, results = _window(monkeypatch, tmp_path)
    for offset in range(8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        if offset not in complete_offsets:
            receipt["independent"] = {
                "status": other_status,
                "checks": {"full_position_key_set": False},
                "rpc_failures": [{"error": "historical block unavailable"}],
            }
            collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    assert final["independent_chain_validation"] == expected_axis
    assert final["time_to_stable_independent_utc"] == expected_time
    for offset in range(8):
        if offset not in complete_offsets:
            path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
            assert collector.read_json(path)["independent"]["checks"] == {
                "full_position_key_set": False
            }


@pytest.mark.parametrize(
    "complete_offsets, expected_axis, expected_time",
    [
        ((0, 2), "partial", "2026-09-29T00:00:00+00:00"),
        ((0,), "partial", "not achieved"),
        ((), "unavailable", "not achieved"),
    ],
)
def test_primary_incomplete_still_reports_independent_axis(
    monkeypatch, tmp_path, complete_offsets, expected_axis, expected_time
):
    receipts, results = _window(monkeypatch, tmp_path)
    for offset in range(8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        if offset not in complete_offsets:
            receipt["independent"] = {"status": "unavailable"}
        if offset == 4:
            receipt["status"] = "primary_incomplete"
        collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["independent_chain_validation"] == expected_axis
    assert final["time_to_stable_independent_utc"] == expected_time
    assert final["time_to_stable_primary_utc"] == "2026-09-28T00:00:00+00:00"


def test_no_complete_primary_day_reports_not_achieved(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    for offset in range(1, 8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        receipt["status"] = "primary_incomplete"
        collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "not achieved"


def test_incomplete_later_receipt_refetches_earlier_days_before_stability(
    monkeypatch, tmp_path
):
    receipts, results = _window(monkeypatch, tmp_path)
    first_path = receipts / "2026-09-28.json"
    first = collector.read_json(first_path)
    first["position_count"] += 1
    collector.write_json(first_path, first)
    missing_path = receipts / "2026-09-30.json"
    missing = collector.read_json(missing_path)
    missing["status"] = "primary_incomplete"
    collector.write_json(missing_path, missing)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-29T00:00:00+00:00"
    assert any(
        item["date_utc"] == "2026-09-28"
        for item in final["partial_refetch_errors"]
    )


def test_incomplete_window_does_not_certify_unavailable_refetch(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    for offset in range(2, 8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        receipt["status"] = "primary_incomplete"
        collector.write_json(path, receipt)
    monkeypatch.setattr(
        collector, "fetch_connection",
        lambda *_: (_ for _ in ()).throw(RuntimeError("refetch unavailable")),
    )

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "not achieved"
    assert final["partial_refetch_errors"] == [
        {"date_utc": "2026-09-28", "error_type": "RuntimeError"}
    ]


@pytest.mark.parametrize(
    "bad_evidence",
    ["missing_checks", "failed_check", "missing_proof", "wrong_block", "wrong_amount"],
)
def test_complete_independent_status_requires_consistent_evidence(
    monkeypatch, tmp_path, bad_evidence
):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-28.json"
    receipt = collector.read_json(path)
    independent = receipt["independent"]
    if bad_evidence == "missing_checks":
        independent.pop("checks")
    elif bad_evidence == "failed_check":
        independent["checks"]["full_position_key_set"] = False
    elif bad_evidence == "missing_proof":
        independent.pop("proof")
    elif bad_evidence == "wrong_block":
        independent["proof"]["block"] += 1
    else:
        independent["proof"]["oi_total_indexer_raw"] += 1
    collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    assert final["independent_chain_validation"] == "partial"
    assert final["time_to_stable_independent_utc"] == "2026-09-29T00:00:00+00:00"


def test_process_day_retries_inconsistent_complete_independent_receipt(monkeypatch, tmp_path):
    receipts, _ = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-27.json"
    receipt = collector.read_json(path)
    receipt["independent"]["checks"]["full_position_key_set"] = False
    collector.write_json(path, receipt)
    attempted = []

    def retry(*_args):
        attempted.append(True)
        return {"status": "unavailable", "rpc_failures": []}

    monkeypatch.setattr(collector, "collect_independent", retry)
    result = collector.process_day(collector.BASELINE, receipts)
    assert attempted == [True]
    assert result["independent"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "offset, field, value, expected_time",
    [
        (1, "protocol_sha256", "wrong protocol", "2026-09-29T00:00:00+00:00"),
        (1, "source", "https://wrong.example", "2026-09-29T00:00:00+00:00"),
        (1, "snapshot_utc", "malformed date", "2026-09-29T00:00:00+00:00"),
        (1, "status", "primary_incomplete", "2026-09-29T00:00:00+00:00"),
        (0, "source", "https://wrong.example", "2026-09-28T00:00:00+00:00"),
    ],
)
def test_invalid_receipt_cannot_count_as_independently_validated(
    monkeypatch, tmp_path, offset, field, value, expected_time
):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
    receipt = collector.read_json(path)
    receipt[field] = value
    collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["independent_chain_validation"] == "partial"
    assert final["time_to_stable_independent_utc"] == expected_time
    assert "malformed date" not in final["time_to_stable_independent_utc"]


def test_no_valid_provenance_means_independent_unavailable(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    for offset in range(8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        receipt["source"] = "https://wrong.example"
        collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["independent_chain_validation"] == "unavailable"
    assert final["time_to_stable_independent_utc"] == "not achieved"


def test_malformed_daily_json_retracts_prior_pass(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    collector.write_json(
        results / "g5_final.json",
        {"primary_status": "pass", "independent_chain_validation": "complete"},
    )
    (receipts / "2026-09-29.json").write_text("{broken", encoding="utf-8")

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["independent_chain_validation"] == "partial"
    assert final["complete_receipts"] == 7
    assert final["receipt_load_errors"][0]["date_utc"] == "2026-09-29"
    assert "broken" not in json.dumps(final)


def test_default_all_phase_retracts_pass_after_receipt_parse_failure(
    monkeypatch, tmp_path, capsys
):
    receipts, results = _window(monkeypatch, tmp_path)
    # This test exercises finalization, not the byte-level protocol hash; a
    # Windows Git checkout may translate the frozen Markdown's line endings.
    monkeypatch.setattr(collector, "check_protocol", lambda: None)
    collector.write_json(results / "g5_final.json", {"primary_status": "pass"})
    broken_path = receipts / "2026-09-29.json"
    broken_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        collector.sys, "argv",
        ["collect_gmx_g5_window", "--output-dir", str(receipts), "--result-dir", str(results)],
    )

    collector.main()

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["receipt_load_errors"][0]["date_utc"] == "2026-09-29"
    assert broken_path.read_text(encoding="utf-8") == "{broken"
    assert json.loads(capsys.readouterr().out)["daily_statuses"]["2026-09-29"] == "corrupt_receipt"


@pytest.mark.parametrize(
    "broken_bytes, error_type",
    [
        (b"{broken", "JSONDecodeError"),
        (b"\xff", "UnicodeDecodeError"),
        (b"[]", "InvalidReceiptShape"),
        (b"null", "InvalidReceiptShape"),
        (b"42", "InvalidReceiptShape"),
        (b'"bad"', "InvalidReceiptShape"),
    ],
)
def test_corrupt_prior_receipt_does_not_starve_later_due_day(
    monkeypatch, tmp_path, capsys, broken_bytes, error_type
):
    receipts, results = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(collector, "check_protocol", lambda: None)
    broken_path = receipts / "2026-09-29.json"
    broken_path.write_bytes(broken_bytes)
    later_path = receipts / "2026-09-30.json"
    later = collector.read_json(later_path)
    later["status"] = "primary_incomplete"
    collector.write_json(later_path, later)
    monkeypatch.setattr(
        collector, "collect_independent", lambda *_: {"status": "unavailable"}
    )
    monkeypatch.setattr(
        collector.sys, "argv",
        ["collect_gmx_g5_window", "--output-dir", str(receipts), "--result-dir", str(results)],
    )

    collector.main()

    output = capsys.readouterr().out
    assert "{broken" not in output
    summary = json.loads(output)
    statuses = summary["daily_statuses"]
    assert statuses["2026-09-29"] == "corrupt_receipt"
    assert statuses["2026-09-30"] == "primary_complete"
    assert collector.read_json(later_path)["status"] == "primary_complete"
    assert broken_path.read_bytes() == broken_bytes
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["receipt_load_errors"] == [
        {"date_utc": "2026-09-29", "error_type": error_type}
    ]
    assert summary["receipt_load_errors"] == final["receipt_load_errors"]


@pytest.mark.parametrize(
    "broken_bytes, error_type",
    [(b"{broken", "JSONDecodeError"), (b"[]", "InvalidReceiptShape")],
)
def test_independent_phase_continues_after_corrupt_receipt(
    monkeypatch, tmp_path, capsys, broken_bytes, error_type
):
    receipts, results = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(collector, "check_protocol", lambda: None)
    broken_path = receipts / "2026-09-29.json"
    broken_path.write_bytes(broken_bytes)
    later_path = receipts / "2026-09-30.json"
    later = collector.read_json(later_path)
    later["independent"] = {"status": "pending"}
    collector.write_json(later_path, later)
    checked = []

    def independent(day, *_args):
        checked.append(day)
        return {"status": "unavailable"}

    monkeypatch.setattr(collector, "collect_independent", independent)
    monkeypatch.setattr(
        collector.sys, "argv",
        ["collect_gmx_g5_window", "--output-dir", str(receipts),
         "--result-dir", str(results), "--phase", "independent"],
    )

    collector.main()

    summary = json.loads(capsys.readouterr().out)
    assert summary["daily_statuses"]["2026-09-29"] == "corrupt_receipt"
    assert date(2026, 9, 30) in checked
    assert collector.read_json(later_path)["status"] == "primary_complete"
    assert broken_path.read_bytes() == broken_bytes
    assert summary["receipt_load_errors"] == [
        {"date_utc": "2026-09-29", "error_type": error_type}
    ]


def test_finalize_phase_reports_non_object_receipt_in_status(monkeypatch, tmp_path, capsys):
    receipts, results = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(collector, "check_protocol", lambda: None)
    broken_path = receipts / "2026-09-29.json"
    broken_path.write_bytes(b"null")
    monkeypatch.setattr(
        collector.sys, "argv",
        ["collect_gmx_g5_window", "--output-dir", str(receipts),
         "--result-dir", str(results), "--phase", "finalize"],
    )

    collector.main()

    summary = json.loads(capsys.readouterr().out)
    assert summary["daily_statuses"]["2026-09-29"] == "corrupt_receipt"
    assert summary["receipt_load_errors"] == [
        {"date_utc": "2026-09-29", "error_type": "InvalidReceiptShape"}
    ]
    assert broken_path.read_bytes() == b"null"
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["receipt_load_errors"] == summary["receipt_load_errors"]


@pytest.mark.parametrize("bad_independent", [None, "complete"])
def test_malformed_independent_state_cannot_preserve_prior_complete_axis(
    monkeypatch, tmp_path, bad_independent
):
    receipts, results = _window(monkeypatch, tmp_path)
    collector.write_json(
        results / "g5_final.json",
        {"primary_status": "pass", "independent_chain_validation": "complete"},
    )
    path = receipts / "2026-09-28.json"
    receipt = collector.read_json(path)
    receipt["independent"] = bad_independent
    collector.write_json(path, receipt)

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    assert final["independent_chain_validation"] == "partial"
    assert final["time_to_stable_independent_utc"] == "2026-09-29T00:00:00+00:00"


def test_complete_window_reconciles_and_passes(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    assert len(final["metrics"]["daily"]) == 7
    assert all("account" not in str(day) for day in final["metrics"]["daily"])


def test_changed_refetch_is_inconclusive(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path, refetch_position=71)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["independent_chain_validation"] == "complete"
    assert final["time_to_stable_independent_utc"] == "2026-09-28T00:00:00+00:00"
    assert final["time_to_stable_primary_utc"] == "not achieved"
    assert "mismatch" in final["reason"].lower()
    assert len(final["reason"]) <= 500


def test_later_verified_day_sets_stability_after_earlier_receipt_mismatch(
    monkeypatch, tmp_path
):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-28.json"
    receipt = collector.read_json(path)
    receipt["position_count"] += 1
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-29T00:00:00+00:00", final
    assert "mismatch" in final["reason"]


def test_late_daily_scan_failure_preserves_earlier_verified_stability(
    monkeypatch, tmp_path
):
    receipts, results = _window(monkeypatch, tmp_path)
    first_path = receipts / "2026-09-28.json"
    first_receipt = collector.read_json(first_path)
    first_receipt["position_count"] += 1
    collector.write_json(first_path, first_receipt)
    original = audit.fetch_connection
    end_stamp = collector.utc_midnight(collector.END)

    def fail_end_day(field, where, *args):
        if field == "positionsConnection" and f"snapshotTimestamp_eq:{end_stamp}" in where:
            raise RuntimeError("Oct 4 pagination incomplete")
        return original(field, where, *args)

    monkeypatch.setattr(audit, "fetch_connection", fail_end_day)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-29T00:00:00+00:00"
    assert "pagination" in final["reason"]
    assert "mismatch" in final["reason"]


def test_early_daily_scan_failure_can_find_later_verified_day(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    original = audit.fetch_connection
    first_stamp = collector.utc_midnight(collector.BASELINE + collector.timedelta(days=1))

    def fail_first_day(field, where, *args):
        if field == "positionsConnection" and f"snapshotTimestamp_eq:{first_stamp}" in where:
            raise RuntimeError("Sep 28 pagination incomplete")
        return original(field, where, *args)

    monkeypatch.setattr(audit, "fetch_connection", fail_first_day)
    monkeypatch.setattr(collector, "fetch_connection", fail_first_day)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-29T00:00:00+00:00"
    assert "pagination" in final["reason"]


def test_same_totals_with_changed_position_id_is_inconclusive(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path, mismatch="different_id")
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "position_ids_sha256" in final["reason"]


def test_zero_position_day_reconciles_with_zero_coverage(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path, empty_day=date(2026, 9, 29))
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    assert final["metrics"]["daily"][1]["venue_coverage"] == 0


@pytest.mark.parametrize("mismatch", [
    "unmatched", "position_time", "block_time", "empty_account",
    "negative_size", "duplicate_position", "negative_oi", "zero_oi",
    "duplicate_market", "multiple_blocks", "fractional_size",
])
def test_invalid_refetch_is_inconclusive(monkeypatch, tmp_path, mismatch):
    receipts, results = _window(monkeypatch, tmp_path, mismatch=mismatch)
    collector.maybe_finalize(receipts, results)
    assert collector.read_json(results / "g5_final.json")["primary_status"] == "inconclusive"


def test_previous_pass_is_retracted_after_refetch_drift(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path, refetch_position=71)
    collector.write_json(results / "g5_final.json", {"primary_status": "pass"})
    collector.maybe_finalize(receipts, results)
    assert collector.read_json(results / "g5_final.json")["primary_status"] == "inconclusive"


def test_wrong_receipt_date_is_inconclusive(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-29.json"
    receipt = collector.read_json(path)
    receipt["snapshot_utc"] = "2026-09-30T00:00:00+00:00"
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    assert collector.read_json(results / "g5_final.json")["primary_status"] == "inconclusive"


def test_wrong_first_receipt_date_still_finds_later_stable_day(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-28.json"
    receipt = collector.read_json(path)
    receipt["snapshot_utc"] = "2026-09-30T00:00:00+00:00"
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-29T00:00:00+00:00"
    assert "fixed date mismatch" in final["reason"]


def test_invalid_baseline_provenance_keeps_later_stability_but_not_pass(
    monkeypatch, tmp_path
):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-27.json"
    receipt = collector.read_json(path)
    receipt["protocol_sha256"] = "wrong"
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-28T00:00:00+00:00"
    assert "protocol_sha256" in final["reason"]


@pytest.mark.parametrize("field,value", [
    ("protocol_sha256", None), ("protocol_sha256", "wrong"),
    ("source", None), ("source", "https://other.invalid/graphql"),
])
@pytest.mark.parametrize("receipt_day", [date(2026, 9, 27), date(2026, 9, 29)])
def test_all_receipts_require_frozen_provenance(
    monkeypatch, tmp_path, field, value, receipt_day
):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / f"{receipt_day}.json"
    receipt = collector.read_json(path)
    if value is None:
        receipt.pop(field)
    else:
        receipt[field] = value
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert field in final["reason"]


@pytest.mark.parametrize("field,value", [
    ("venue_coverage", "0.71"), ("market_ratio_minimum", "0.69"),
])
def test_refetch_reconciles_existing_coverage_results(monkeypatch, tmp_path, field, value):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-29.json"
    receipt = collector.read_json(path)
    receipt[field] = value
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-28T00:00:00+00:00"
    assert field in final["reason"]


@pytest.mark.parametrize("digest_present", [True, False])
def test_market_redistribution_with_same_totals_ids_and_minimum(
    monkeypatch, tmp_path, digest_present
):
    receipts, results = _window(monkeypatch, tmp_path)
    original_digest = hashlib.sha256(
        b"0xa|20|0|100|0\n0xb|60|0|100|0\n0xc|60|0|100|0"
    ).hexdigest()
    with localcontext() as context:
        context.prec = 80
        coverage = str(Decimal(140) / Decimal(300))
    for offset in range(8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        receipt.update({
            "position_count": 3, "oi_market_count": 3, "positive_oi_markets": 3,
            "position_notional_raw": "140", "indexer_oi_raw": "300",
            "venue_coverage": coverage,
            "market_ratio_minimum": "0.2",
            "position_ids_sha256": hashlib.sha256(b"a\nb\nc").hexdigest(),
        })
        if digest_present and offset:
            receipt["market_side_sha256"] = original_digest
        elif offset:
            receipt.pop("market_side_sha256")
        collector.write_json(path, receipt)

    def redistributed(field, where, *_args):
        if field == "tradeActionsConnection":
            return [], 0, 1
        stamp = int(where.split("snapshotTimestamp_eq:")[1].split(",")[0].split("}")[0])
        if field == "positionsConnection":
            return [
                {
                    "id": identifier, "account": "0xaccount", "market": market,
                    "isLong": True, "sizeInUsd": str(size), "openedAt": stamp - 86400,
                    "snapshotTimestamp": stamp,
                }
                for identifier, market, size in zip(
                    "abc", ("0xa", "0xb", "0xc"), (20, 50, 70), strict=True
                )
            ], 3, 1
        return [
            {
                "marketAddress": market, "snapshotTimestamp": stamp,
                "blockTimestamp": stamp, "blockNumber": stamp,
                "longOpenInterestUsd": "100", "shortOpenInterestUsd": "0",
            }
            for market in ("0xa", "0xb", "0xc")
        ], 3, 1

    monkeypatch.setattr(audit, "fetch_connection", redistributed)
    collector.write_json(results / "g5_final.json", {"primary_status": "pass"})
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    if digest_present:
        assert final["primary_status"] == "inconclusive"
        assert "market_side_sha256" in final["reason"]
    else:
        assert final["primary_status"] == "inconclusive"
        assert "market_side_sha256" in final["reason"]


def test_baseline_without_digest_uses_exact_primary_ratios(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path, position=1)
    with localcontext() as context:
        context.prec = 80
        exact_third = str(Decimal(1) / Decimal(3))
    for offset in range(8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        receipt["indexer_oi_raw"] = "3"
        receipt["venue_coverage"] = exact_third
        receipt["market_ratio_minimum"] = exact_third
        if offset:
            receipt["market_side_sha256"] = hashlib.sha256(
                b"0xmarket|1|0|3|0"
            ).hexdigest()
        else:
            assert "market_side_sha256" not in receipt
        collector.write_json(path, receipt)

    original = audit.fetch_connection

    def third_oi(field, where, *args):
        rows, count, pages = original(field, where, *args)
        if field == "fundingBalanceOiSnapshotsConnection":
            rows[0]["longOpenInterestUsd"] = "3"
        return rows, count, pages

    monkeypatch.setattr(audit, "fetch_connection", third_oi)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "not_pass"


def test_missing_primary_digest_is_inconclusive(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    path = receipts / "2026-09-29.json"
    receipt = collector.read_json(path)
    receipt.pop("market_side_sha256")
    collector.write_json(path, receipt)
    collector.write_json(results / "g5_final.json", {"primary_status": "pass"})

    collector.maybe_finalize(receipts, results)

    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "market_side_sha256" in final["reason"]


@pytest.mark.parametrize("first_status", [
    None,
    {"height": 10},
    {"height": "11", "finalizedHeight": "10"},
])
def test_missing_or_malformed_squid_status_is_inconclusive(monkeypatch, tmp_path, first_status):
    receipts, results = _window(
        monkeypatch, tmp_path, statuses=[first_status] * 4
    )
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "squidStatus" in final["reason"]


def test_finalized_height_below_end_oi_block_retracts_pass(monkeypatch, tmp_path):
    end_block = collector.utc_midnight(collector.END)
    low = {"height": end_block + 1, "finalizedHeight": end_block - 1}
    receipts, results = _window(monkeypatch, tmp_path, statuses=[low] * 4)
    collector.write_json(results / "g5_final.json", {"primary_status": "pass"})
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "finalizedHeight" in final["reason"]


def test_equal_finalized_height_and_empty_interval_pass(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    event_check = final["metrics"]["event_source_internal_reconciliation"]
    assert event_check["trade_action_count"] == 0
    assert event_check["trade_action_sha256"] == hashlib.sha256(b"[]").hexdigest()
    assert len(event_check["status_checks"]) == 4
    assert event_check["scan_pages"] == [1, 1]


def test_squid_status_regression_after_event_scan_is_inconclusive(monkeypatch, tmp_path):
    end_block = collector.utc_midnight(collector.END)
    high = {"height": end_block + 2, "finalizedHeight": end_block + 1}
    low = {"height": end_block + 2, "finalizedHeight": end_block}
    receipts, results = _window(monkeypatch, tmp_path, statuses=[high, low, high, high])
    collector.maybe_finalize(receipts, results)
    assert collector.read_json(results / "g5_final.json")["primary_status"] == "inconclusive"


@pytest.mark.parametrize("change", ["id", "account", "timestamp", "count"])
def test_changed_second_event_scan_is_inconclusive(monkeypatch, tmp_path, change):
    start = collector.utc_midnight(collector.BASELINE)
    first = [{"id": "t1", "account": "0xtrade", "timestamp": start + 1}]
    second = [dict(first[0])]
    if change == "count":
        second.append({"id": "t2", "account": "0xtrade", "timestamp": start + 2})
    else:
        second[0][change] = {"id": "t2", "account": "0xother", "timestamp": start + 2}[change]
    receipts, results = _window(monkeypatch, tmp_path, event_scans=[first, second])
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "TradeAction" in final["reason"]


@pytest.mark.parametrize("bad_rows", [
    [{"id": "t1", "account": "0xtrade", "timestamp": 0},
     {"id": "t1", "account": "0xtrade", "timestamp": 1}],
    [{"id": "t1", "account": None, "timestamp": 0}],
    [{"id": "t1", "account": "0xtrade", "timestamp": 0}],
])
def test_bad_event_rows_are_inconclusive(monkeypatch, tmp_path, bad_rows):
    start = collector.utc_midnight(collector.BASELINE)
    rows = [dict(row, timestamp=start + row["timestamp"]) for row in bad_rows]
    if len(rows) == 1 and rows[0]["account"] is not None:
        rows[0]["timestamp"] = collector.utc_midnight(collector.END)
    receipts, results = _window(monkeypatch, tmp_path, event_scans=[rows, rows])
    collector.maybe_finalize(receipts, results)
    assert collector.read_json(results / "g5_final.json")["primary_status"] == "inconclusive"


def test_failed_second_event_pagination_is_inconclusive(monkeypatch, tmp_path):
    receipts, results = _window(
        monkeypatch, tmp_path, event_scans=[[], RuntimeError("TradeAction pagination incomplete")]
    )
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "pagination" in final["reason"]


def test_matching_nonempty_event_scans_retain_only_aggregate_proof(monkeypatch, tmp_path):
    start = collector.utc_midnight(collector.BASELINE)
    rows = [{"id": "t1", "account": "0xtrade", "timestamp": start + 1}]
    receipts, results = _window(monkeypatch, tmp_path, event_scans=[rows, rows])
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    proof = final["metrics"]["event_source_internal_reconciliation"]
    expected = hashlib.sha256(
        json.dumps([["t1", "0xtrade", start + 1]], separators=(",", ":")).encode()
    ).hexdigest()
    assert proof["trade_action_sha256"] == expected
    assert proof["trade_action_count"] == 1
    assert "0xtrade" not in json.dumps(final)


def test_final_result_retains_only_current_run_request_metadata(monkeypatch, tmp_path):
    receipts, results = _window(monkeypatch, tmp_path)
    monkeypatch.setattr(audit, "REQUEST_LOG", [{"started_at_utc": "prior run"}])
    original_gql = audit.gql

    def logged_status(query):
        audit.REQUEST_LOG.append({"started_at_utc": "current run", "outcome": "success"})
        return original_gql(query)

    monkeypatch.setattr(audit, "gql", logged_status)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    attempts = final["metrics"]["graphql_request_attempts"]
    assert len(attempts) == 4
    assert all(item["started_at_utc"] == "current run" for item in attempts)


def test_failed_event_scan_retains_current_run_request_metadata(monkeypatch, tmp_path):
    receipts, results = _window(
        monkeypatch, tmp_path,
        event_scans=[[], RuntimeError("TradeAction pagination incomplete")],
    )
    request_log = [{"started_at_utc": "prior run"}]
    monkeypatch.setattr(audit, "REQUEST_LOG", request_log)
    monkeypatch.setattr(collector, "REQUEST_LOG", request_log)
    original_gql = audit.gql

    def logged_status(query):
        audit.REQUEST_LOG.append({"started_at_utc": "current run", "outcome": "success"})
        return original_gql(query)

    monkeypatch.setattr(audit, "gql", logged_status)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert "pagination" in final["reason"]
    assert final["time_to_stable_primary_utc"] == "2026-09-28T00:00:00+00:00"
    attempts = final["graphql_request_attempts"]
    assert len(attempts) == 3
    assert all(item["started_at_utc"] == "current run" for item in attempts)


def test_failed_event_scan_does_not_certify_mismatched_first_day(monkeypatch, tmp_path):
    receipts, results = _window(
        monkeypatch, tmp_path,
        refetch_position=71,
        event_scans=[[], RuntimeError("TradeAction pagination incomplete")],
    )
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "not achieved"
    assert "pagination" in final["reason"]
    assert "mismatch" in final["reason"]


def test_failed_event_scan_preserves_later_verified_primary_day(monkeypatch, tmp_path):
    receipts, results = _window(
        monkeypatch, tmp_path,
        event_scans=[[], RuntimeError("TradeAction pagination incomplete")],
    )
    path = receipts / "2026-09-28.json"
    receipt = collector.read_json(path)
    receipt["position_count"] += 1
    collector.write_json(path, receipt)
    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "inconclusive"
    assert final["time_to_stable_primary_utc"] == "2026-09-29T00:00:00+00:00"
    assert "pagination" in final["reason"]
    assert "mismatch" in final["reason"]


def test_success_preserves_prior_failed_finalization_request_history(monkeypatch, tmp_path):
    end_block = collector.utc_midnight(collector.END)
    statuses = [
        {"height": end_block + 1, "finalizedHeight": end_block}
        for _ in range(7)
    ]
    receipts, results = _window(
        monkeypatch, tmp_path,
        event_scans=[[], RuntimeError("TradeAction pagination incomplete"), [], []],
        statuses=statuses,
    )
    request_log = []
    monkeypatch.setattr(audit, "REQUEST_LOG", request_log)
    monkeypatch.setattr(collector, "REQUEST_LOG", request_log)
    original_gql = audit.gql

    def logged_status(query):
        request_log.append({"started_at_utc": "event status", "outcome": "success"})
        return original_gql(query)

    monkeypatch.setattr(audit, "gql", logged_status)
    collector.maybe_finalize(receipts, results)
    first = collector.read_json(results / "g5_final.json")
    assert first["primary_status"] == "inconclusive"
    assert len(first["finalization_attempts"]) == 1
    assert len(first["finalization_attempts"][0]["graphql_request_attempts"]) == 3

    collector.maybe_finalize(receipts, results)
    final = collector.read_json(results / "g5_final.json")
    assert final["primary_status"] == "pass"
    assert [item["status"] for item in final["finalization_attempts"]] == [
        "inconclusive", "pass",
    ]
    assert len(final["finalization_attempts"][0]["graphql_request_attempts"]) == 3
    assert len(final["finalization_attempts"][1]["graphql_request_attempts"]) == 4


def test_exact_threshold_rejects_float_round_up(monkeypatch, tmp_path):
    receipts, results = _window(
        monkeypatch, tmp_path, position=7 * 10**19 - 1,
        refetch_position=7 * 10**19 - 1,
    )
    for offset in range(8):
        path = receipts / f"{collector.BASELINE + collector.timedelta(days=offset)}.json"
        receipt = collector.read_json(path)
        receipt["indexer_oi_raw"] = str(10**20)
        exact_ratio = str(Decimal(7 * 10**19 - 1) / Decimal(10**20))
        receipt["venue_coverage"] = exact_ratio
        receipt["market_ratio_minimum"] = exact_ratio
        if offset:
            receipt["market_side_sha256"] = hashlib.sha256(
                f"0xmarket|{7 * 10**19 - 1}|0|{10**20}|0".encode()
            ).hexdigest()
        collector.write_json(path, receipt)

    original = audit.fetch_connection

    def large_oi(field, where, *args):
        rows, count, pages = original(field, where, *args)
        if field == "fundingBalanceOiSnapshotsConnection":
            rows[0]["longOpenInterestUsd"] = str(10**20)
        return rows, count, pages

    monkeypatch.setattr(audit, "fetch_connection", large_oi)
    collector.maybe_finalize(receipts, results)
    assert collector.read_json(results / "g5_final.json")["primary_status"] == "not_pass"
