"""Darvas box strategy on daily bars built from the cached Alpaca 5-minute bars.

Rules fixed BEFORE the test (Nicolas Darvas, "How I Made $2,000,000 in the
Stock Market", as commonly formalized):

Box (per stock, daily bars):
  1. Top: a high that is not exceeded on the next `n_conf` days (3).
  2. Bottom: after the top is set, the lowest low that is not undercut on the
     next `n_conf` days, while no high exceeds the top. A high above the top
     before the bottom is confirmed starts a new top.
  3. The box is complete once both are confirmed.
Entry:
  - Long only. A buy stop one cent above the box top, from the day after the box
    completes. Fill at the higher of the day's open and top + 0.01.
  - A day that both breaks the top and undercuts the bottom is ambiguous and
    is not taken. A low under the bottom before a breakout voids the box.
  - Filter (classic): the box top is a new 52-week high (above every high of
    the 252 sessions before the top day).
Exit:
  - Initial stop one cent under the box bottom. Each later completed box while
    in the trade raises the stop to one cent under its bottom (never lowers).
  - A day opening under the stop fills at the open, otherwise at the stop. An
    entry day that also touches the stop counts as stopped (conservative).
  - No target. Open trades at the end of the data close at the last close.
Costs: $0.02 a share round trip plus 2 basis points slippage on each side.

Pre-registered variants (24): n_conf 3 / 5, 52-week filter on / off, maximum
box depth none / 10 % / 20 %, market filter off / SPY above its 200-day average.
Search years 2022-2023 (2021 is the 52-week warm-up), test 2024-2026 once.

    python3 -m backtest.research.darvas --cache bt_cache --out bt_results/darvas
"""

import argparse
import gzip
import glob
import itertools
import json
import os
import random

import numpy as np
import pandas as pd

from .. import data

COST_PS, SLIP = 0.02, 0.0002
IS_START, IS_END, OOS_START = "2022-01-01", "2023-12-31", "2024-01-01"


# ------------------------------------------------------------------ daily bars

def daily_from_5m(path):
    with gzip.open(path, "rt") as f:
        days = json.load(f)
    rows = []
    for d in sorted(days):
        b = days[d]
        if not b or b[0]["slot"] != 0:
            continue
        rows.append((d, b[0]["o"], max(x["h"] for x in b), min(x["l"] for x in b), b[-1]["c"], sum(x["v"] for x in b)))
    return pd.DataFrame(rows, columns=["date", "o", "h", "l", "c", "v"])


def spy_daily(cache):
    p = glob.glob(os.path.join(cache, "alpacaext-sip-SPY-5m-*.json.gz"))[0]
    with gzip.open(p, "rt") as f:
        bars = json.load(f)
    df = pd.DataFrame(bars)
    df = df[(df.t >= "09:30") & (df.t <= "15:55")]
    g = df.groupby("d").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index()
    g = g.rename(columns={"d": "date"})
    g["sma200"] = g.c.rolling(200).mean()
    g["mkt_ok"] = (g.c > g.sma200).shift(1).fillna(False)     # known before the open
    return g


# ------------------------------------------------------------------ the rules

