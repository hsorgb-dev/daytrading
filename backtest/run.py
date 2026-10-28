"""Backtest runner.

    python3 -m backtest.run skill_orb --source yahoo                       # 60 days of 5-minute bars
    python3 -m backtest.run skill_orb --source alpaca --start 2021-01-01 --end 2026-10-27
    python3 -m backtest.run skill_orb --symbols SPY,QQQ --out results/spy

Writes <out>/summary.json, <out>/trades.csv and prints a short table.
"""

import argparse
import csv
import json
import os
import random
import statistics
import sys

from . import data, engine, strategies
from .universe import LIQUID_100, INDEX_ETFS


def load(symbols, a, log):
    out, failed = {}, {}
    for i, s in enumerate(symbols):
        try:
            if a.source == "yahoo":
                out[s] = data.yahoo_bars(s, a.interval, a.range, cache=a.cache)
            else:
                raw = data.alpaca_bars(s, a.start, a.end, "1m" if a.interval == "1m" else "5m",
                                       feed=a.feed, cache=a.cache)
                out[s] = raw
        except data.DataError as e:
            failed[s] = str(e)
        if log and (i + 1) % 20 == 0:
            print("  loaded %d/%d" % (i + 1, len(symbols)), file=sys.stderr)
    return out, failed


def run_rule(bars_by_sym, step, rule_kw, fill, cost, slip, dates=None):
    trades = []
    for sym, days in bars_by_sym.items():
        for d, bars in days.items():
            if dates and d not in dates:
                continue
            sig = strategies.skill_orb(bars, step, **rule_kw)
            if sig is None:
                continue
            t = engine.run_trade(bars, sig, step, fill=fill, cost_ps=cost, slip_bps=slip)
            if t is None:
                continue
            t.update(symbol=sym, date=d)
            trades.append(t)
    return trades


def run_placebo(bars_by_sym, step, trades, fill, cost, slip, draws=20, seed=3):
    """Benchmark for each real trade: `draws` longs on the same symbol, at the same
    signal time, with the same stop distance (in %) and the same R target, but on
    randomly drawn OTHER sessions, with no breakout condition. Same time of day,
    same holding window, same risk: the only thing missing is the setup. If the
    setup has an edge, the real trades beat this."""
    rng = random.Random(seed)
    out = []
    for t in trades:
        days = bars_by_sym[t["symbol"]]
        others = [d for d in days if d != t["date"] and strategies.session_ok(days[d], step)]
        if not others:
            continue
        frac = t["risk"] / t["entry"]
        for _ in range(draws):
            d = rng.choice(others)
            bars = days[d]
            i = next((k for k, b in enumerate(bars) if b["t"] == t["sig_t"]), None)
            if i is None:
                continue
            ref = bars[i + 1]["o"] if fill == "next_open" and i + 1 < len(bars) else bars[i]["c"]
            sig = engine.Signal(i, "long", engine.cents_away(ref * (1 - frac), True), target_r=t["target"] and
                                round((t["target"] - t["entry"]) / t["risk"], 3))
            p = engine.run_trade(bars, sig, step, fill=fill, cost_ps=cost, slip_bps=slip)
            if p:
                p.update(symbol=t["symbol"], date=d)
                out.append(p)
    return out


