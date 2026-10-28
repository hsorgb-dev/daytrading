"""S&P 500 Open Playbook v3.4 (the S&P part), as a backtest.

    python3 -m backtest.open_playbook --symbol ES=F                     # Yahoo, 60 days, ES futures
    python3 -m backtest.open_playbook --symbol SPY                      # Yahoo, SPY with pre-market, points scaled
    python3 -m backtest.open_playbook --symbol SPY --source alpaca --start 2021-01-01 --end 2026-10-27

Rules (from the playbook table; points are ES points, scaled by --scale for SPY):
- 5-minute bars, New York time. Pre-open range (POR) = bars starting 08:30..09:25.
- Trading window 09:30-09:55, last signal bar 09:50. One trade per session.
- Continuation: a bar that CLOSES beyond the POR arms the setup (it is not the
  entry). The entry bar is the next or the one after it, and must
  close beyond the POR in the trade's direction, be a candle in that direction
  with a body >= 50 % of its range, close on the right side of the EMA20 and no
  more than `max_dist` (4) points from the POR level. Entry at its close.
- Rejection: price has traded outside the POR and a bar CLOSES back inside:
  under the low and back -> long, over the high and back -> short. Entry at the
  close of that bar.
- Stop: the extreme of the entry bar plus 1 point buffer.
- Exits: 50 % at 2R, 50 % at 3R; optional break-even from 1R; flat 15:55.
- Same bar touches stop and a target: the stop counts (conservative). A bar
  that opens through the stop fills at its open. Break-even moves the stop only
  from the bar AFTER the one that reached 1R.

Not specified in v3.4 and therefore left open here (parameters, off by default):
the "Abstands- und Risikofilter" beyond the 4-point rule (--max-risk caps the
initial risk in points) and the rejection "Qualitätsfilter". Results with
--max-risk are reported as a variant.
"""

import argparse
import json
import os
import statistics
import sys

from . import data, engine

POR_START, POR_END = "08:30", "09:25"          # bar start times
WIN_FIRST, WIN_LAST = "09:30", "09:50"         # signal bars
FLAT_LAST_BAR = "15:50"                         # its close is 15:55


def ema_series(closes, n=20):
    k = 2.0 / (n + 1)
    out, e = [], None
    for c in closes:
        e = c if e is None else e + k * (c - e)
        out.append(e)
    return out


def sessions(bars):
    """Index ranges per New York date: {date: [indices into bars]}."""
    by = {}
    for i, b in enumerate(bars):
        by.setdefault(b["d"], []).append(i)
    return by


