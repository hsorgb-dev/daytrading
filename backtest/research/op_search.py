"""Parameter search for the S&P Open Playbook on cached Alpaca SPY bars (pre-market included).

    python3 -m backtest.research.op_search --cache bt_cache --out bt_results/op_search.csv

Signal rules come from backtest/open_playbook.find_signal (v3.4 at its defaults);
exits from open_playbook.manage. Each distinct trade is simulated once per exit
scheme and reused across signal configs. Selection happens on the in-sample years
only (see the report); out-of-sample columns are filled for every config so the
report can show how in-sample rank carries over.
"""

import argparse
import itertools
import statistics

import numpy as np
import pandas as pd

from .. import data
from .. import open_playbook as op

SIGNAL_GRID = {
    "setups": ["beide", "rejection", "continuation"],
    "win_last": ["09:50", "10:30"],
    "min_sig": ["09:30", "09:45"],
    "max_dist": [4.0, 8.0, 15.0, 1e9],
    "rej_min_pen": [0.0, 2.0, 5.0],
    "ema": [True, False],
}
EXITS = {
    "50% 2R / 50% 3R": dict(tp1=2.0, tp2=3.0, breakeven=False),
    "50% 2R / 50% 3R, BE ab 1R": dict(tp1=2.0, tp2=3.0, breakeven=True),
    "50% 1R / 50% 2R": dict(tp1=1.0, tp2=2.0, breakeven=False),
    "1R": dict(tp1=1.0, tp2=1.0, breakeven=False),
    "2R": dict(tp1=2.0, tp2=2.0, breakeven=False),
    "3R": dict(tp1=3.0, tp2=3.0, breakeven=False),
    "bis 15:55": dict(tp1=1e9, tp2=1e9, breakeven=False),
}
FILTERS = list(itertools.product(["alle", "eng", "weit"], ["alle", "mit Gap", "gegen Gap"]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="bt_cache")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--start", default="2021-01-04")
    ap.add_argument("--end", default="2026-10-27")
    ap.add_argument("--scale", type=float, default=0.1)
    ap.add_argument("--cost", type=float, default=0.5)
    ap.add_argument("--is-end", default="2023-12-31")
    ap.add_argument("--out", default="bt_results/op_search.csv")
    a = ap.parse_args(argv)
    bars = data.alpaca_bars_ext(a.symbol, a.start, a.end, cache=a.cache)
    ema = op.ema_series([b["c"] for b in bars])
    by = op.sessions(bars)
    days = []
    for d in sorted(by):
        idx = [i for i in by[d] if "08:30" <= bars[i]["t"] <= "15:55"]
        if any(bars[i]["t"] == "09:30" for i in idx) and idx[0] > 0:
            days.append((d, idx))

    # day features, known at 09:30: gap vs the previous regular close, pre-open range width
    feat, prev_close = {}, None
    for d, idx in days:
        o930 = next(bars[i]["o"] for i in idx if bars[i]["t"] == "09:30")
        por = [bars[i] for i in idx if "08:30" <= bars[i]["t"] <= "09:25"]
        width = (max(b["h"] for b in por) - min(b["l"] for b in por)) / o930 * 100 if por else np.nan
        feat[d] = {"gap": (o930 / prev_close - 1) if prev_close else 0.0, "por_pct": width}
        rth = [bars[i] for i in idx if bars[i]["t"] <= "15:55"]
        prev_close = rth[-1]["c"]
    is_days = [d for d, _ in days if d <= a.is_end]
    med_width = statistics.median(feat[d]["por_pct"] for d in is_days if feat[d]["por_pct"] == feat[d]["por_pct"])

    exit_cache = {}

    def trade_result(d, idx, sig, ex_name):
        key = (d, sig["k"], sig["side"], round(sig["stop"], 4), ex_name)
        if key not in exit_cache:
            p = dict(op.DEFAULTS, **EXITS[ex_name])
            exit_cache[key] = op.manage(bars, idx, sig, p, a.scale, a.cost)
        return exit_cache[key]

    rows = []
    keys = list(SIGNAL_GRID)
    for combo in itertools.product(*SIGNAL_GRID.values()):
        sp = dict(zip(keys, combo))
        if sp["setups"] == "rejection" and (sp["max_dist"] != 4.0 or sp["ema"] is False):
            continue      # continuation-only knobs do not change a rejection-only config
        if sp["setups"] == "continuation" and sp["rej_min_pen"] != 0.0:
            continue
        p = dict(op.DEFAULTS, rejection=sp["setups"] != "continuation", continuation=sp["setups"] != "rejection",
                 win_last=sp["win_last"], min_sig=sp["min_sig"], max_dist=sp["max_dist"],
                 rej_min_pen=sp["rej_min_pen"], ema=sp["ema"])
        sigs = []
        for d, idx in days:
            s, _ = op.find_signal(bars, idx, ema, a.scale, p)
            if s:
                sigs.append((d, idx, s))
        for ex_name in EXITS:
            trades = []
            for d, idx, s in sigs:
                r = trade_result(d, idx, s, ex_name)
                if r is None or "skipped" in r:
                    continue
                f = feat[d]
                trades.append((d, s["side"], s["setup"], r["R"], r["R_net"], r["risk_pts"], f["gap"], f["por_pct"]))
            df = pd.DataFrame(trades, columns=["date", "side", "setup", "R", "net", "risk_pts", "gap", "por_pct"])
            df["cost_R"] = df["R"] - df["net"]
            for wf, gf in FILTERS:
                t = df
                if wf == "eng":
                    t = t[t.por_pct <= med_width]
                elif wf == "weit":
                    t = t[t.por_pct > med_width]
                if gf != "alle":
                    with_gap = ((t.side == "long") & (t.gap > 0)) | ((t.side == "short") & (t.gap < 0))
                    t = t[with_gap] if gf == "mit Gap" else t[~with_gap]
                rec = dict(sp, exit=ex_name, range_filter=wf, gap_filter=gf)
                for part, m in (("is", t.date <= a.is_end), ("oos", t.date > a.is_end)):
                    x = t[m]["net"].to_numpy()
                    n = len(x)
                    mean = x.mean() if n else np.nan
                    sd = x.std(ddof=1) if n > 1 else np.nan
                    rec.update({part + "_n": n, part + "_avg": mean,
                                part + "_t": mean / sd * np.sqrt(n) if n > 1 and sd > 0 else np.nan,
                                part + "_win": float((x > 0).mean()) if n else np.nan,
                                part + "_avg_cost2x": float((t[m]["net"] - t[m]["cost_R"]).mean()) if n else np.nan,
                                part + "_cont_share": float((t[m]["setup"] == "continuation").mean()) if n else np.nan})
                rows.append(rec)
    res = pd.DataFrame(rows)
    res.to_csv(a.out, index=False)
    print("configs", len(res), "unique trades simulated", len(exit_cache))


if __name__ == "__main__":
    main()
