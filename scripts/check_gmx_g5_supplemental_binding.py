"""Read-only same-indexer binding check; not a G5 criterion or chain proof."""

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, date, datetime
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from scripts.audit_gmx_g5_public import fetch_connection, utc_midnight
from scripts.collect_gmx_g5_window import (
    collect_primary,
    independently_validated,
    verify_refetched_day,
)

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_POSITION_ID = re.compile(r"0x([0-9a-fA-F]{64}):([0-9]+)\Z")


def check_binding(day: date, receipt: dict[str, Any]) -> dict[str, Any]:
    if receipt.get("status") != "primary_complete":
        raise ValueError("primary receipt is not complete")
    independent = receipt.get("independent")
    proof = independent.get("proof") if isinstance(independent, dict) else None
    saved_key_digest = proof.get("indexer_key_set_sha256") if isinstance(proof, dict) else None
    if (
        not isinstance(proof, dict)
        or not isinstance(saved_key_digest, str)
        or not _HEX_DIGEST.fullmatch(saved_key_digest)
    ):
        raise ValueError("missing or malformed independent key digest")
    if not isinstance(independent, dict) or independent.get("status") != "complete":
        raise ValueError("independent proof is not complete")
    if not independently_validated(day, receipt):
        raise ValueError("independent proof does not satisfy frozen criteria")
    chain_key_digest = proof.get("key_set_sha256")
    if not isinstance(chain_key_digest, str) or not _HEX_DIGEST.fullmatch(chain_key_digest):
        raise ValueError("missing or malformed on-chain key digest")
    if chain_key_digest != saved_key_digest:
        raise ValueError("on-chain key digest mismatch")
    if any(
        proof.get(field) != receipt["position_count"]
        for field in (
            "keys_read", "onchain_position_count", "indexer_position_count",
            "indexer_distinct_key_count",
        )
    ):
        raise ValueError("proof position count mismatch")

    observed = collect_primary(day)
    observed["date_utc"] = day.isoformat()
    verify_refetched_day(day, observed, receipt)

    timestamp = utc_midnight(day)
    where = "{isSnapshot_eq:true," + f'snapshotTimestamp_eq:{timestamp},sizeInUsd_gt:"0"' + "}"
    rows, reported_count, _ = fetch_connection(
        "positionsConnection", where, "id_ASC", "id sizeInUsd", 1000
    )
    ids: list[str] = []
    keys: list[bytes] = []
    for row in rows:
        position_id = row.get("id") if isinstance(row, dict) else None
        match = _POSITION_ID.fullmatch(position_id) if isinstance(position_id, str) else None
        if not isinstance(position_id, str) or match is None or match.group(2) != str(timestamp):
            raise ValueError("malformed or wrong-time position ID")
        ids.append(position_id)
        keys.append(bytes.fromhex(match.group(1)))
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("empty or duplicate position keys")
    if len(keys) != reported_count or reported_count != receipt.get("position_count"):
        raise ValueError("position count mismatch")

    id_digest = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
    key_digest = hashlib.sha256(b"".join(sorted(keys))).hexdigest()
    if id_digest != receipt.get("position_ids_sha256"):
        raise ValueError("position ID digest mismatch")
    if key_digest != saved_key_digest:
        raise ValueError("independent key digest mismatch")
    return {
        "status": "pass",
        "date_utc": day.isoformat(),
        "position_count": reported_count,
        "position_ids_sha256": id_digest,
        "indexer_key_set_sha256": key_digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt_bytes = args.receipt.read_bytes()
        receipt = json.loads(receipt_bytes)
        if not isinstance(receipt, dict):
            raise ValueError("receipt is not an object")
        if receipt.get("snapshot_utc") != datetime.fromtimestamp(
            utc_midnight(args.day), UTC
        ).isoformat():
            raise ValueError("receipt date mismatch")
        result = check_binding(args.day, receipt)
        result["receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
        result["checked_at_utc"] = datetime.now(UTC).isoformat()
    except (
        OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError,
        InvalidOperation,
    ) as error:
        print(f"binding check failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
