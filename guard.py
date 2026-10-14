"""guard.py — risk limits, durable state and kill switches for an unattended bar-cycle runner.

Design rules (the whole point of this file):

* state lives on disk and is restored on every tick — a cron job keeps nothing in memory;
* the daily stop is measured from equity snapshotted at 00:00 UTC, never from the balance
  observed mid-tick (otherwise a dropping balance "resets" the stop all day long);
* N consecutive losing trades lock the stand until a human removes the lock file:
  an automatic unlock would let a broken strategy restart itself at the worst moment;
* kill switches are files, not flags in memory: `stop` = soft (no new entries, let open
  positions reach their stop/take), `stop_hard` = hard (flat everything now);
* clock drift above the limit blocks new entries, because an out-of-sync clock silently
  invalidates signed requests and bar-close timing;
* at most `max_active_orders` orders may be live at once — this is what stops a looping
  script from spraying the book with limit orders.

Nothing in here talks to an exchange: the same code guards a paper book and a live account.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_STATE = {
    "created": None,
    "runs": 0,
    "anchor_ts": None,
    "day": None,
    "day_start_equity": None,
    "pending": [],
    "forward_book": [],
    "notes": [],
}


def load_params(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_STATE, created=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must not silently become "no positions" — surface it.
        raise RuntimeError(f"state file is unreadable: {path}")


def save_state(path: str, state: dict) -> None:
    """Atomic write: a killed process must never leave half a state file behind."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def utc_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts else time.time()))


def roll_day(state: dict, equity: float, ts: float | None = None) -> float:
    """Snapshot start-of-day equity and return today's PnL in percent."""
    day = utc_day(ts)
    if state.get("day") != day or not state.get("day_start_equity"):
        state["day"] = day
        state["day_start_equity"] = equity
    start = state["day_start_equity"]
    return (equity / start - 1) * 100 if start else 0.0


def lock(path: str, state: dict, reason: str) -> dict:
    """Write the lock file. Unlocking is a human action (delete the file), by design."""
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "unlock": "remove the lock file by hand after reviewing the journal",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    state.setdefault("notes", []).append(dict(record, kind="lock"))
    return record


def is_locked(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"reason": "lock file exists but is unreadable"}


def killswitch(base_dir: str, params: dict) -> tuple[str | None, str]:
    """Return ('hard'|'soft'|None, reason). Files beat flags: they survive restarts."""
    guards = params.get("guards", {})
    if os.path.exists(os.path.join(base_dir, guards.get("killswitch_hard_file", "stop_hard"))):
        return "hard", f"found {guards.get('killswitch_hard_file', 'stop_hard')}"
    if os.path.exists(os.path.join(base_dir, guards.get("killswitch_soft_file", "stop"))):
        return "soft", f"found {guards.get('killswitch_soft_file', 'stop')}"
    return None, ""


def clock_drift_ms(local_ms: int, server_ms: int) -> int:
    """Positive = our clock is behind the server."""
    return server_ms - local_ms


def drift_ok(local_ms: int, server_ms: int | None, params: dict) -> tuple[int | None, bool]:
    """Missing server time is treated as "cannot verify" — and that blocks new entries."""
    if server_ms is None:
        return None, False
    limit = params.get("guards", {}).get("drift_limit_ms", 1500)
    drift = clock_drift_ms(local_ms, server_ms)
    return drift, abs(drift) <= limit


def loss_streak(results: Iterable[float]) -> int:
    """Length of the losing run at the end of a sequence of trade results."""
    n = 0
    for value in reversed(list(results)):
        if value < 0:
            n += 1
        else:
            break
    return n


@dataclass
class EntryDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def allow_new_entry(params: dict, state: dict, *, base_dir: str, drift_is_ok: bool,
                    open_positions: int = 0, active_orders: int = 0) -> EntryDecision:
    """Single source of truth for "may we open a position now?"."""
    reasons: list[str] = []
    if not drift_is_ok:
        reasons.append("clock drift above the limit (or server time unavailable)")
    switch, why = killswitch(base_dir, params)
    if switch:
        reasons.append(f"kill switch: {why} ({switch})")
    if is_locked(os.path.join(base_dir, params.get("guards", {}).get("lock_file", "locked.json"))):
        reasons.append("stand is locked (manual unlock required)")
    risk = params.get("risk", {})
    if open_positions >= risk.get("max_open_positions", 1):
        reasons.append("position limit reached")
    if active_orders >= risk.get("max_active_orders", 2):
        reasons.append("active-order limit reached")
    equity = state.get("day_start_equity")
    if equity and state.get("day_pnl_pct", 0.0) <= -risk.get("daily_stop_pct", 1.0):
        reasons.append("daily stop hit")
    if loss_streak(t.get("net", 0.0) for t in state.get("forward_book", [])) >= risk.get(
            "loss_streak_limit", 3):
        reasons.append("loss-streak limit hit")
    return EntryDecision(not reasons, reasons)


def as_dict(obj: Any) -> dict:
    """Small helper for tests and reports."""
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))
