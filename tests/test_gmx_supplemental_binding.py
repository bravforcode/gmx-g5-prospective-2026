"""Offline contract tests for the optional same-indexer binding check."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from scripts import check_gmx_g5_supplemental_binding as binding
from scripts import collect_gmx_g5_window as collector

DAY = date(2026, 9, 28)
STAMP = 1790553600
IDS = [f"0x{byte * 32}:{STAMP}" for byte in ("11", "22", "33", "44", "55", "66", "77")]
ID_DIGEST = "870747825c7beee3861ada7322f1000cfc31a1696eeec1001b3a71c0260169f4"
KEY_DIGEST = "e74d7920d532bf63b923118f49f7518f696bede012107f67ab9d30bcf3e1a6b2"


def fixture():
    snapshot = datetime.fromtimestamp(STAMP, UTC).isoformat()
    observed = {
        "snapshot_utc": snapshot,
        "position_count": 7,
        "oi_market_count": 1,
        "oi_block": 42,
        "positive_oi_markets": 1,
        "zero_oi_markets": 0,
        "position_notional_raw": "70",
        "indexer_oi_raw": "70",
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
                "indexer_distinct_key_count": 7,
                "indexer_key_set_sha256": KEY_DIGEST,
                "indexer_market_set_sha256": "d" * 64,
                "indexer_oi_market_count": 1,
                "indexer_oi_market_pages": 1,
                "indexer_only_keys": 0,
                "indexer_only_markets": 0,
                "indexer_pages": 1,
                "indexer_position_count": 7,
                "indexer_position_size_total_raw": 70,
                "key_set_sha256": KEY_DIGEST,
                "keys_read": 7,
                "oi_exact_markets": 1,
                "oi_exact_sides": 2,
                "oi_indexer_pages": 1,
                "oi_market_rows": 1,
                "oi_max_abs_difference_raw": 0,
                "oi_positive_indexer_markets": 1,
                "oi_sum_abs_difference_raw": 0,
                "oi_total_indexer_raw": 70,
                "oi_total_onchain_raw": 70,
                "onchain_only_keys": 0,
                "onchain_position_count": 7,
                "position_value_mismatches": 0,
                "position_values_sampled": 7,
                "rpc_requests": 1,
            },
        },
    }
    rows = [{"id": value, "sizeInUsd": "10"} for value in IDS]
    return observed, receipt, rows


def test_committed_sep28_receipt_has_complete_public_proof_and_coverage():
    path = Path(__file__).resolve().parents[1] / "receipts" / "2026-09-28.json"
    receipt = json.loads(path.read_bytes())
    proof = receipt["independent"]["proof"]

    assert collector.independently_validated(DAY, receipt)
    assert receipt["position_count"] == 3701
    assert proof["position_values_sampled"] == 7
    assert proof["keys_read"] == proof["onchain_position_count"]
    assert proof["indexer_position_count"] == proof["indexer_distinct_key_count"]
    assert proof["indexer_distinct_key_count"] == receipt["position_count"]
    assert proof["key_set_sha256"] == proof["indexer_key_set_sha256"]
    assert len(bytes.fromhex(proof["key_set_sha256"])) == 32
    with localcontext() as context:
        context.prec = 80
        assert Decimal(receipt["venue_coverage"]) == (
            Decimal(receipt["position_notional_raw"])
            / Decimal(receipt["indexer_oi_raw"])
        )
    assert Decimal(receipt["market_ratio_minimum"]) > 0


def test_matching_primary_and_key_bytes_pass(monkeypatch):
    observed, receipt, rows = fixture()
    before = repr(receipt)
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)

    def fetch(field, where, order, fields, page_size):
        assert (field, order, fields, page_size) == (
            "positionsConnection", "id_ASC", "id sizeInUsd", 1000
        )
        assert f"snapshotTimestamp_eq:{STAMP}" in where
        return rows, 7, 1

    monkeypatch.setattr(binding, "fetch_connection", fetch)
    result = binding.check_binding(DAY, receipt)

    assert result["position_count"] == 7
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
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 7, 1))

    with pytest.raises(ValueError, match="independent proof"):
        binding.check_binding(DAY, receipt)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("missing_chain_digest", "on-chain key digest"),
        ("different_chain_digest", "on-chain key digest"),
        ("short_chain_digest", "on-chain key digest"),
        ("wrong_keys_read", "proof position count"),
        ("wrong_onchain_count", "proof position count"),
        ("wrong_indexer_count", "frozen criteria"),
        ("wrong_distinct_count", "proof position count"),
    ],
)
def test_independent_key_proof_must_match_digest_and_counts(
    monkeypatch, change, expected
):
    observed, receipt, rows = fixture()
    proof = receipt["independent"]["proof"]
    if change == "missing_chain_digest":
        proof.pop("key_set_sha256")
    elif change == "different_chain_digest":
        proof["key_set_sha256"] = "0" * 64
    elif change == "short_chain_digest":
        proof["key_set_sha256"] = KEY_DIGEST[:-1]
    elif change == "wrong_keys_read":
        proof["keys_read"] = 1
    elif change == "wrong_onchain_count":
        proof["onchain_position_count"] = 1
    elif change == "wrong_indexer_count":
        proof["indexer_position_count"] = 1
    else:
        proof["indexer_distinct_key_count"] = 0
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 7, 1))

    with pytest.raises(ValueError, match=expected):
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
        rows[1] = {"id": f"0x{'88' * 32}:{STAMP}", "sizeInUsd": "10"}
        replacement_ids = sorted([IDS[0], rows[1]["id"], *IDS[2:]])
        replacement_digest = hashlib.sha256("\n".join(replacement_ids).encode()).hexdigest()
        observed["position_ids_sha256"] = replacement_digest
        receipt["position_ids_sha256"] = replacement_digest
    elif change == "market_side":
        observed["market_side_sha256"] = "b" * 64
    elif change == "timestamp":
        rows[1] = {"id": f"0x{'22' * 32}:{STAMP + 1}", "sizeInUsd": "10"}
    elif change == "duplicate":
        rows[1] = rows[0].copy()
    elif change == "malformed":
        rows[1] = {"id": f"0x22:{STAMP}", "sizeInUsd": "10"}
    elif change == "incomplete_proof":
        receipt["independent"]["status"] = "unavailable"
    elif change == "incomplete_primary":
        receipt["status"] = "primary_incomplete"
    elif change == "malformed_proof":
        receipt["independent"]["proof"]["indexer_key_set_sha256"] = "not-a-digest"
    else:
        receipt["independent"] = {}
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 7, 1))

    with pytest.raises((ValueError, RuntimeError), match=expected):
        binding.check_binding(DAY, receipt)


def test_cli_emits_only_aggregate_status_and_preserves_receipt(monkeypatch, tmp_path, capsys):
    observed, receipt, rows = fixture()
    path = tmp_path / "receipt.json"
    original = json.dumps(receipt).encode()
    path.write_bytes(original)
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 7, 1))

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


def test_cli_bounds_malformed_receipt_decimal(monkeypatch, tmp_path, capsys):
    observed, receipt, rows = fixture()
    receipt["venue_coverage"] = "bad"
    path = tmp_path / "receipt.json"
    original = json.dumps(receipt).encode()
    path.write_bytes(original)
    monkeypatch.setattr(binding, "collect_primary", lambda day: observed)
    monkeypatch.setattr(binding, "fetch_connection", lambda *_: (rows, 7, 1))

    assert binding.main(["--day", DAY.isoformat(), "--receipt", str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("binding check failed:")
    assert "Traceback" not in output.err
    assert all(position_id not in output.err for position_id in IDS)
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
