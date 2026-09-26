"""Offline checks for GMX DataStore ABI decoding and OI aggregation."""

import pytest
from scripts import probe_gmx_onchain_position_set as probe


def test_position_keys_decodes_complete_dynamic_array(monkeypatch):
    key_a = bytes.fromhex("11" * 32)
    key_b = bytes.fromhex("22" * 32)
    payload = probe.uint256(32) + probe.uint256(2) + key_a + key_b
    monkeypatch.setattr(probe, "eth_call", lambda *args: payload)

    assert probe.position_keys(b"\x00" * 32, 0, 2, "0x1") == [key_a, key_b]

    with pytest.raises(RuntimeError, match="count/length mismatch"):
        probe.position_keys(b"\x00" * 32, 0, 3, "0x1")


def test_market_list_decodes_address_array(monkeypatch):
    address_a = bytes.fromhex("11" * 20)
    address_b = bytes.fromhex("22" * 20)
    payloads = iter(
        [
            probe.uint256(2),
            probe.uint256(32)
            + probe.uint256(2)
            + address_a.rjust(32, b"\x00")
            + address_b.rjust(32, b"\x00"),
        ]
    )
    monkeypatch.setattr(probe, "eth_call", lambda *args: next(payloads))

    assert probe.market_list("0x1") == [address_a, address_b]


def test_onchain_oi_same_collateral_is_not_double_counted(monkeypatch):
    market = "0x" + "11" * 20
    token = "0x" + "22" * 20
    monkeypatch.setattr(probe, "market_tokens", lambda *_: (token, token))
    calls = iter([10, 10, 14, 14])
    monkeypatch.setattr(probe, "eth_call", lambda *args: probe.uint256(next(calls)))

    assert probe.onchain_oi(market, "0x1") == (10, 14)


def test_onchain_oi_distinct_collateral_sums_both(monkeypatch):
    market = "0x" + "11" * 20
    monkeypatch.setattr(
        probe,
        "market_tokens",
        lambda *_: ("0x" + "22" * 20, "0x" + "33" * 20),
    )
    calls = iter([5, 7, 11, 13])
    monkeypatch.setattr(probe, "eth_call", lambda *args: probe.uint256(next(calls)))

    assert probe.onchain_oi(market, "0x1") == (12, 24)
