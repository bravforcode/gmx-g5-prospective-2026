"""Offline contract tests for the optional same-indexer binding check."""

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from scripts import check_gmx_g5_supplemental_binding as binding
from scripts import collect_gmx_g5_window as collector

DAY = date(2026, 9, 28)
STAMP = 1790553600
IDS = [f"0x{'11' * 32}:{STAMP}", f"0x{'22' * 32}:{STAMP}"]
ID_DIGEST = "ab3fa378fc1d652de48d7e34c8d268994dd458f1e97c3c8542a46253375e6689"
KEY_DIGEST = "5189c77d29fe5d546a045ec46986852785fea5c13ac7da9c115ff5fb6edf817c"


def fixture():
    snapshot = datetime.fromtimestamp(STAMP, UTC).isoformat()
    observed = {
        "snapshot_utc": snapshot,
        "position_count": 2,
        "oi_market_count": 1,
        "oi_block": 42,
        "positive_oi_markets": 1,
        "zero_oi_markets": 0,
        "position_notional_raw": "100",
        "indexer_oi_raw": "100",
        "position_ids_sha256": ID_DIGEST,
        "venue_coverage": "1",
        "market_ratio_minimum": "1",
        "market_side_sha256": "a" * 64,
    }
    receipt = {
        **observed,
        "protocol_sha256": collector.PROTOCOL_SHA256,
        "source": collector.ENDPOINT,
        "status": "primary_complete",
        "independent": {
            "checked_at_utc": "2026-09-28T00:09:00+00:00",
            "checks": {
                "full_position_key_set": True,
                "market_universe": True,
                "oi_raw_unit_tolerance": True,
                "sampled_position_sizes": True,
                "count_alignment": True,
                "primary_amount_alignment": True,
            },
            "prior_rpc_failures": [],
            "probe_code_sha256": "c" * 64,
            "rpc_url": "https://arb1.arbitrum.io/rpc",
            "status": "complete",
            "proof": {
                "block": 42,
                "block_hash": "0x" + "aa" * 32,
                "block_time_utc": snapshot,
                "chain_id": 42161,
                "chain_market_count": 1,
                "chain_market_set_sha256": "d" * 64,
                "chain_only_markets": 0,
                "chain_only_oi_raw": 0,
                "chain_only_positive_oi_markets": 0,
                "complete_set_read": True,
                "indexer_distinct_key_count": 2,
                "indexer_key_set_sha256": KEY_DIGEST,
                "indexer_market_set_sha256": "d" * 64,
                "indexer_oi_market_count": 1,
                "indexer_oi_market_pages": 1,
                "indexer_only_keys": 0,
                "indexer_only_markets": 0,
                "indexer_pages": 1,
                "indexer_position_count": 2,
                "indexer_position_size_total_raw": 100,
                "key_set_sha256": KEY_DIGEST,
                "keys_read": 2,
                "oi_exact_markets": 1,
                "oi_exact_sides": 2,
                "oi_indexer_pages": 1,
                "oi_market_rows": 1,
                "oi_max_abs_difference_raw": 0,
                "oi_positive_indexer_markets": 1,
                "oi_sum_abs_difference_raw": 0,
                "oi_total_indexer_raw": 100,
                "oi_total_onchain_raw": 100,
                "onchain_only_keys": 0,
                "onchain_position_count": 2,
                "position_value_mismatches": 0,
                "position_values_sampled": 7,
                "rpc_requests": 1,
            },
        },
    }
    rows = [{"id": value, "sizeInUsd": "50"} for value in IDS]
    return observed, receipt, rows


def test_matching_primary_and_key_bytes_pass(monkeypatch):
    observed, receipt, rows = fixture()
    before = repr(receipt)
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)

    def fetch(field, where, order, fields, page_size):
        assert (field, order, fields, page_size) == (
            "positionsConnection", "id_ASC", "id sizeInUsd", 1000
        )
        assert f"snapshotTimestamp_eq:{STAMP}" in where
        return rows, 2, 1

    monkeypatch.setattr(binding, "fetch_connection", fetch)
    result = binding.check_binding(DAY, receipt)

    assert result["position_count"] == 2
    assert result["position_ids_sha256"] == ID_DIGEST
    assert result["indexer_key_set_sha256"] == KEY_DIGEST
    assert repr(receipt) == before


