"""auditor.py — independent verification of what the journal claims.

The journal is written by the trading loop; the auditor is deliberately a separate program that
trusts nothing it is told. It re-derives every number from raw candles and reports what does not
reconcile:

``chain``      — is the append-only hash chain intact (nobody edited or dropped a record)?
``pinning``    — do all records carry the same, expected parameter version?
``no-lookahead`` — does the recorded signal appear when the rule may only read bars up to that bar?
``recompute``  — do the recorded trade results match an independent re-simulation with the same costs?
``gaps``       — are there missing candles (a data hole that would silently skip trades)?

Exit code 1 if anything fails, so it can gate a scheduled job or CI. An auditor that only prints
warnings is decoration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, Sequence

import journal
import paper_book
from paper_book import Bar


def _load_params(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _bars_for(record: dict, symbol: str) -> list[Bar]:
    symbol_payload = (record.get("symbols") or {}).get(symbol) or {}
    return paper_book.to_bars(symbol_payload.get("bars", []))


def check_gaps(bars: Sequence[Bar], interval_ms: int, tolerance: float = 1.5) -> list[str]:
    issues = []
    for previous, current in zip(bars, bars[1:]):
        step = current.open_ms - previous.open_ms
        if step <= 0 or step > interval_ms * tolerance:
            issues.append(f"gap between {previous.open_ms} and {current.open_ms} ({step} ms)")
    return issues


def audit_records(records: Sequence[dict], params: dict, rule_for: Callable[[str], paper_book.Rule],
                  interval_ms: int) -> dict:
    report = {"records": len(records), "checks": {}, "issues": []}
    if not records:
        report["checks"] = {"chain": True, "pinning": True, "no-lookahead": True, "recompute": True,
                            "gaps": True}
        report["notes"] = ["journal is empty: nothing to audit yet"]
        return report

    expected_version = params.get("params_version")
    versions = {record.get("params_version") for record in records}
    report["checks"]["pinning"] = versions == {expected_version}
    if not report["checks"]["pinning"]:
        report["issues"].append(f"parameter versions in the journal {sorted(versions)} "
                                f"do not match the expected {expected_version!r}")

    lookahead_issues, recompute_issues, gap_issues = [], [], []
    for record in records:
        for symbol, payload in (record.get("symbols") or {}).items():
            bars = _bars_for(record, symbol)
            if not bars:
                continue
            gap_issues += [f"[{record.get('seq')}] {symbol}: {msg}" for msg in
                           check_gaps(bars, interval_ms)]
            claimed = payload.get("trades") or []
            recomputed = paper_book.simulate(
                bars, rule_for(symbol),
                fee_side=params["costs"]["fee_side"], slip_side=params["costs"]["slip_side"],
                stop_atr=params.get("strategy", {}).get("stop_atr", 1.0),
                take_atr=params.get("strategy", {}).get("take_atr", 1.5),
                max_hold=params.get("strategy", {}).get("max_hold_bars", 48))
            if len(claimed) != len(recomputed):
                recompute_issues.append(
                    f"[{record.get('seq')}] {symbol}: journal claims {len(claimed)} trades, "
                    f"independent re-simulation got {len(recomputed)}")
            for index, (a, b) in enumerate(zip(claimed, recomputed)):
                if abs(a.get("net", 0) - b.net) > 1e-9:
                    recompute_issues.append(
                        f"[{record.get('seq')}] {symbol} trade {index}: recorded {a.get('net')}, "
                        f"recomputed {round(b.net, 9)}")
            # A rule that is allowed to read bars[:index+1] must produce the recorded side.
            for trade in claimed:
                index = next((i for i, bar in enumerate(bars) if bar.open_ms == trade.get("signal_ms")), None)
                if index is None:
                    lookahead_issues.append(f"[{record.get('seq')}] {symbol}: signal bar not in candles")
                    continue
                side = rule_for(symbol)(bars[:index + 1], index)
                if side != trade.get("side"):
                    lookahead_issues.append(
                        f"[{record.get('seq')}] {symbol} bar {bars[index].open_ms}: recorded side "
                        f"{trade.get('side')}, rule sees {side} when restricted to bars up to that bar")

    report["checks"]["no-lookahead"] = not lookahead_issues
    report["checks"]["recompute"] = not recompute_issues
    report["checks"]["gaps"] = not gap_issues
    report["issues"] += lookahead_issues + recompute_issues + gap_issues
    return report


def audit(journal_path: str, params: dict, rule_for: Callable[[str], paper_book.Rule],
          interval_ms: int | None = None) -> dict:
    interval_ms = interval_ms or params.get("interval_ms", 3_600_000)
    chain = journal.verify_chain(journal_path)
    report = audit_records(journal.read_all(journal_path), params, rule_for, interval_ms)
    report["checks"]["chain"] = chain["ok"]
    report["issues"] += chain["issues"]
    report["ok"] = all(report["checks"].values())
    return report


def format_report(report: dict) -> str:
    lines = ["audit report", "=" * 40]
    for name, ok in sorted(report["checks"].items()):
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {name}")
    lines.append(f"  records: {report['records']}")
    if report.get("issues"):
        lines.append("  issues:")
        lines += [f"    - {issue}" for issue in report["issues"]]
    else:
        lines.append("  issues: none")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent verification of a run journal")
    parser.add_argument("--journal", default=os.path.join("runs", "journal.jsonl"))
    parser.add_argument("--params", default="params.example.json")
    parser.add_argument("--symbol", action="append", default=None,
                        help="symbols to audit (default: from params)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    params = _load_params(args.params)
    symbols = args.symbol or params.get("symbols", ["DEMOUSDT"])

    def rule_for(_symbol: str) -> paper_book.Rule:
        strategy = params.get("strategy", {})
        return paper_book.ema_cross(strategy.get("fast", 9), strategy.get("slow", 21))

    report = audit(args.journal, params, rule_for)
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
