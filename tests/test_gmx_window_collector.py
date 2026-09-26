"""Offline tests for the frozen-window collection gates."""

from datetime import UTC, date, datetime

from scripts import collect_gmx_g5_window as collector


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


def test_independent_failure_does_not_erase_primary_receipt(monkeypatch, tmp_path):
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
            "independent": {"status": "unavailable"},
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