def find_signal(bars, idx, ema, scale, p):
    """The first signal of one session, or None. idx: indices of that date's bars."""
    day = [i for i in idx]
    por = [bars[i] for i in day if POR_START <= bars[i]["t"] <= POR_END]
    if len(por) < p["min_por_bars"]:
        return None, "pre-open range incomplete (%d bars)" % len(por)
    ph, pl = max(b["h"] for b in por), min(b["l"] for b in por)
    win = [i for i in day if WIN_FIRST <= bars[i]["t"] <= WIN_LAST]
    if not win or bars[win[0]]["t"] != WIN_FIRST:
        return None, "no 09:30 bar"
    buf, maxd = p["buffer"] * scale, p["max_dist"] * scale
    armed = {"long": None, "short": None}   # position in win of the arming bar
    was_below = was_above = False
    for k, i in enumerate(win):
        b = bars[i]
        prev_c = bars[i - 1]["c"]
        rng = b["h"] - b["l"]
        body = abs(b["c"] - b["o"])
        # ---- rejection (entry at this bar's close)
        if p["rejection"]:
            pen = p["rej_min_pen"] * scale
            if b["l"] < pl - pen:
                was_below = True
            if b["h"] > ph + pen:
                was_above = True
            inside = pl < b["c"] < ph
            if inside and was_below and (b["l"] < pl or prev_c < pl):
                return {"k": i, "side": "long", "setup": "rejection", "entry": b["c"],
                        "stop": b["l"] - buf, "por": (ph, pl)}, None
            if inside and was_above and (b["h"] > ph or prev_c > ph):
                return {"k": i, "side": "short", "setup": "rejection", "entry": b["c"],
                        "stop": b["h"] + buf, "por": (ph, pl)}, None
        # ---- continuation entry (needs an arming bar one or two bars back)
        if p["continuation"]:
            for side in ("long", "short"):
                a = armed[side]
                if a is None or not (1 <= k - a <= 2):
                    continue
                if side == "long":
                    ok = (b["c"] > b["o"] and rng > 0 and body >= 0.5 * rng and b["c"] > ph
                          and (not p["ema"] or b["c"] > ema[i]) and b["c"] - ph <= maxd)
                else:
                    ok = (b["c"] < b["o"] and rng > 0 and body >= 0.5 * rng and b["c"] < pl
                          and (not p["ema"] or b["c"] < ema[i]) and pl - b["c"] <= maxd)
                if ok:
                    stop = b["l"] - buf if side == "long" else b["h"] + buf
                    return {"k": i, "side": side, "setup": "continuation", "entry": b["c"], "stop": stop,
                            "por": (ph, pl)}, None
            # ---- arming: the first close beyond the range (a bar can arm only, not enter)
            if armed["long"] is None and b["c"] > ph:
                armed["long"] = k
            if armed["short"] is None and b["c"] < pl:
                armed["short"] = k
    return None, "no signal"


def manage(bars, idx, sig, p, scale, cost_pts):
    """Walk the trade bar by bar after the entry bar. Returns R (gross and net) and details."""
    long = sig["side"] == "long"
    e, s0 = sig["entry"], sig["stop"]
    risk = (e - s0) if long else (s0 - e)
    if risk <= 0:
        return None
    if p["max_risk"] and risk > p["max_risk"] * scale:
        return {"skipped": "risk %.2f > max" % (risk / scale)}
    lvl = lambda r: e + r * risk if long else e - r * risk
    tp1, tp2, be_lvl = lvl(p["tp1"]), lvl(p["tp2"]), lvl(1.0)
    stop = s0
    half1 = half2 = None        # R of each half once closed
    be_armed_next = False
    after = [i for i in idx if bars[i]["t"] > bars[sig["k"]]["t"] and bars[i]["t"] <= FLAT_LAST_BAR]
    exit_t = None
    for i in after:
        b = bars[i]
        if be_armed_next:
            stop = e
            be_armed_next = False
        hit_stop = (b["l"] <= stop) if long else (b["h"] >= stop)
        if hit_stop:
            px = (min(b["o"], stop) if long else max(b["o"], stop))
            r = ((px - e) if long else (e - px)) / risk
            if half1 is None:
                half1 = r
            half2 = r
            exit_t = b["t"]
            break
        fav = b["h"] if long else b["l"]
        reach = (lambda L: fav >= L) if long else (lambda L: fav <= L)
        if half1 is None and reach(tp1):
            half1 = p["tp1"]
        if half1 is not None and reach(tp2):
            half2 = p["tp2"]
            exit_t = b["t"]
            break
        if p["breakeven"] and stop == s0 and reach(be_lvl):
            be_armed_next = True
    if half2 is None:            # time exit at the 15:50 bar's close (15:55)
        last = bars[after[-1]] if after else bars[sig["k"]]
        r = ((last["c"] - e) if long else (e - last["c"])) / risk
        if half1 is None:
            half1 = r
        half2 = r
        exit_t = last["t"]
    gross = 0.5 * half1 + 0.5 * half2
    net = gross - cost_pts * scale / risk
    outcome = ("tp2" if half2 == p["tp2"] else "tp1+stop" if half1 == p["tp1"] and half2 < p["tp1"]
               else "tp1+time" if half1 == p["tp1"] else "stop" if half2 < 0 or (p["breakeven"] and half2 == 0)
               else "time")
    return {"R": round(gross, 4), "R_net": round(net, 4), "risk_pts": round(risk / scale, 2), "exit_t": exit_t,
            "outcome": outcome}