def darvas_trades(df, sym, n_conf=3, need_52w=True, max_depth=None, mkt_ok=None):
    """Walk one stock day by day. Returns a list of trade dicts."""
    D, O, H, L, C = df.date.to_numpy(), df.o.to_numpy(), df.h.to_numpy(), df.l.to_numpy(), df.c.to_numpy()
    n = len(df)
    trades = []
    state, top, top_i, cnt, bot, cnt_b = "top", H[0], 0, 0, None, 0
    box = None                       # (top, bottom, top_i) of the last completed box
    pos = None                       # open trade

    def start_top(i):
        return "top", H[i], i, 0, None, 0

    for i in range(1, n):
        # ---------------- position management first (stop from yesterday's boxes)
        if pos is not None:
            if L[i] <= pos["stop"]:
                fill = min(O[i], pos["stop"])
                pos.update(exit=fill, exit_date=D[i], exit_i=i, outcome="stop")
                trades.append(pos)
                pos = None
        # ---------------- entry on a completed box (buy stop above its top)
        if box is not None and state == "box":
            btop, bbot, bti = box
            broke_up, broke_dn = H[i] > btop, L[i] < bbot
            if broke_up and not broke_dn:
                ok = pos is None
                if ok and need_52w:
                    lo = max(0, bti - 252)
                    ok = bti >= 252 and btop > H[lo:bti].max()
                if ok and max_depth is not None:
                    ok = (btop - bbot) / btop <= max_depth
                if ok and mkt_ok is not None:
                    ok = bool(mkt_ok.get(D[i], False))
                if ok:
                    entry = max(O[i], round(btop + 0.01, 2))
                    stop = round(bbot - 0.01, 2)
                    risk = entry - stop
                    if risk > 0:
                        pos = dict(sym=sym, entry_date=D[i], entry_i=i, entry=entry, stop=stop, stop0=stop,
                                   risk=risk, depth=(btop - bbot) / btop)
                        if L[i] <= stop:                       # same-day stop, conservative
                            pos.update(exit=stop, exit_date=D[i], exit_i=i, outcome="stop")
                            trades.append(pos)
                            pos = None
                state, top, top_i, cnt, bot, cnt_b = start_top(i)
                box = None
                continue
            if broke_dn:
                state, top, top_i, cnt, bot, cnt_b = start_top(i)
                box = None
                continue
            continue
        # ---------------- box construction
        if state == "top":
            if H[i] > top:
                top, top_i, cnt = H[i], i, 0
            else:
                cnt += 1
                if cnt >= n_conf:
                    seg = L[top_i + 1:i + 1]
                    k = int(np.argmin(seg))
                    bot, cnt_b = seg[k], len(seg) - 1 - k
                    state = "bottom"
                    if cnt_b >= n_conf:
                        state, box = "box", (top, bot, top_i)
        elif state == "bottom":
            if H[i] > top:
                state, top, top_i, cnt, bot, cnt_b = start_top(i)
            elif L[i] < bot:
                bot, cnt_b = L[i], 0
            else:
                cnt_b += 1
                if cnt_b >= n_conf:
                    state, box = "box", (top, bot, top_i)
        if state == "box" and pos is not None:
            # a completed box while in the trade raises the stop, never lowers it
            pos["stop"] = max(pos["stop"], round(box[1] - 0.01, 2))
    if pos is not None:
        pos.update(exit=C[-1], exit_date=D[-1], exit_i=n - 1, outcome="open")
        trades.append(pos)
    for t in trades:
        t["R"] = (t["exit"] - t["entry"]) / t["risk"]
        t["R_net"] = t["R"] - (COST_PS + SLIP * (t["entry"] + t["exit"])) / t["risk"]
        t["days"] = t["exit_i"] - t["entry_i"]
        t["ret"] = t["exit"] / t["entry"] - 1 - (COST_PS + SLIP * (t["entry"] + t["exit"])) / t["entry"]
    return trades


# ------------------------------------------------------------------ portfolio

def portfolio(trades, daily, start, end, risk_frac=0.01, max_pos=10, max_w=0.20, cash0=100_000.0):
    """Fixed-fractional sizing: risk 1 % of equity per trade, at most 10 positions and
    20 % of equity each. Signals of the same day go tightest box first."""
    dates = sorted({d for df in daily.values() for d in df.date if start <= d <= end})
    closes = {s: dict(zip(df.date, df.c)) for s, df in daily.items()}
    by_entry = {}
    for t in trades:
        if start <= t["entry_date"] <= end:
            by_entry.setdefault(t["entry_date"], []).append(t)
    cash, open_, curve, taken = cash0, [], [], 0
    for d in dates:
        # exits first
        still = []
        for p in open_:
            t = p["t"]
            if t["exit_date"] <= d or d == dates[-1]:
                px = t["exit"] if t["exit_date"] <= d else closes[t["sym"]].get(d, t["entry"])
                cash += p["sh"] * px - p["sh"] * (COST_PS / 2 + SLIP * px)
            else:
                still.append(p)
        open_ = still
        eq = cash + sum(p["sh"] * closes[p["t"]["sym"]].get(d, p["last"]) for p in open_)
        for t in sorted(by_entry.get(d, []), key=lambda t: t["depth"]):
            if len(open_) >= max_pos:
                break
            sh = int(min(risk_frac * eq / t["risk"], max_w * eq / t["entry"], cash / t["entry"]))
            if sh <= 0:
                continue
            cash -= sh * t["entry"] + sh * (COST_PS / 2 + SLIP * t["entry"])
            taken += 1
            if t["exit_date"] == d:                 # stopped the same day
                cash += sh * t["exit"] - sh * (COST_PS / 2 + SLIP * t["exit"])
                continue
            open_.append({"t": t, "sh": sh, "last": t["entry"]})
        for p in open_:
            p["last"] = closes[p["t"]["sym"]].get(d, p["last"])
        eq = cash + sum(p["sh"] * p["last"] for p in open_)
        curve.append((d, eq))
    return curve, taken


def curve_stats(curve):
    if len(curve) < 2:
        return {}
    eq = np.array([e for _, e in curve])
    yrs = len(eq) / 252
    peak = np.maximum.accumulate(eq)
    r = np.diff(eq) / eq[:-1]
    return {"cagr": (eq[-1] / eq[0]) ** (1 / yrs) - 1, "max_dd": float(((eq - peak) / peak).min()),
            "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else np.nan,
            "total": eq[-1] / eq[0] - 1}


