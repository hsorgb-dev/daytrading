"""Search the ORB grid on the in-sample years, then score every config out of sample.

    python3 -m backtest.research.orb_search --grid bt_results/orb_grid.parquet --out bt_results/orb_search.csv

Config = entry rule x direction x stop x target x entry cutoff x filter.
Filters use only what is known when the opening range ends:
  rvol>=x  the stock's opening-range volume vs its own 14-session average
  topK     the K stocks of the day with the highest first-5-minute relative volume
           ("stocks in play"), ranked across the whole universe, not just the ones
           that later break out
Selection uses the in-sample years only and ranks by a day-clustered t-statistic
of the mean net R per trade. Out-of-sample columns are computed for every config
so the report can show whether in-sample rank carries over at all.
"""

import argparse
import itertools

import numpy as np
import pandas as pd

TARGET_COLS = {"1R": "R1", "2R": "R2", "3R": "R3", "15:55": "Reod"}
CUTOFFS = {"bis 15:30": 71, "bis 10:30": 11}
FILTERS = ["alle", "rvol>=1", "rvol>=1.5", "rvol>=2", "rvol>=3", "top5", "top10", "top20"]


def clustered(df, col):
    """Mean of col per trade, day-clustered t-stat, n, days."""
    if len(df) == 0:
        return np.nan, np.nan, 0, 0
    g = df.groupby("date", observed=True)[col].agg(["sum", "count"])
    S, N = g["sum"].to_numpy(), g["count"].to_numpy()
    m = S.sum() / N.sum()
    resid = S - m * N
    se = np.sqrt((resid ** 2).sum()) / N.sum() if len(S) > 1 else np.nan
    return m, (m / se if se and se > 0 else np.nan), int(N.sum()), len(S)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default="bt_results/orb_grid.parquet")
    ap.add_argument("--out", default="bt_results/orb_search.csv")
    ap.add_argument("--is-end", default="2023-12-31")
    a = ap.parse_args(argv)
    g = pd.read_parquet(a.grid)
    g["date"] = g["date"].astype(str)

    # universe-wide ranking by first-5-minute relative volume, known at 9:35
    fc = g[g.entry_mode == "fc5"].drop_duplicates(["sym", "date"])[["sym", "date", "rvol"]]
    fc = fc.rename(columns={"rvol": "rvol5"})
    fc["rank5"] = fc.groupby("date")["rvol5"].rank(ascending=False, method="first")
    g = g.merge(fc[["sym", "date", "rank5"]], on=["sym", "date"], how="left")
    g["is"] = g["date"] <= a.is_end

    rows = []
    for (mode, stop), sub in g.groupby(["entry_mode", "stop"], observed=True):
        for side_name in ("long", "short", "beide"):
            if side_name == "long":
                s = sub[sub.side == 1]
            elif side_name == "short":
                s = sub[sub.side == -1]
            else:   # first signal of the day per stock, either side
                s = sub.sort_values("sig").drop_duplicates(["sym", "date"], keep="first")
            for (cut_name, cut), flt in itertools.product(CUTOFFS.items(), FILTERS):
                if mode == "fc5" and cut_name != "bis 15:30":
                    continue
                t = s[s.sig <= cut]
                if flt.startswith("rvol"):
                    t = t[t.rvol >= float(flt[6:])]
                elif flt.startswith("top"):
                    t = t[t.rank5 <= int(flt[3:])]
                for tname, tcol in TARGET_COLS.items():
                    t2 = t.assign(net=t[tcol] - t["cost_R"], net2=t[tcol] - 2 * t["cost_R"])
                    rec = {"entry": mode, "side": side_name, "stop": stop, "target": tname,
                           "cutoff": cut_name, "filter": flt}
                    for part, mask in (("is", t2["is"]), ("oos", ~t2["is"])):
                        m, tt, n, d = clustered(t2[mask], "net")
                        m2, _, _, _ = clustered(t2[mask], "net2")
                        rec.update({part + "_avg": m, part + "_t": tt, part + "_n": n, part + "_days": d,
                                    part + "_avg_cost2x": m2,
                                    part + "_win": float((t2[mask]["net"] > 0).mean()) if n else np.nan})
                    rows.append(rec)
    res = pd.DataFrame(rows)
    res.to_csv(a.out, index=False)
    print("configs", len(res))


if __name__ == "__main__":
    main()
