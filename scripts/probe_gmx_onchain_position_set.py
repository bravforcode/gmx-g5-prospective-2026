"""Read GMX's on-chain position-set count at one historical Arbitrum block.

Uses the public Arbitrum RPC and GMX DataStore, without credentials or writes.
Only aggregate counts and a key-set digest are printed; position keys are not.
This probes an independent enumeration path, not position-size/OI coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from urllib.request import Request, urlopen

from Crypto.Hash import keccak

RPC = os.environ.get("GMX_RPC_URL", "https://arb1.arbitrum.io/rpc")
DATA_STORE = "0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8"
READER = "0xfA26cBb46e2614609406de08CA1Dc7f70a684184"
RPC_REQUESTS = 0
RPC_DELAY_SECONDS = 0.0


def keccak256(data: bytes) -> bytes:
    digest = keccak.new(digest_bits=256)
    digest.update(data)
    return digest.digest()


def uint256(value: int) -> bytes:
    return value.to_bytes(32, "big")


def rpc_call(method: str, params: list[object]) -> object:
    global RPC_REQUESTS
    if RPC_DELAY_SECONDS:
        time.sleep(RPC_DELAY_SECONDS)
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    request = Request(
        RPC,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "quant-paper-g5/1.0"},
        method="POST",
    )
    RPC_REQUESTS += 1
    with urlopen(request, timeout=45) as response:
        result = json.load(response)
    if "error" in result:
        raise RuntimeError(f"{method} failed: {result['error']}")
    return result["result"]


def eth_call(data: bytes, block_tag: str, to: str = DATA_STORE) -> bytes:
    result = rpc_call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block_tag])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise RuntimeError("eth_call returned non-hex data")
    return bytes.fromhex(result[2:])


def position_list_key() -> bytes:
    return position_list_key_for("POSITION_LIST")


def selector(signature: str) -> bytes:
    return keccak256(signature.encode("ascii"))[:4]


def address_word(address: str) -> bytes:
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError(f"invalid EVM address: {address}")
    return bytes.fromhex(address[2:]).rjust(32, b"\x00")


def market_tokens(market: str, block_tag: str) -> tuple[str, str]:
    call_data = (
        selector("getMarket(address,address)")
        + address_word(DATA_STORE)
        + address_word(market)
    )
    result = eth_call(call_data, block_tag, READER)
    if len(result) != 128 or result[:32][-20:] != bytes.fromhex(market[2:]):
        raise RuntimeError("Reader.getMarket returned an unexpected market struct")
    return "0x" + result[64:96][-20:].hex(), "0x" + result[96:128][-20:].hex()


def onchain_oi(market: str, block_tag: str) -> tuple[int, int]:
    long_token, short_token = market_tokens(market, block_tag)
    prefix = position_list_key_for("OPEN_INTEREST")
    divisor = 2 if long_token.lower() == short_token.lower() else 1
    totals: list[int] = []
    for is_long in (True, False):
        amount = 0
        for collateral in (long_token, short_token):
            key = keccak256(
                prefix + address_word(market) + address_word(collateral) + uint256(int(is_long))
            )
            result = eth_call(selector("getUint(bytes32)") + key, block_tag)
            if len(result) != 32:
                raise RuntimeError("DataStore.getUint returned an invalid word")
            amount += int.from_bytes(result, "big")
        totals.append(amount // divisor)
    return totals[0], totals[1]


def position_list_key_for(label: str) -> bytes:
    value = label.encode("ascii")
    encoded = uint256(32) + uint256(len(value)) + value.ljust((len(value) + 31) // 32 * 32, b"\x00")
    return keccak256(encoded)


def position_count(key: bytes, block_tag: str) -> int:
    result = eth_call(selector("getBytes32Count(bytes32)") + key, block_tag)
    if len(result) != 32:
        raise RuntimeError(f"position count returned {len(result)} bytes, expected 32")
    return int.from_bytes(result, "big")


def market_list(block_tag: str) -> list[bytes]:
    key = position_list_key_for("MARKET_LIST")
    count_data = eth_call(selector("getAddressCount(bytes32)") + key, block_tag)
    if len(count_data) != 32:
        raise RuntimeError("market count returned an invalid word")
    count = int.from_bytes(count_data, "big")
    markets: list[bytes] = []
    for start in range(0, count, 100):
        end = min(start + 100, count)
        call_data = (
            selector("getAddressValuesAt(bytes32,uint256,uint256)")
            + key
            + uint256(start)
            + uint256(end)
        )
        result = eth_call(call_data, block_tag)
        if len(result) != 64 + 32 * (end - start):
            raise RuntimeError("market-list response has invalid length")
        if int.from_bytes(result[:32], "big") != 32:
            raise RuntimeError("market-list response has invalid ABI offset")
        if int.from_bytes(result[32:64], "big") != end - start:
            raise RuntimeError("market-list response has invalid array count")
        for index in range(end - start):
            word = result[64 + index * 32 : 96 + index * 32]
            if word[:12] != b"\x00" * 12:
                raise RuntimeError("market-list address padding is invalid")
            markets.append(word[12:])
    if len(markets) != len(set(markets)):
        raise RuntimeError("duplicate market in on-chain list")
    return markets


def position_keys(key: bytes, start: int, end: int, block_tag: str) -> list[bytes]:
    call_data = (
        selector("getBytes32ValuesAt(bytes32,uint256,uint256)")
        + key
        + uint256(start)
        + uint256(end)
    )
    result = eth_call(call_data, block_tag)
    if len(result) < 64 or int.from_bytes(result[:32], "big") != 32:
        raise RuntimeError("position-key response has invalid ABI offset")
    count = int.from_bytes(result[32:64], "big")
    if count != end - start or len(result) != 64 + 32 * count:
        raise RuntimeError("position-key response count/length mismatch")
    return [result[64 + i * 32 : 96 + i * 32] for i in range(count)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--expected", type=int)
    parser.add_argument("--all-keys", action="store_true")
    parser.add_argument("--compare-indexer", action="store_true")
    parser.add_argument("--sample-values", type=int, default=0)
    parser.add_argument("--market", help="compare one market's on-chain OI to indexer OI")
    parser.add_argument(
        "--all-markets", action="store_true", help="cross-check every indexer OI market"
    )
    parser.add_argument("--market-list", action="store_true", help="compare on-chain market list")
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()
    if args.block <= 0 or not 1 <= args.page_size <= 500:
        raise SystemExit("block must be positive and page-size must be 1..500")
    if args.compare_indexer and not args.all_keys:
        raise SystemExit("--compare-indexer requires --all-keys")
    if args.sample_values < 0 or (args.sample_values and not args.compare_indexer):
        raise SystemExit("--sample-values requires --compare-indexer")
    if args.all_markets and args.market:
        raise SystemExit("choose --all-markets or --market")
    if args.all_markets:
        global RPC_DELAY_SECONDS
        RPC_DELAY_SECONDS = 0.2

    block_tag = hex(args.block)
    chain_id = rpc_call("eth_chainId", [])
    if chain_id != "0xa4b1":
        raise RuntimeError(f"unexpected chain ID: {chain_id}")
    block = rpc_call("eth_getBlockByNumber", [block_tag, False])
    if not isinstance(block, dict) or block.get("number") != block_tag:
        raise RuntimeError("historical block unavailable or number mismatch")

    key = position_list_key()
    count = position_count(key, block_tag)
    if args.expected is not None and count != args.expected:
        raise RuntimeError(f"on-chain count {count} != expected {args.expected}")

    limit = count if args.all_keys else min(count, 10)
    values: list[bytes] = []
    for start in range(0, limit, args.page_size):
        values.extend(position_keys(key, start, min(start + args.page_size, limit), block_tag))
        time.sleep(0.25)
    if len(set(values)) != len(values):
        raise RuntimeError("duplicate position keys returned")
    comparison: dict[str, object] = {}
    if args.compare_indexer:
        from audit_gmx_g5_public import fetch_connection

        snapshot_ts = int(block["timestamp"], 16)
        where = (
            "{isSnapshot_eq:true,"
            f'snapshotTimestamp_eq:{snapshot_ts},sizeInUsd_gt:"0"}}'
        )
        rows, reported_count, pages = fetch_connection(
            "positionsConnection", where, "id_ASC", "id sizeInUsd", 1000
        )
        indexer_keys: list[bytes] = []
        for row in rows:
            key_hex, id_timestamp = str(row["id"]).split(":", 1)
            if int(id_timestamp) != snapshot_ts:
                raise RuntimeError("indexer position ID timestamp mismatch")
            indexer_keys.append(bytes.fromhex(key_hex.removeprefix("0x")))
        onchain_set = set(values)
        indexer_set = set(indexer_keys)
        comparison = {
            "indexer_position_count": reported_count,
            "indexer_pages": pages,
            "indexer_distinct_key_count": len(indexer_set),
            "indexer_position_size_total_raw": sum(int(row["sizeInUsd"]) for row in rows),
            "onchain_only_keys": len(onchain_set - indexer_set),
            "indexer_only_keys": len(indexer_set - onchain_set),
            "indexer_key_set_sha256": hashlib.sha256(
                b"".join(sorted(indexer_set))
            ).hexdigest(),
        }
        if reported_count != len(indexer_keys) or len(indexer_set) != len(indexer_keys):
            raise RuntimeError("indexer position count/unique IDs mismatch")
        if args.sample_values:
            sample_count = min(args.sample_values, len(rows))
            sample_indices = (
                [len(rows) // 2]
                if sample_count == 1
                else [round(i * (len(rows) - 1) / (sample_count - 1)) for i in range(sample_count)]
            )
            size_label = position_list_key_for("SIZE_IN_USD")
            mismatches = 0
            for index in sample_indices:
                storage_key = keccak256(indexer_keys[index] + size_label)
                encoded_size = eth_call(selector("getUint(bytes32)") + storage_key, block_tag)
                if len(encoded_size) != 32:
                    raise RuntimeError("on-chain position size returned an invalid word")
                if int.from_bytes(encoded_size, "big") != int(rows[index]["sizeInUsd"]):
                    mismatches += 1
                time.sleep(0.25)
            comparison.update(
                {"position_values_sampled": sample_count, "position_value_mismatches": mismatches}
            )
    if args.market:
        from audit_gmx_g5_public import fetch_connection

        snapshot_ts = int(block["timestamp"], 16)
        chain_long, chain_short = onchain_oi(args.market, block_tag)
        oi_rows, _, _ = fetch_connection(
            "fundingBalanceOiSnapshotsConnection",
            f"{{snapshotTimestamp_eq:{snapshot_ts}}}",
            "marketAddress_ASC",
            "marketAddress longOpenInterestUsd shortOpenInterestUsd",
            500,
        )
        rows = [
            row for row in oi_rows
            if row["marketAddress"].lower() == args.market.lower()
        ]
        if len(rows) != 1:
            raise RuntimeError("indexer OI row was not unique or absent")
        node = rows[0]
        index_long = int(node["longOpenInterestUsd"])
        index_short = int(node["shortOpenInterestUsd"])
        comparison.update(
            {
                "market": args.market,
                "onchain_long_oi_raw": chain_long,
                "onchain_short_oi_raw": chain_short,
                "indexer_long_oi_raw": index_long,
                "indexer_short_oi_raw": index_short,
                "market_oi_exact_match": (chain_long, chain_short)
                == (index_long, index_short),
            }
        )
    if args.market_list:
        from audit_gmx_g5_public import fetch_connection

        snapshot_ts = int(block["timestamp"], 16)
        rows, reported_count, pages = fetch_connection(
            "fundingBalanceOiSnapshotsConnection",
            f"{{snapshotTimestamp_eq:{snapshot_ts}}}",
            "marketAddress_ASC",
            "marketAddress",
            500,
        )
        chain_markets = set(market_list(block_tag))
        indexer_markets = {bytes.fromhex(str(row["marketAddress"])[2:]) for row in rows}
        if len(indexer_markets) != reported_count:
            raise RuntimeError("indexer OI market list contains duplicates")
        missing_oi = [
            onchain_oi("0x" + key.hex(), block_tag)
            for key in chain_markets - indexer_markets
        ]
        comparison.update(
            {
                "chain_market_count": len(chain_markets),
                "indexer_oi_market_count": reported_count,
                "indexer_oi_market_pages": pages,
                "chain_only_markets": len(chain_markets - indexer_markets),
                "indexer_only_markets": len(indexer_markets - chain_markets),
                "chain_only_positive_oi_markets": sum(sum(pair) > 0 for pair in missing_oi),
                "chain_only_oi_raw": sum(sum(pair) for pair in missing_oi),
                "chain_market_set_sha256": hashlib.sha256(
                    b"".join(sorted(chain_markets))
                ).hexdigest(),
                "indexer_market_set_sha256": hashlib.sha256(
                    b"".join(sorted(indexer_markets))
                ).hexdigest(),
            }
        )
    if args.all_markets:
        from audit_gmx_g5_public import fetch_connection

        snapshot_ts = int(block["timestamp"], 16)
        oi_rows, oi_count, oi_pages = fetch_connection(
            "fundingBalanceOiSnapshotsConnection",
            f"{{snapshotTimestamp_eq:{snapshot_ts}}}",
            "marketAddress_ASC",
            "marketAddress longOpenInterestUsd shortOpenInterestUsd",
            500,
        )
        distinct_markets = {str(row["marketAddress"]).lower() for row in oi_rows}
        if len(distinct_markets) != len(oi_rows):
            raise RuntimeError("duplicate OI market address")
        exact_markets = 0
        exact_sides = 0
        positive_indexer_markets = 0
        max_abs_difference_raw = 0
        sum_abs_difference_raw = 0
        total_onchain_raw = 0
        total_indexer_raw = 0
        for row in oi_rows:
            market = str(row["marketAddress"])
            actual = onchain_oi(market, block_tag)
            expected = (int(row["longOpenInterestUsd"]), int(row["shortOpenInterestUsd"]))
            exact_markets += actual == expected
            positive_indexer_markets += sum(expected) > 0
            for chain_side, index_side in zip(actual, expected, strict=True):
                delta = abs(chain_side - index_side)
                exact_sides += delta == 0
                max_abs_difference_raw = max(max_abs_difference_raw, delta)
                sum_abs_difference_raw += delta
                total_onchain_raw += chain_side
                total_indexer_raw += index_side
        comparison.update(
            {
                "oi_market_rows": oi_count,
                "oi_indexer_pages": oi_pages,
                "oi_positive_indexer_markets": positive_indexer_markets,
                "oi_exact_markets": exact_markets,
                "oi_exact_sides": exact_sides,
                "oi_max_abs_difference_raw": max_abs_difference_raw,
                "oi_sum_abs_difference_raw": sum_abs_difference_raw,
                "oi_total_onchain_raw": total_onchain_raw,
                "oi_total_indexer_raw": total_indexer_raw,
            }
        )
    print(
        json.dumps(
            {
                "chain_id": int(chain_id, 16),
                "block": args.block,
                "block_hash": block["hash"],
                "block_time_utc": datetime.fromtimestamp(
                    int(block["timestamp"], 16), UTC
                ).isoformat(),
                "onchain_position_count": count,
                "keys_read": len(values),
                "complete_set_read": args.all_keys or count <= 10,
                "key_set_sha256": hashlib.sha256(b"".join(sorted(values))).hexdigest()
                if values
                else None,
                "rpc_requests": RPC_REQUESTS,
                **comparison,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
