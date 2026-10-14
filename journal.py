"""journal.py — append-only, tamper-evident run journal (JSON Lines + SHA-256 chain).

Why a hash chain instead of a plain log: the interesting question is never "what does the
log say today", it is "could someone have edited it after the fact". Each record carries the
hash of the previous record, so removing or rewriting a line breaks the chain and the auditor
notices. Nothing here assumes the writer is honest — it only makes edits visible.

Layout: one JSON object per line, fields `seq`, `ts`, `prev_hash`, `hash` are managed here;
everything else is the caller's payload.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Iterator

GENESIS = "0" * 64


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(payload)).hexdigest()


class Journal:
    """Append-only writer. One record per run; the file only grows."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)

    def last(self) -> dict | None:
        last = None
        for record in iter_records(self.path):
            last = record
        return last

    def append(self, payload: dict, ts: float | None = None) -> dict:
        previous = self.last()
        seq = (previous["seq"] + 1) if previous else 1
        prev_hash = previous["hash"] if previous else GENESIS
        record = dict(payload)
        record.update({
            "seq": seq,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts) if ts else time.localtime()),
            "ts_ms": int((ts if ts else time.time()) * 1000),
            "prev_hash": prev_hash,
        })
        record["hash"] = record_hash(prev_hash, {k: v for k, v in record.items() if k != "hash"})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record


def iter_records(path: str) -> Iterator[dict]:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"journal line {lineno} is not valid JSON: {exc}") from exc


def verify_chain(path: str) -> dict:
    """Walk the chain and report every structural problem found."""
    issues: list[str] = []
    expected_seq = 1
    prev_hash = GENESIS
    count = 0
    for record in iter_records(path):
        count += 1
        if record.get("seq") != expected_seq:
            issues.append(f"record {count}: seq {record.get('seq')} != expected {expected_seq}")
            expected_seq = record.get("seq", expected_seq)
        if record.get("prev_hash") != prev_hash:
            issues.append(f"record {count}: prev_hash does not match the previous record")
        payload = {k: v for k, v in record.items() if k != "hash"}
        if record_hash(record.get("prev_hash", GENESIS), payload) != record.get("hash"):
            issues.append(f"record {count}: hash mismatch — the record was modified")
        prev_hash = record.get("hash", prev_hash)
        expected_seq += 1
    return {"ok": not issues, "records": count, "issues": issues}


def payload_of(record: dict) -> dict:
    """Everything the caller wrote, without the envelope fields."""
    return {k: v for k, v in record.items() if k not in {"seq", "ts", "ts_ms", "prev_hash", "hash"}}


def read_all(path: str) -> list[dict]:
    return list(iter_records(path))


def as_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def _unused(_: Any) -> None:  # pragma: no cover - keeps the import list honest
    return None