DEFAULTS = dict(min_por_bars=10, rej_min_pen=0.0, buffer=1.0, max_dist=4.0, ema=True, continuation=True, rejection=True,
                tp1=2.0, tp2=3.0, breakeven=False, max_risk=None)


def backtest(bars, p, scale, cost_pts, start=None, end=None):
    ema = ema_series([b["c"] for b in bars])
    by = sessions(bars)
    trades, reasons = [], {}
    for d in sorted(by):
        if (start and d < start) or (end and d > end):
            continue
        idx = by[d]
        if not any(bars[i]["t"] == "09:30" for i in idx):
            continue                 # not a cash session (weekend, holiday)
        sig, why = find_signal(bars, idx, ema, scale, p)
        if sig is None:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        res = manage(bars, idx, sig, p, scale, cost_pts)
        if res is None or "skipped" in res:
            reasons["risk filter" if res else "zero risk"] = reasons.get("risk filter" if res else "zero risk", 0) + 1
            continue
        res.update(date=d, symbol="", sig_t=bars[sig["k"]]["t"], side=sig["side"], setup=sig["setup"],
                   entry=round(sig["entry"], 2), stop=round(sig["stop"], 2),
                   por_high=round(sig["por"][0], 2), por_low=round(sig["por"][1], 2))
        trades.append(res)
    return trades, reasons


