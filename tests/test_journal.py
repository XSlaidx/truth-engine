"""Journal must be tamper-evident: any edit, deletion or reorder breaks the chain."""
import json
import os

import journal


def _write(path, payloads):
    book = journal.Journal(str(path))
    for payload in payloads:
        book.append(payload)
    return str(path)


def test_append_creates_a_chain(tmp_path):
    path = _write(tmp_path / "j.jsonl", [{"run": 1}, {"run": 2}, {"run": 3}])
    records = journal.read_all(path)
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert records[0]["prev_hash"] == journal.GENESIS
    assert records[1]["prev_hash"] == records[0]["hash"]
    assert journal.verify_chain(path)["ok"] is True


def test_edited_record_is_detected(tmp_path):
    path = _write(tmp_path / "j.jsonl", [{"pnl": 1.5}, {"pnl": -0.4}])
    lines = open(path, encoding="utf-8").read().splitlines()
    tampered = json.loads(lines[0])
    tampered["pnl"] = 99.0                       # the classic "improve the past" edit
    lines[0] = json.dumps(tampered, ensure_ascii=False)
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    report = journal.verify_chain(path)
    assert report["ok"] is False
    assert any("hash mismatch" in issue for issue in report["issues"])


def test_removed_record_is_detected(tmp_path):
    path = _write(tmp_path / "j.jsonl", [{"run": 1}, {"run": 2}, {"run": 3}])
    lines = open(path, encoding="utf-8").read().splitlines()
    open(path, "w", encoding="utf-8").write("\n".join([lines[0], lines[2]]) + "\n")

    report = journal.verify_chain(path)
    assert report["ok"] is False
    assert any("prev_hash" in issue for issue in report["issues"])


def test_truncated_journal_still_verifies(tmp_path):
    """Dropping the tail is visible only against an external anchor; the chain itself stays valid."""
    path = _write(tmp_path / "j.jsonl", [{"run": 1}, {"run": 2}])
    lines = open(path, encoding="utf-8").read().splitlines()
    open(path, "w", encoding="utf-8").write(lines[0] + "\n")
    assert journal.verify_chain(path)["ok"] is True


def test_empty_journal_is_ok(tmp_path):
    report = journal.verify_chain(str(tmp_path / "missing.jsonl"))
    assert report == {"ok": True, "records": 0, "issues": []}


def test_journal_file_only_grows(tmp_path):
    path = _write(tmp_path / "j.jsonl", [{"run": 1}])
    size_before = os.path.getsize(path)
    _write(tmp_path / "j.jsonl", [])  # appending nothing to an existing file
    assert os.path.getsize(path) == size_before
    journal.Journal(path).append({"run": 2})
    assert os.path.getsize(path) > size_before
