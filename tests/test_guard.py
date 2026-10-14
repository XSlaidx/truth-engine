"""Guard rails: day snapshot, loss streak, kill switches, drift, entry permission."""
import json
import os

import guard

PARAMS = {
    "params_version": "test",
    "guards": {"drift_limit_ms": 1500, "killswitch_soft_file": "stop",
               "killswitch_hard_file": "stop_hard", "lock_file": "locked.json"},
    "risk": {"max_open_positions": 1, "max_active_orders": 2, "daily_stop_pct": 1.0,
             "loss_streak_limit": 3},
}


def test_daily_stop_is_measured_from_the_utc_snapshot(tmp_path):
    state = {}
    first = guard.roll_day(state, 1000.0)
    assert first == 0.0 and state["day_start_equity"] == 1000.0
    # 0.5% down is not a stop, even though the balance moved
    assert round(guard.roll_day(state, 995.0), 2) == -0.5
    # the snapshot does not move during the day
    assert state["day_start_equity"] == 1000.0
    # a new UTC day resets the reference point
    next_day = 1_700_000_000 + 86400
    guard.roll_day(state, 900.0, ts=next_day)
    assert state["day_start_equity"] == 900.0


def test_loss_streak_counts_only_the_tail():
    assert guard.loss_streak([0.01, -0.02, -0.03]) == 2
    assert guard.loss_streak([-0.01, 0.02, -0.03]) == 1
    assert guard.loss_streak([]) == 0


def test_loss_streak_limit_blocks_new_entries(tmp_path):
    state = {"forward_book": [{"net": -0.01}, {"net": -0.01}, {"net": -0.01}]}
    decision = guard.allow_new_entry(PARAMS, state, base_dir=str(tmp_path), drift_is_ok=True)
    assert decision.allowed is False
    assert any("loss-streak" in reason for reason in decision.reasons)


def test_lock_requires_manual_unlock(tmp_path):
    state = {}
    guard.lock(str(tmp_path / "locked.json"), state, "3 losses in a row")
    assert guard.is_locked(str(tmp_path / "locked.json"))["reason"] == "3 losses in a row"
    os.remove(tmp_path / "locked.json")
    assert guard.is_locked(str(tmp_path / "locked.json")) is None


def test_kill_switch_files_beat_flags(tmp_path):
    soft = tmp_path / "stop"
    hard = tmp_path / "stop_hard"
    soft.write_text("", encoding="utf-8")
    assert guard.killswitch(str(tmp_path), PARAMS)[0] == "soft"
    hard.write_text("", encoding="utf-8")
    assert guard.killswitch(str(tmp_path), PARAMS)[0] == "hard"


def test_drift_blocks_entries_when_server_time_is_unavailable():
    drift, ok = guard.drift_ok(local_ms=1_000_000, server_ms=None, params=PARAMS)
    assert drift is None and ok is False
    drift, ok = guard.drift_ok(local_ms=1_000_000, server_ms=1_000_900, params=PARAMS)
    assert drift == 900 and ok is True
    drift, ok = guard.drift_ok(local_ms=1_000_000, server_ms=1_009_000, params=PARAMS)
    assert drift == 9000 and ok is False


def test_order_and_position_limits(tmp_path):
    state = {}
    busy = guard.allow_new_entry(PARAMS, state, base_dir=str(tmp_path), drift_is_ok=True,
                                 open_positions=1)
    assert busy.allowed is False and any("position limit" in r for r in busy.reasons)
    orders = guard.allow_new_entry(PARAMS, state, base_dir=str(tmp_path), drift_is_ok=True,
                                   active_orders=2)
    assert orders.allowed is False and any("active-order" in r for r in orders.reasons)
    assert guard.allow_new_entry(PARAMS, state, base_dir=str(tmp_path), drift_is_ok=True).allowed


def test_state_round_trip_is_atomic_and_typed(tmp_path):
    path = str(tmp_path / "state.json")
    state = guard.load_state(path)
    state["runs"] = 3
    guard.save_state(path, state)
    assert guard.load_state(path)["runs"] == 3
    assert not os.path.exists(path + ".tmp")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    try:
        guard.load_state(path)
    except RuntimeError as exc:
        assert "unreadable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("corrupt state must not be silently replaced")


def test_params_example_is_valid_json():
    with open(os.path.join(os.path.dirname(__file__), "..", "params.example.json"), encoding="utf-8") as fh:
        params = json.load(fh)
    assert params["params_version"]
    assert params["costs"]["fee_side"] > 0 and params["costs"]["slip_side"] > 0
