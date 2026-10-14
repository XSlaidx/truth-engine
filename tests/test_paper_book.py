"""Paper book: closed bars only, costs on both sides, no invented exits, no lookahead."""
import paper_book
from paper_book import Bar


def flat(count, price=100.0, interval=3600_000, start=1_600_000_000_000):
    return [Bar(start + i * interval, price, price, price, price) for i in range(count)]


def test_forming_bar_is_dropped():
    bars = flat(5)
    now = bars[-1].open_ms + 60_000          # the last candle is still being written
    assert len(paper_book.drop_forming(bars, 3600_000, now)) == 4
    now = bars[-1].open_ms + 3600_000        # the last candle has closed
    assert len(paper_book.drop_forming(bars, 3600_000, now)) == 5


def test_costs_are_charged_on_both_sides():
    import math
    start = 1_600_000_000_000
    prices = [100 + 5 * math.sin(i / 4) for i in range(160)]     # oscillates: the rule trades
    bars = [Bar(start + i * 3600_000, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
    rule = paper_book.ema_cross(3, 8)
    free = paper_book.simulate(bars, rule, fee_side=0.0, slip_side=0.0)
    costly = paper_book.simulate(bars, rule, fee_side=0.0005, slip_side=0.0002)
    assert free and len(free) == len(costly)
    assert sum(t.net for t in costly) < sum(t.net for t in free)
    # commission is charged twice per trade; slippage always drags in the same direction,
    # but it also enters the denominator (entry price), so compare with a small tolerance
    assert all(t.net <= free[i].net - 2 * 0.0005 + 1e-6 for i, t in enumerate(costly))
    assert all(t.net < free[i].net for i, t in enumerate(costly))


def test_stop_and_take_are_honoured():
    start = 1_600_000_000_000
    bars = [Bar(start + i * 3600_000, 100, 101, 99, 100) for i in range(70)]
    # entry on bar 60 (rule returns +1 exactly there), then a dive that hits the stop
    bars[60] = Bar(start + 60 * 3600_000, 100, 101, 99, 100)
    bars[61] = Bar(start + 61 * 3600_000, 100, 100, 50, 60)
    rule = lambda b, i: 1 if i == 60 else 0          # noqa: E731 - deliberately explicit
    trades = paper_book.simulate(bars, rule, fee_side=0.0, slip_side=0.0, stop_atr=1.0)
    assert len(trades) == 1
    assert trades[0].exit_reason == "stop" and trades[0].net < 0

    bars[61] = Bar(start + 61 * 3600_000, 100, 200, 100, 150)
    trades = paper_book.simulate(bars, rule, fee_side=0.0, slip_side=0.0, take_atr=1.0)
    assert trades[0].exit_reason == "take" and trades[0].net > 0


def test_time_stop_closes_the_trade():
    start = 1_600_000_000_000
    bars = [Bar(start + i * 3600_000, 100, 100.5, 99.5, 100) for i in range(120)]
    rule = lambda b, i: 1 if i == 60 else 0          # noqa: E731
    trades = paper_book.simulate(bars, rule, fee_side=0.0, slip_side=0.0, max_hold=10)
    assert len(trades) == 1 and trades[0].exit_reason == "time-stop"
    assert trades[0].exit_ms == bars[70].open_ms


def test_no_exit_is_invented_for_an_open_trade():
    start = 1_600_000_000_000
    bars = [Bar(start + i * 3600_000, 100, 100.5, 99.5, 100) for i in range(70)]
    rule = lambda b, i: 1 if i == 68 else 0          # noqa: E731
    assert paper_book.simulate(bars, rule, fee_side=0.0, slip_side=0.0, max_hold=48) == []


def test_rule_cannot_see_the_future():
    """The interface promises bars[:index+1]; prove the shipped rule obeys it."""
    bars = [Bar(1_600_000_000_000 + i * 3600_000, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(90)]
    rule = paper_book.ema_cross(5, 20)
    for index in range(30, len(bars)):
        assert rule(bars[:index + 1], index) == rule(bars, index)


def test_metrics_track_drawdown():
    trades = [paper_book.Trade(1, 2, 3, 1, 100, 101, "take", 0.05),
              paper_book.Trade(4, 5, 6, -1, 100, 110, "stop", -0.2),
              paper_book.Trade(7, 8, 9, 1, 100, 102, "take", 0.01)]
    report = paper_book.metrics(trades)
    assert report["trades"] == 3
    assert report["net_pct"] == -14.0
    assert report["max_drawdown_pct"] > 0
    assert report["win_rate"] == 66.7