def test_abbreviated_independent_proof_is_rejected(monkeypatch):
    observed, receipt, rows = fixture()
    receipt["independent"] = {
        "status": "complete",
        "proof": {"indexer_key_set_sha256": KEY_DIGEST},
    }
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 2, 1))

    with pytest.raises(ValueError, match="independent proof"):
        binding.check_binding(DAY, receipt)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("key_set", "independent key digest mismatch"),
        ("market_side", "market_side_sha256"),
        ("timestamp", "wrong-time position ID"),
        ("duplicate", "duplicate position keys"),
        ("malformed", "malformed or wrong-time position ID"),
        ("missing_proof", "independent key digest"),
        ("malformed_proof", "independent key digest"),
        ("incomplete_proof", "independent proof is not complete"),
        ("incomplete_primary", "primary receipt is not complete"),
    ],
)
def test_binding_rejects_mismatches(monkeypatch, change, expected):
    observed, receipt, rows = fixture()
    if change == "key_set":
        rows[1] = {"id": f"0x{'33' * 32}:{STAMP}", "sizeInUsd": "50"}
        replacement_digest = hashlib.sha256("\n".join([IDS[0], rows[1]["id"]]).encode()).hexdigest()
        observed["position_ids_sha256"] = replacement_digest
        receipt["position_ids_sha256"] = replacement_digest
    elif change == "market_side":
        observed["market_side_sha256"] = "b" * 64
    elif change == "timestamp":
        rows[1] = {"id": f"0x{'22' * 32}:{STAMP + 1}", "sizeInUsd": "50"}
    elif change == "duplicate":
        rows[1] = rows[0].copy()
    elif change == "malformed":
        rows[1] = {"id": f"0x22:{STAMP}", "sizeInUsd": "50"}
    elif change == "incomplete_proof":
        receipt["independent"]["status"] = "unavailable"
    elif change == "incomplete_primary":
        receipt["status"] = "primary_incomplete"
    elif change == "malformed_proof":
        receipt["independent"]["proof"]["indexer_key_set_sha256"] = "not-a-digest"
    else:
        receipt["independent"] = {}
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 2, 1))

    with pytest.raises((ValueError, RuntimeError), match=expected):
        binding.check_binding(DAY, receipt)


def test_cli_emits_only_aggregate_status_and_preserves_receipt(monkeypatch, tmp_path, capsys):
    observed, receipt, rows = fixture()
    path = tmp_path / "receipt.json"
    original = json.dumps(receipt).encode()
    path.write_bytes(original)
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 2, 1))

    assert binding.main(["--day", DAY.isoformat(), "--receipt", str(path)]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "pass"
    assert result["receipt_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["date_utc"] == DAY.isoformat()
    assert datetime.fromisoformat(result["checked_at_utc"]).tzinfo == UTC
    assert all(position_id not in output.out for position_id in IDS)
    assert "0x" not in output.out
    assert path.read_bytes() == original


@pytest.mark.parametrize("receipt_bytes", [b"{", b"[]", b"{}"])
def test_cli_rejects_invalid_receipt_without_traceback(
    monkeypatch, tmp_path, capsys, receipt_bytes
):
    path = tmp_path / "receipt.json"
    path.write_bytes(receipt_bytes)
    monkeypatch.setattr(
        binding, "collect_primary", lambda *_: pytest.fail("invalid receipt was queried")
    )

    assert binding.main(["--day", DAY.isoformat(), "--receipt", str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("binding check failed:")
    assert "Traceback" not in output.err
    assert path.read_bytes() == receipt_bytes


def test_cli_rejects_wrong_receipt_date_before_refetch(monkeypatch, tmp_path, capsys):
    _, receipt, _ = fixture()
    receipt["snapshot_utc"] = "2026-09-27T00:00:00+00:00"
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(
        binding, "collect_primary", lambda *_: pytest.fail("wrong date was queried")
    )

    assert binding.main(["--day", DAY.isoformat(), "--receipt", str(path)]) == 1
    assert capsys.readouterr().out == ""
