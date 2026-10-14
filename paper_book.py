"""paper_book.py — honest bar-close simulation: costs on both sides, no peeking at the future.

Three rules this module enforces, because they are where most "profitable" backtests are born:

1. **Closed bars only.** `drop_forming()` removes the candle that is still being written; a signal
   computed on a forming bar is a lookahead bug wearing a costume.
2. **Costs on both sides.** Every trade pays commission *and* slippage on entry and on exit.
   A strategy that only survives without them does not survive.
3. **No intrabar magic.** A stop is triggered only if the price actually traded there, judged on
   the following bars — never on the bar that produced the signal.

The trading rule itself is pluggable (`Rule`): the skeleton ships with a textbook EMA cross so the
pipeline can be exercised end to end, not because it is expected to make money. Real rules stay in
private code; this file is the harness that keeps them honest.
"""
from __future__ import annotations

import statistics as stats
from dataclasses import dataclass, asdict
from typing import Callable, Iterable, Sequence

Interval = int  # milliseconds


@dataclass(frozen=True)
class Bar:
    open_ms: int
    open: float
    high: float
    low: float
    close: float


def to_bars(rows: Iterable[Sequence[float]]) -> list[Bar]:
    """Accept exchange-style rows: [open_time, open, high, low, close, ...]."""
    return [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def drop_forming(bars: Sequence[Bar], interval_ms: Interval, now_ms: int) -> list[Bar]:
    """Keep only candles whose interval has fully elapsed."""
    if not bars:
        return []
    return list(bars[:-1]) if bars[-1].open_ms + interval_ms > now_ms else list(bars)


def atr(bars: Sequence[Bar], index: int, period: int = 14) -> float:
    start = index - period + 1
    if start < 1:
        return 0.0
    ranges = []
    for i in range(start, index + 1):
        prev_close = bars[i - 1].close
        ranges.append(max(bars[i].high - bars[i].low,
                          abs(bars[i].high - prev_close),
                          abs(bars[i].low - prev_close)))
    return stats.fmean(ranges)


def ema(values: Sequence[float], period: int) -> list[float]:
    out: list[float] = []
    k = 2 / (period + 1)
    for i, value in enumerate(values):
        out.append(value if i == 0 else value * k + out[-1] * (1 - k))
    return out


Rule = Callable[[Sequence[Bar], int], int]
"""A rule maps (bars, index) -> +1 long, -1 short, 0 flat. It may only read bars[:index+1]."""


def ema_cross(fast: int = 9, slow: int = 21) -> Rule:
    """Textbook example rule — illustration of the interface, not a strategy."""

    def rule(bars: Sequence[Bar], index: int) -> int:
        if index < slow + 1:
            return 0
        closes = [b.close for b in bars[:index + 1]]
        fast_ma, slow_ma = ema(closes, fast), ema(closes, slow)
        if fast_ma[index] > slow_ma[index] and fast_ma[index - 1] <= slow_ma[index - 1]:
            return 1
        if fast_ma[index] < slow_ma[index] and fast_ma[index - 1] >= slow_ma[index - 1]:
            return -1
        return 0

    return rule


@dataclass
class Trade:
    signal_ms: int
    entry_ms: int
    exit_ms: int
    side: int
    entry: float
    exit: float
    exit_reason: str
    net: float

    def as_dict(self) -> dict:
        return asdict(self)


def simulate(bars: Sequence[Bar], rule: Rule, *, fee_side: float, slip_side: float,
             stop_atr: float = 1.0, take_atr: float = 1.5, max_hold: int = 48,
             warmup: int = 60) -> list[Trade]:
    """Walk the bars once. Entry at the signal bar's close, exits judged on later bars only."""
    trades: list[Trade] = []
    index = max(warmup, 1)
    while index < len(bars) - 1:
        side = rule(bars, index)
        if not side:
            index += 1
            continue
        atr_now = atr(bars, index)
        if atr_now <= 0:
            index += 1
            continue
        signal_bar = bars[index]
        entry = signal_bar.close * (1 + slip_side * side)
        stop = entry - side * stop_atr * atr_now
        take = entry + side * take_atr * atr_now
        exit_price, exit_index, reason = None, None, ""
        cursor = index + 1
        while cursor < len(bars) and cursor - index <= max_hold:
            bar = bars[cursor]
            if (side > 0 and bar.low <= stop) or (side < 0 and bar.high >= stop):
                exit_price, exit_index, reason = stop, cursor, "stop"
                break
            if (side > 0 and bar.high >= take) or (side < 0 and bar.low <= take):
                exit_price, exit_index, reason = take, cursor, "take"
                break
            cursor += 1
        if exit_price is None:
            exit_index = min(index + max_hold, len(bars) - 1)
            if index + max_hold >= len(bars):
                break  # the trade has not closed yet: never invent an exit
            exit_price, reason = bars[exit_index].close, "time-stop"
        gross = side * (exit_price * (1 - slip_side * side) - entry) / entry
        net = gross - 2 * (fee_side + slip_side)
        trades.append(Trade(signal_bar.open_ms, bars[index + 1].open_ms, bars[exit_index].open_ms,
                            side, entry, exit_price, reason, net))
        index = exit_index + 1
    return trades


def metrics(trades: Sequence[Trade]) -> dict:
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for trade in trades:
        equity *= 1 + trade.net
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    return {
        "trades": len(trades),
        "net_pct": round(sum(t.net for t in trades) * 100, 2),
        "win_rate": round(100 * sum(1 for t in trades if t.net > 0) / len(trades), 1) if trades else 0.0,
        "max_drawdown_pct": round(drawdown * 100, 2),
        "last_results": [round(t.net, 5) for t in trades[-3:]],
    }
