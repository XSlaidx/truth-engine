"""The auditor is the point of the repo: it must pass clean runs and fail dirty ones."""
import json
import os

import auditor
import demo
import journal
import paper_book

PARAMS_PATH = os.path.join(os.path.dirname(__file__), "..", "params.example.json")


def load_params():
    with open(PARAMS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def rule_for(_symbol):
    return paper_book.ema_cross(9, 21)


def build_journal(tmp_path, params, runs=2):
    path = str(tmp_path / "journal.jsonl")
    book = journal.Journal(path)
    for index in range(runs):
        book.append(demo.run_once(params, seed=1000 + index))
    return path


def test_clean_run_passes_every_check(tmp_path):
    params = load_params()
    report = auditor.audit(build_journal(tmp_path, params), params, rule_for)
    assert report["ok"] is True, report["issues"]
    assert set(report["checks"]) == {"chain", "pinning", "no-lookahead", "recompute", "gaps"}
    assert report["records"] == 2


def test_rewritten_pnl_is_caught_by_recompute(tmp_path):
    params = load_params()
    path = build_journal(tmp_path, params, runs=1)
    lines = open(path, encoding="utf-8").read().splitlines()
    record = json.loads(lines[0])
    symbol = next(iter(record["symbols"]))
    record["symbols"][symbol]["trades"][0]["net"] = 0.5   # "make the first trade a winner"
    lines[0] = json.dumps(record, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    report = auditor.audit(path, params, rule_for)
    assert report["ok"] is False
    assert report["checks"]["recompute"] is False or report["checks"]["chain"] is False


def test_lookahead_signal_is_caught(tmp_path):
    params = load_params()
    path = build_journal(tmp_path, params, runs=1)
    lines = open(path, encoding="utf-8").read().splitlines()
    record = json.loads(lines[0])
    symbol = next(iter(record["symbols"]))
    bars = [paper_book.Bar(*row) for row in record["symbols"][symbol]["bars"]]
    rule = rule_for(symbol)
    # pick a bar where the rule provably sees nothing: any trade recorded there is a lookahead
    quiet_index = next(i for i in range(80, min(len(bars) - 3, 400)) if rule(bars[:i + 1], i) == 0)
    record["symbols"][symbol]["trades"].append(
        {"signal_ms": bars[quiet_index].open_ms, "entry_ms": bars[quiet_index + 1].open_ms,
         "exit_ms": bars[quiet_index + 2].open_ms, "side": 1, "entry": bars[quiet_index].close,
         "exit": bars[quiet_index + 2].close, "exit_reason": "time-stop", "net": 0.001})
    lines[0] = json.dumps(record, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    report = auditor.audit(path, params, rule_for)
    assert report["ok"] is False
    assert report["checks"]["no-lookahead"] is False
    assert any("rule sees" in issue for issue in report["issues"])


def test_data_gap_is_reported(tmp_path):
    params = load_params()
    path = build_journal(tmp_path, params, runs=1)
    lines = open(path, encoding="utf-8").read().splitlines()
    record = json.loads(lines[0])
    symbol = next(iter(record["symbols"]))
    bars = record["symbols"][symbol]["bars"]
    del bars[50]                                   # a hole in the candle series
    lines[0] = json.dumps(record, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    report = auditor.audit(path, params, rule_for)
    assert report["checks"]["gaps"] is False


def test_parameter_version_must_be_pinned(tmp_path):
    params = load_params()
    path = build_journal(tmp_path, params, runs=1)      # journal written under the demo version
    other = dict(params, params_version="something-else")
    report = auditor.audit(path, other, rule_for)       # audited against a different revision
    assert report["checks"]["pinning"] is False


def test_cli_exit_code_reflects_findings(tmp_path):
    params = load_params()
    path = build_journal(tmp_path, params, runs=1)
    assert auditor.main(["--journal", path, "--params", PARAMS_PATH]) == 0

    lines = open(path, encoding="utf-8").read().splitlines()
    record = json.loads(lines[0])
    record["params_version"] = "tampered"
    lines[0] = json.dumps(record, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    assert auditor.main(["--journal", path, "--params", PARAMS_PATH]) == 1


def test_empty_journal_is_not_a_failure(tmp_path):
    report = auditor.audit(str(tmp_path / "nothing.jsonl"), load_params(), rule_for)
    assert report["ok"] is True and report["records"] == 0