def buy_hold(daily, start, end):
    """Equal weight, rebalanced daily, over the stocks trading each day."""
    rets = []
    for s, df in daily.items():
        x = df[(df.date >= start) & (df.date <= end)].set_index("date").c.pct_change()
        rets.append(x.rename(s))
    m = pd.concat(rets, axis=1).mean(axis=1).fillna(0)
    eq = (1 + m).cumprod() * 100_000
    return list(zip(eq.index, eq.to_numpy()))


def trade_stats(trades, start, end):
    t = [x for x in trades if start <= x["entry_date"] <= end]
    if not t:
        return {"n": 0}
    r = np.array([x["R_net"] for x in t])
    months = pd.Series(r, index=[x["entry_date"][:7] for x in t]).groupby(level=0).agg(["sum", "count"])
    S, N = months["sum"].to_numpy(), months["count"].to_numpy()
    m = S.sum() / N.sum()
    se = np.sqrt(((S - m * N) ** 2).sum()) / N.sum()
    srt = np.sort(r)
    k = max(1, int(0.02 * len(r)))
    return {"n": len(r), "avg_R": float(m), "t": float(m / se) if se > 0 else np.nan,
            "win": float((r > 0).mean()), "median_R": float(np.median(r)),
            "avg_R_without_top2pct": float(srt[:-k].mean()) if len(r) > 20 else np.nan,
            "avg_days": float(np.mean([x["days"] for x in t])),
            "avg_ret": float(np.mean([x["ret"] for x in t]))}


def random_benchmark(trades, daily, start, end, draws=20, seed=5):
    """Same stock, a random entry day in the same window, same stop distance in %,
    same holding period at most; exits at that stop or after the holding period."""
    rng = random.Random(seed)
    out = []
    for t in trades:
        if not (start <= t["entry_date"] <= end):
            continue
        df = daily[t["sym"]]
        idx = df.index[(df.date >= start) & (df.date <= end)].to_numpy()
        if len(idx) < 30:
            continue
        frac = t["risk"] / t["entry"]
        hold = max(1, t["days"])
        O, L, C = df.o.to_numpy(), df.l.to_numpy(), df.c.to_numpy()
        for _ in range(draws):
            i = int(rng.choice(idx))
            e = O[i]
            s = e * (1 - frac)
            j_end = min(len(df) - 1, i + hold)
            ex = C[j_end]
            for j in range(i, j_end + 1):
                if L[j] <= s:
                    ex = min(O[j], s) if j > i else s
                    break
            risk = e - s
            out.append((ex - e) / risk - (COST_PS + SLIP * (e + ex)) / risk)
    return float(np.mean(out)) if out else np.nan, len(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="bt_cache")
    ap.add_argument("--out", default="bt_results/darvas")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    daily = {}
    for p in sorted(glob.glob(os.path.join(a.cache, "alpaca-sip-*-5m-2021-01-04-2026-10-27.json.gz"))):
        s = os.path.basename(p).split("-")[2]
        daily[s] = daily_from_5m(p)
    spy = spy_daily(a.cache)
    mkt = dict(zip(spy.date, spy.mkt_ok))
    end = max(df.date.max() for df in daily.values())

    grid = list(itertools.product([3, 5], [True, False], [None, 0.10, 0.20], [False, True]))
    rows, all_trades = [], {}
    for n_conf, w52, depth, mf in grid:
        trades = []
        for s, df in daily.items():
            trades += darvas_trades(df, s, n_conf, w52, depth, mkt if mf else None)
        key = (n_conf, w52, depth, mf)
        all_trades[key] = trades
        rec = {"n_conf": n_conf, "w52": w52, "max_depth": depth, "mkt_filter": mf}
        for part, (s0, s1) in (("is", (IS_START, IS_END)), ("oos", (OOS_START, end))):
            st = trade_stats(trades, s0, s1)
            rec.update({part + "_" + k: v for k, v in st.items()})
            ps = curve_stats(portfolio(trades, daily, s0, s1)[0])
            rec.update({part + "_pf_" + k: v for k, v in ps.items()})
        rows.append(rec)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(a.out, "grid.csv"), index=False)

    bench = {}
    for part, (s0, s1) in (("is", (IS_START, IS_END)), ("oos", (OOS_START, end))):
        bench["bh_" + part] = curve_stats(buy_hold(daily, s0, s1))
        sp = spy[(spy.date >= s0) & (spy.date <= s1)]
        bench["spy_" + part] = curve_stats(list(zip(sp.date, sp.c * 1000)))
    base_key = (3, True, None, False)
    for part, (s0, s1) in (("is", (IS_START, IS_END)), ("oos", (OOS_START, end))):
        bench["random_" + part] = random_benchmark(all_trades[base_key], daily, s0, s1)
    with open(os.path.join(a.out, "bench.json"), "w") as f:
        json.dump(bench, f, indent=1, default=float)
    pd.to_pickle(all_trades, os.path.join(a.out, "trades.pkl"))
    print("done", len(res), "configs")


if __name__ == "__main__":
    main()
