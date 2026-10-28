"""Trade simulation and statistics, in R.

A strategy looks at one session of one symbol and returns a Signal (or None):
the bar index whose close fired, the side, the stop and the target rule. The
engine then fills it and walks it bar by bar:

- Fill: the OPEN of the bar after the signal bar (fill="next_open", the honest
  default: you cannot buy at a close you only know once it has printed), or the
  signal bar's close (fill="signal_close", what the skill's own replay assumes).
- Stop first: a bar that touches both the stop and the target counts as a stop.
- Gap through the stop: a bar that opens beyond the stop fills at its open.
- Target: filled at the target price (never better, even on a gap through it).
- Time exit: the close of the last bar before flat-by.
- Costs: a round-trip cost per share, taken off as cost / risk-per-share in R,
  plus an optional slippage in basis points of the entry on each side.

Statistics resample DAYS, not trades: on a strong morning many breakouts fire
together and win or lose together, so treating them as independent would make
the intervals far too narrow.
"""

import math
import random
import statistics


class Signal(object):
    __slots__ = ("idx", "side", "stop", "target_r", "target", "meta")

    def __init__(self, idx, side, stop, target_r=None, target=None, meta=None):
        self.idx, self.side, self.stop = idx, side, stop
        self.target_r, self.target, self.meta = target_r, target, meta or {}


def _end_min(bars, step):
    last = bars[-1]
    return 9 * 60 + 30 + last["slot"] + step


def cents_away(x, long):
    c = round(x * 100, 4)
    return (math.floor(c) if long else math.ceil(c)) / 100.0


def run_trade(bars, sig, step, flat_by_min=15 * 60 + 55, fill="next_open", cost_ps=0.02, slip_bps=0.0,
              min_risk_frac=0.0005):
    """Fill and walk one signal. Returns a trade dict, or None when it cannot be taken
    (no bar to fill on, the fill is already beyond the stop, risk too small)."""
    long = sig.side == "long"
    flat_by = min(flat_by_min, _end_min(bars, step) - 5)      # early close: 5 minutes before it
    # last bar allowed to be held: the last one that ends at or before flat_by
    held = [i for i, b in enumerate(bars) if 9 * 60 + 30 + b["slot"] + step <= flat_by]
    if not held:
        return None
    last = held[-1]
    if fill == "next_open":
        fi = sig.idx + 1
        if fi > last:
            return None
        entry = bars[fi]["o"]
        walk_from = fi            # the fill bar itself can hit stop / target after the open
    else:
        fi = sig.idx
        entry = bars[fi]["c"]
        walk_from = fi + 1
    entry = round(entry, 2)
    stop = sig.stop
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0 or risk < min_risk_frac * entry:
        return None
    if sig.target is not None:
        target = sig.target
        if (target <= entry) if long else (target >= entry):
            return None
    else:
        target = round(entry + sig.target_r * risk, 2) if long else round(entry - sig.target_r * risk, 2)
    outcome, exit_px, exit_i = None, None, None
    for i in range(walk_from, last + 1):
        b = bars[i]
        if long:
            if b["l"] <= stop:
                outcome, exit_px = "stop", (b["o"] if b["o"] < stop else stop)
            elif b["h"] >= target:
                outcome, exit_px = "target", target
        else:
            if b["h"] >= stop:
                outcome, exit_px = "stop", (b["o"] if b["o"] > stop else stop)
            elif b["l"] <= target:
                outcome, exit_px = "target", target
        if outcome:
            exit_i = i
            break
    if outcome is None:
        outcome, exit_px, exit_i = "time", bars[last]["c"], last
    gross = ((exit_px - entry) if long else (entry - exit_px)) / risk
    slip = slip_bps / 1e4 * (entry + exit_px)
    net = gross - (cost_ps + slip) / risk
    return {"side": sig.side, "sig_i": sig.idx, "sig_t": bars[sig.idx]["t"], "entry_t": bars[fi]["t"],
            "entry": entry, "stop": stop, "target": target, "risk": round(risk, 4),
            "risk_pct": round(100 * risk / entry, 3), "exit_t": bars[exit_i]["t"], "exit": round(exit_px, 4),
            "outcome": outcome, "R": round(gross, 4), "R_net": round(net, 4), "meta": sig.meta}


# ------------------------------------------------------------------ stats

def pctl(sorted_xs, q):
    if not sorted_xs:
        return None
    return sorted_xs[min(len(sorted_xs) - 1, max(0, int(q * len(sorted_xs))))]


def max_drawdown(rs):
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def summarize(trades, key="R_net", reps=2000, seed=7):
    """Point estimates plus day-clustered bootstrap 95 % intervals."""
    if not trades:
        return {"n": 0}
    xs = [t[key] for t in trades]
    by_day = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t[key])
    days = sorted(by_day)
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x <= 0]
    out = {
        "n": len(xs), "days_with_trades": len(days),
        "win_rate": len(wins) / len(xs),
        "avg_R": statistics.mean(xs),
        "median_R": statistics.median(xs),
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "total_R": sum(xs),
        "outcomes": {o: sum(1 for t in trades if t["outcome"] == o) for o in ("target", "stop", "time")},
    }
    # day-clustered bootstrap of the mean R per trade
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        tot = cnt = 0
        for _ in range(len(days)):
            v = by_day[days[rng.randrange(len(days))]]
            tot += sum(v)
            cnt += len(v)
        means.append(tot / cnt)
    means.sort()
    out["avg_R_ci95"] = [pctl(means, 0.025), pctl(means, 0.975)]
    out["p_avg_R_le_0"] = sum(1 for m in means if m <= 0) / len(means)
    # robustness: drop the 5 best trades
    srt = sorted(xs)
    out["avg_R_without_top5"] = statistics.mean(srt[:-5]) if len(srt) > 10 else None
    return out


def daily_series(trades, key="R_net", one_per_day=False):
    """R per day in date order. one_per_day: only the first trade of each day
    (by signal time), as a trader picking one setup a day would."""
    by_day = {}
    for t in sorted(trades, key=lambda t: (t["date"], t["sig_t"], t["symbol"])):
        if one_per_day and t["date"] in by_day:
            continue
        by_day.setdefault(t["date"], []).append(t[key])
    if one_per_day:
        return [(d, v[0]) for d, v in sorted(by_day.items())]
    return [(d, statistics.mean(v)) for d, v in sorted(by_day.items())]


def dd_distribution(day_rs, reps=2000, block=5, seed=11):
    """Max drawdown (in R) of the daily series, block-bootstrapped (runs of `block`
    consecutive days), as percentiles: what a bad stretch of this length looks like."""
    if len(day_rs) < block * 2:
        return None
    rng = random.Random(seed)
    n = len(day_rs)
    dds = []
    for _ in range(reps):
        seq = []
        while len(seq) < n:
            s = rng.randrange(n - block + 1)
            seq.extend(day_rs[s:s + block])
        dds.append(max_drawdown(seq[:n]))
    dds.sort()
    return {"p50": pctl(dds, 0.5), "p95": pctl(dds, 0.05), "observed": max_drawdown(day_rs)}
