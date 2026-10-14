"""demo.py — the whole loop in one command, offline: candles -> paper book -> journal -> auditor.

Run it to see what the repository actually does:

    python demo.py            # generate candles, run the paper book, audit the journal
    python demo.py --runs 3   # append three runs, then audit the whole chain

Nothing here touches an exchange, and the candles are a seeded random walk, so the output is
reproducible. The point is not the (meaningless) PnL of a synthetic series — it is that the
harness runs, records exactly the bars it used, and the auditor re-derives every claim from them.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

import auditor
import journal
import paper_book


def synth_bars(symbol: str, count: int, *, seed: int, interval_ms: int, start_price: float,
               volatility: float = 0.004) -> list[paper_book.Bar]:
    """Deterministic pseudo-market: same seed -> same candles on every machine."""
    rng = random.Random(f"{symbol}:{seed}")
    bars: list[paper_book.Bar] = []
    price = start_price
    open_ms = 1_600_000_000_000
    for i in range(count):
        drift = rng.gauss(0, volatility)
        close = max(1.0, price * (1 + drift))
        high = max(price, close) * (1 + abs(rng.gauss(0, volatility / 2)))
        low = min(price, close) * (1 - abs(rng.gauss(0, volatility / 2)))
        bars.append(paper_book.Bar(open_ms + i * interval_ms, price, high, low, close))
        price = close
    return bars


def run_once(params: dict, *, seed: int, bars_count: int = 400) -> dict:
    interval_ms = params["interval_ms"]
    strategy = params.get("strategy", {})
    rule = paper_book.ema_cross(strategy.get("fast", 9), strategy.get("slow", 21))
    payload = {"params_version": params["params_version"], "mode": "paper", "seed": seed,
               "interval_ms": interval_ms, "symbols": {}}
    for symbol, spec in params["symbols"].items():
        bars = synth_bars(symbol, bars_count, seed=seed, interval_ms=interval_ms,
                          start_price=spec["start_price"])
        closed = paper_book.drop_forming(bars, interval_ms, bars[-1].open_ms + interval_ms)
        trades = paper_book.simulate(closed, rule, fee_side=params["costs"]["fee_side"],
                                     slip_side=params["costs"]["slip_side"],
                                     stop_atr=strategy.get("stop_atr", 1.0),
                                     take_atr=strategy.get("take_atr", 1.5),
                                     max_hold=strategy.get("max_hold_bars", 48))
        payload["symbols"][symbol] = {
            "bars": [[b.open_ms, b.open, b.high, b.low, b.close] for b in closed],
            "trades": [t.as_dict() for t in trades],
            "metrics": paper_book.metrics(trades),
        }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline demo of the verification harness")
    parser.add_argument("--params", default="params.example.json")
    parser.add_argument("--journal", default=os.path.join("runs", "journal.jsonl"))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--keep-journal", action="store_true",
                        help="append to an existing journal instead of starting a fresh one")
    args = parser.parse_args(argv)

    with open(args.params, encoding="utf-8") as fh:
        params = json.load(fh)

    if not args.keep_journal and os.path.exists(args.journal):
        os.remove(args.journal)

    book = journal.Journal(args.journal)
    for index in range(args.runs):
        record = book.append(run_once(params, seed=1000 + index))
        summary = {symbol: payload["metrics"] for symbol, payload in record["symbols"].items()}
        print(f"run {record['seq']}: {json.dumps(summary, ensure_ascii=False)}")

    def rule_for(symbol: str) -> paper_book.Rule:
        strategy = params.get("strategy", {})
        return paper_book.ema_cross(strategy.get("fast", 9), strategy.get("slow", 21))

    report = auditor.audit(args.journal, params, rule_for)
    print()
    print(auditor.format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