def group(trades, key):
    out = {}
    for t in trades:
        out.setdefault(key(t), []).append(t["R_net"])
    return {k: {"n": len(v), "avg_R": round(statistics.mean(v), 3),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3)} for k, v in sorted(out.items())}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ES=F")
    ap.add_argument("--source", choices=["yahoo", "alpaca"], default="yahoo")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--scale", type=float, help="price units per ES point (default 1 for ES, 0.1 for SPY)")
    ap.add_argument("--cost", type=float, default=0.5, help="round-trip cost in ES points (default 0.5: "
                    "commission + 1 tick slippage each side)")
    ap.add_argument("--cache", default="bt_cache")
    ap.add_argument("--out", default="bt_results/open_playbook")
    ap.add_argument("--oos-from")
    ap.add_argument("--min-por-bars", type=int, default=10, help="bars needed in the 08:30-09:25 range (of 12)")
    a = ap.parse_args(argv)
    DEFAULTS["min_por_bars"] = a.min_por_bars
    scale = a.scale or (1.0 if a.symbol.upper().startswith(("ES", "MES")) else 0.1)
    if a.source == "yahoo":
        bars = data.yahoo_bars_ext(a.symbol, cache=a.cache)
    else:
        bars = data.alpaca_bars_ext(a.symbol, a.start, a.end, cache=a.cache)

    variants = [
        ("Basis v3.4 (ohne BE, ohne Risikofilter)", {}, a.cost),
        ("Break-even ab 1R", {"breakeven": True}, a.cost),
        ("Ohne Kosten", {}, 0.0),
        ("Kosten-Stress (doppelt)", {}, 2 * a.cost),
        ("Nur Continuation", {"rejection": False}, a.cost),
        ("Nur Rejection", {"continuation": False}, a.cost),
        ("Continuation ohne EMA20-Filter", {"rejection": False, "ema": False}, a.cost),
        ("Max. Risiko 10 Punkte", {"max_risk": 10.0}, a.cost),
        ("Max. Abstand 2 Punkte", {"max_dist": 2.0}, a.cost),
        ("Max. Abstand 6 Punkte", {"max_dist": 6.0}, a.cost),
        ("Ziele 1,5R / 2,5R", {"tp1": 1.5, "tp2": 2.5}, a.cost),
        ("Rejection: Durchstoß ≥ 2 Punkte", {"rej_min_pen": 2.0}, a.cost),
        ("Rejection: Durchstoß ≥ 4 Punkte", {"rej_min_pen": 4.0}, a.cost),
        ("Durchstoß ≥ 2 Pkt + max. Risiko 10", {"rej_min_pen": 2.0, "max_risk": 10.0}, a.cost),
        ("Puffer 0,5 Punkte", {"buffer": 0.5}, a.cost),
        ("Puffer 2 Punkte", {"buffer": 2.0}, a.cost),
    ]
    res = {"symbol": a.symbol, "source": a.source, "scale": scale, "cost_pts": a.cost, "variants": []}
    base = None
    for name, over, cost in variants:
        p = dict(DEFAULTS, **over)
        tr, why = backtest(bars, p, scale, cost, a.start, a.end)
        for t in tr:
            t["symbol"] = a.symbol
        s = engine.summarize(tr)
        res["variants"].append(dict(name=name, params=over, cost=cost, no_trade=why, **s))
        if base is None:
            base, base_why = tr, why
    days = sorted({t["date"] for t in base} | set())
    cash_days = sorted(d for d, idx in sessions(bars).items() if any(bars[i]["t"] == "09:30" for i in idx)
                       and (not a.start or d >= a.start) and (not a.end or d <= a.end))
    res["cash_sessions"] = len(cash_days)
    res["first_day"], res["last_day"] = cash_days[0], cash_days[-1]
    res["by_setup"] = group(base, lambda t: "%s %s" % (t["setup"], t["side"]))
    res["by_outcome"] = group(base, lambda t: t["outcome"])
    res["by_signal_time"] = group(base, lambda t: t["sig_t"])
    res["by_year"] = group(base, lambda t: t["date"][:4])
    res["risk_pts"] = {"median": statistics.median(t["risk_pts"] for t in base) if base else None,
                       "p90": sorted(t["risk_pts"] for t in base)[int(0.9 * len(base))] if base else None}
    series = [t["R_net"] for t in sorted(base, key=lambda t: t["date"])]
    res["drawdown"] = engine.dd_distribution(series)
    res["equity"] = [(t["date"], t["R_net"]) for t in sorted(base, key=lambda t: t["date"])]
    if a.oos_from:
        res["in_sample"] = engine.summarize([t for t in base if t["date"] < a.oos_from])
        res["out_of_sample"] = engine.summarize([t for t in base if t["date"] >= a.oos_from])
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(res, f, indent=1)
    with open(os.path.join(a.out, "trades.csv"), "w") as f:
        cols = ["date", "sig_t", "setup", "side", "entry", "stop", "risk_pts", "por_high", "por_low", "exit_t",
                "outcome", "R", "R_net"]
        f.write(",".join(cols) + "\n")
        for t in sorted(base, key=lambda t: t["date"]):
            f.write(",".join(str(t[c]) for c in cols) + "\n")

    print("%s (%s): %d cash sessions %s..%s, scale %.2f, cost %.2f pts" % (
        a.symbol, a.source, len(cash_days), res["first_day"], res["last_day"], scale, a.cost))
    print("%-40s %4s %6s %7s %17s %5s" % ("Variante", "n", "win%", "avg R", "95%-KI", "PF"))
    for v in res["variants"]:
        if not v.get("n"):
            print("%-40s %4d" % (v["name"], 0))
            continue
        ci = v["avg_R_ci95"]
        print("%-40s %4d %5.1f%% %+7.3f [%+.3f, %+.3f] %5s" % (
            v["name"][:40], v["n"], 100 * v["win_rate"], v["avg_R"], ci[0], ci[1],
            "%.2f" % v["profit_factor"] if v["profit_factor"] else "-"))
    print("no trade:", base_why)


if __name__ == "__main__":
    main()