def breakdown(trades, keyfn, labels=None):
    groups = {}
    for t in trades:
        groups.setdefault(keyfn(t), []).append(t["R_net"])
    rows = []
    for k in (labels or sorted(groups)):
        v = groups.get(k)
        if v:
            rows.append({"group": k, "n": len(v), "avg_R": round(statistics.mean(v), 3),
                         "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3)})
    return rows


def time_bucket(t):
    h, m = map(int, t["entry_t"].split(":"))
    x = h * 60 + m
    return "a) bis 10:00" if x < 600 else "b) 10:00-10:59" if x < 660 else "c) 11:00-12:59" if x < 780 else "d) ab 13:00"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("strategy", choices=["skill_orb"])
    p.add_argument("--source", choices=["yahoo", "alpaca"], default="yahoo")
    p.add_argument("--interval", default="5m")
    p.add_argument("--range", default="60d")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--feed", default="sip")
    p.add_argument("--symbols", help="comma list; default: the liquid 100")
    p.add_argument("--cache", default="bt_cache")
    p.add_argument("--out", default="bt_results/skill_orb")
    p.add_argument("--cost", type=float, default=0.02, help="round-trip $ per share")
    p.add_argument("--oos-from", help="YYYY-MM-DD: report in-sample before / out-of-sample from this date")
    a = p.parse_args(argv)

    symbols = a.symbols.split(",") if a.symbols else LIQUID_100
    step = int(a.interval.rstrip("m")) if a.source == "yahoo" else 5
    base_kw = dict(or_minutes=15, stop_rule="or_mid", target_r=2.0)
    variants = [
        ("Basis: Skill-Regel, Fill nächster Open, 0,02 $", base_kw, "next_open", a.cost, 0),
        ("Fill zum Signal-Close (wie Skill-Replay)", base_kw, "signal_close", a.cost, 0),
        ("Ohne Kosten", base_kw, "next_open", 0.0, 0),
        ("Kosten-Stress: 0,04 $ + 2 bp Slippage", base_kw, "next_open", 2 * a.cost, 2.0),
        ("Ziel 1R", dict(base_kw, target_r=1.0), "next_open", a.cost, 0),
        ("Ziel 1,5R", dict(base_kw, target_r=1.5), "next_open", a.cost, 0),
        ("Ziel 3R", dict(base_kw, target_r=3.0), "next_open", a.cost, 0),
        ("Stop am Range-Tief", dict(base_kw, stop_rule="or_low"), "next_open", a.cost, 0),
        ("Range 5 Min", dict(base_kw, or_minutes=5), "next_open", a.cost, 0),
        ("Range 30 Min", dict(base_kw, or_minutes=30), "next_open", a.cost, 0),
        ("Volumen ≥ 1,5x Range-Schnitt", dict(base_kw, min_volume_x=1.5), "next_open", a.cost, 0),
    ]
    slim = lambda t: {"date": t["date"], "R_net": t["R_net"], "outcome": t["outcome"]}
    # one symbol at a time: load, run every variant and the benchmark, let the bars go.
    # Holding six years of 5-minute bars for 99 stocks at once needs more memory than we have.
    per_variant = [[] for _ in variants]
    pl, failed, loaded, all_days = [], {}, 0, set()
    for n, sym in enumerate(symbols):
        got, f = load([sym], a, False)
        failed.update(f)
        if sym not in got:
            continue
        bars1 = got
        if a.source == "alpaca" and a.interval == "1m":
            bars1 = {sym: {d: data.resample(b, 5) for d, b in got[sym].items()}}
        loaded += 1
        all_days.update(bars1[sym])
        for vi, (name, kw, fill, cost, slip) in enumerate(variants):
            tr = run_rule(bars1, step, kw, fill, cost, slip)
            per_variant[vi].extend(tr if vi == 0 else [slim(t) for t in tr])
            if vi == 0:
                pl.extend(slim(t) for t in run_placebo(bars1, step, tr, "next_open", a.cost, 0, seed=3 + n))
        del got, bars1
        if (n + 1) % 10 == 0:
            print("  %d/%d symbols done" % (n + 1, len(symbols)), file=sys.stderr, flush=True)
    all_days = sorted(all_days)
    bars = {}
    results = {"source": a.source, "interval": a.interval, "symbols": len(symbols), "loaded": loaded,
               "failed": failed, "first_day": all_days[0] if all_days else None,
               "last_day": all_days[-1] if all_days else None, "sessions": len(all_days), "variants": []}
    for (name, kw, fill, cost, slip), tr in zip(variants, per_variant):
        results["variants"].append({"name": name, "rule": kw, "fill": fill, "cost": cost, "slip_bps": slip,
                                    **engine.summarize(tr)})
    base_trades = per_variant[0]
    results["placebo"] = engine.summarize(pl)
    results["placebo"]["draws_per_trade"] = 20

    # breakdowns of the base run
    spy = bars.get("SPY")
    if spy is None:
        extra, _ = load(["SPY"], a, False)
        spy = extra.get("SPY") or {}
        if a.source == "alpaca" and a.interval == "1m" and spy:
            spy = {d: data.resample(b, 5) for d, b in spy.items()}
    def spy_day(t):
        b = spy.get(t["date"])
        if not b:
            return "?"
        return "SPY-Tag grün" if b[-1]["c"] > b[0]["o"] else "SPY-Tag rot"
    results["by_entry_time"] = breakdown(base_trades, time_bucket)
    results["by_or_width"] = breakdown(base_trades, lambda t: "eng (<0,8 %)" if t["meta"]["or_pct"] < 0.8
                                       else "mittel (0,8-1,5 %)" if t["meta"]["or_pct"] < 1.5 else "weit (>1,5 %)",
                                       ["eng (<0,8 %)", "mittel (0,8-1,5 %)", "weit (>1,5 %)"])
    results["by_month"] = breakdown(base_trades, lambda t: t["date"][:7])
    if spy:
        results["by_spy_day_hindsight"] = breakdown(base_trades, spy_day)
    if a.oos_from:
        ins = [t for t in base_trades if t["date"] < a.oos_from]
        oos = [t for t in base_trades if t["date"] >= a.oos_from]
        results["in_sample"] = engine.summarize(ins)
        results["out_of_sample"] = engine.summarize(oos)
    by_year = {}
    for t in base_trades:
        by_year.setdefault(t["date"][:4], []).append(t)
    results["by_year"] = {y: engine.summarize(v, reps=500) for y, v in sorted(by_year.items())}

    # one trade a day (the first trigger), as a trader would take it
    one = engine.daily_series(base_trades, one_per_day=True)
    results["one_per_day"] = {"days": len(one), "avg_R": statistics.mean(r for _, r in one) if one else None,
                              "total_R": sum(r for _, r in one),
                              "drawdown": engine.dd_distribution([r for _, r in one])}
    port = engine.daily_series(base_trades)
    results["equal_weight_daily"] = {"days": len(port), "total_R": sum(r for _, r in port),
                                     "drawdown": engine.dd_distribution([r for _, r in port])}
    results["equity_one_per_day"] = one

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(results, f, indent=1, default=str)
    with open(os.path.join(a.out, "trades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        cols = ["date", "symbol", "sig_t", "entry_t", "entry", "stop", "target", "risk_pct", "exit_t", "exit",
                "outcome", "R", "R_net"]
        w.writerow(cols + ["or_pct"])
        for t in sorted(base_trades, key=lambda t: (t["date"], t["sig_t"], t["symbol"])):
            w.writerow([t[c] for c in cols] + [t["meta"]["or_pct"]])

    print("%s: %d/%d symbols, %d sessions %s..%s" % (a.source, loaded, len(symbols), len(all_days),
                                                     results["first_day"], results["last_day"]))
    print("%-42s %6s %6s %7s %17s %6s" % ("Variante", "n", "win%", "avg R", "95%-KI (Tage)", "PF"))
    rows = results["variants"] + [dict(results["placebo"], name="Benchmark: gleiche Zeit, Zufallstag")]
    for v in rows:
        if not v.get("n"):
            print("%-42s %6s" % (v["name"][:42], 0))
            continue
        ci = v["avg_R_ci95"]
        print("%-42s %6d %5.1f%% %+7.3f [%+.3f, %+.3f] %6s" % (
            v["name"][:42], v["n"], 100 * v["win_rate"], v["avg_R"], ci[0], ci[1],
            "%.2f" % v["profit_factor"] if v["profit_factor"] else "-"))
    if failed:
        print("failed:", ", ".join(sorted(failed)))


if __name__ == "__main__":
    main()
