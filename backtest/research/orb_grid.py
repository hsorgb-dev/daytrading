"""Parameter search for opening-range breakouts on the cached Alpaca 5-minute bars.

Research tooling (needs numpy and pandas, unlike the rest of the repo). Simulates
every combination of entry rule, direction and stop on every symbol-day in one
vectorized pass per symbol, with four targets each, and writes one table:

    python3 -m backtest.research.orb_grid --cache bt_cache --out bt_results/orb_grid.parquet

Execution model matches backtest/engine.py: entry at the OPEN of the bar after
the signal bar, stop checked before target in the same bar, a bar opening
through the stop fills at its open, targets fill at the target, flat at the close
of the 15:50 bar. Cost: $0.02 a share round trip, as R = cost / risk per share.
Only full 78-bar sessions are used (half days are skipped).

Features stored per trade, all known at entry time (no look-ahead):
  rvol   volume of the opening range / average volume of the same window over the
         previous 14 full sessions
  atr    14-session average true range from daily bars built from the 5-minute bars,
         previous sessions only
  gap    open / previous close - 1
"""

import argparse
import glob
import gzip
import json
import os

import numpy as np
import pandas as pd

NB = 78                 # 5-minute bars in a full session
LAST_HELD = 76          # the 15:50 bar; its close is 15:55
COST = 0.02
TARGETS = (1.0, 2.0, 3.0, None)          # None: hold to 15:55
STOPS = ("or_mid", "or_opp", "atr05", "atr10", "atr20")
ENTRIES = (("orb5", 1), ("orb15", 3), ("orb30", 6), ("fc5", 1))   # fc5: direction of the first 5-minute candle


def load_symbol(path):
    with gzip.open(path, "rt") as f:
        days = json.load(f)
    dates, arr = [], []
    for d in sorted(days):
        bars = days[d]
        if len(bars) != NB or bars[0]["slot"] != 0:
            continue
        dates.append(d)
        arr.append([[b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars])
    a = np.asarray(arr, dtype=np.float64)          # days x 78 x 5
    return np.array(dates), a


def daily_features(a, n_or):
    O, H, L, C, V = (a[:, :, i] for i in range(5))
    dh, dl, dc, do = H.max(1), L.min(1), C[:, -1], O[:, 0]
    prev_c = np.r_[np.nan, dc[:-1]]
    tr = np.maximum(dh - dl, np.maximum(abs(dh - prev_c), abs(dl - prev_c)))
    atr = pd.Series(tr).rolling(14).mean().shift(1).to_numpy()        # previous 14 sessions
    orv = V[:, :n_or].sum(1)
    rvol = orv / pd.Series(orv).rolling(14).mean().shift(1).to_numpy()
    gap = do / prev_c - 1
    return atr, rvol, gap


def first_true(mask):
    """Index of the first True per row, or -1."""
    any_ = mask.any(1)
    idx = mask.argmax(1)
    return np.where(any_, idx, -1)


def simulate(a, sig_idx, side, stop_px):
    """Vectorized walk. sig_idx: signal bar per day (-1 = no trade). Returns entry, risk,
    and R per target (gross), NaN where no trade."""
    O, H, L, C = (a[:, :, i] for i in range(4))
    nd = len(a)
    ent_i = sig_idx + 1
    ok = (sig_idx >= 0) & (ent_i <= LAST_HELD)
    rows = np.arange(nd)
    ei = np.where(ok, ent_i, 0)
    entry = np.round(O[rows, ei], 2)
    long = side > 0
    risk = np.where(long, entry - stop_px, stop_px - entry)
    ok &= np.isfinite(stop_px) & (risk > 0) & (risk >= 0.0005 * entry)
    col = np.arange(NB)[None, :]
    live = (col >= ei[:, None]) & (col <= LAST_HELD) & ok[:, None]
    s = stop_px[:, None]
    hit_s = live & np.where(long[:, None], L <= s, H >= s)
    si = first_true(hit_s)
    s_open = O[rows, np.maximum(si, 0)]
    s_fill = np.where(long, np.minimum(s_open, stop_px), np.maximum(s_open, stop_px))
    r_stop = np.where(long, s_fill - entry, entry - s_fill) / np.where(risk > 0, risk, np.nan)
    eod = C[:, LAST_HELD]
    r_eod = np.where(long, eod - entry, entry - eod) / np.where(risk > 0, risk, np.nan)
    out = {}
    for t in TARGETS:
        if t is None:
            r = np.where(si >= 0, r_stop, r_eod)
        else:
            tp = np.round(np.where(long, entry + t * risk, entry - t * risk), 2)
            hit_t = live & np.where(long[:, None], H >= tp[:, None], L <= tp[:, None])
            ti = first_true(hit_t)
            stop_first = (si >= 0) & ((ti < 0) | (si <= ti))
            tgt = (ti >= 0) & ~stop_first
            r = np.where(stop_first, r_stop, np.where(tgt, t, r_eod))
        out["R%s" % ("eod" if t is None else str(t).rstrip("0").rstrip("."))] = np.where(ok, r, np.nan)
    return ok, entry, risk, out


def run_symbol(sym, dates, a):
    rows = []
    O, H, L, C, V = (a[:, :, i] for i in range(5))
    for name, n in ENTRIES:
        atr, rvol, gap = daily_features(a, n)
        orh, orl = H[:, :n].max(1), L[:, :n].min(1)
        for side in (1, -1):
            if name == "fc5":
                d = np.sign(C[:, 0] - O[:, 0])
                sig = np.where(d == side, 0, -1)
                orh, orl = H[:, 0], L[:, 0]
            else:
                beyond = (C > orh[:, None]) if side > 0 else (C < orl[:, None])
                beyond[:, :n] = False
                beyond[:, LAST_HELD:] = False
                sig = first_true(beyond)
            sides = np.full(len(a), side)
            for st in STOPS:
                if st == "or_mid":
                    sp = (orh + orl) / 2
                elif st == "or_opp":
                    sp = orl if side > 0 else orh
                else:
                    k = {"atr05": 0.05, "atr10": 0.10, "atr20": 0.20}[st]
                    ei = np.clip(sig + 1, 0, NB - 1)
                    ep = np.round(O[np.arange(len(a)), ei], 2)
                    sp = ep - side * k * atr
                sp = np.where(side > 0, np.floor(np.round(sp * 100, 4)) / 100, np.ceil(np.round(sp * 100, 4)) / 100)
                ok, entry, risk, rs = simulate(a, sig, sides, sp)
                if not ok.any():
                    continue
                df = pd.DataFrame({
                    "sym": sym, "date": dates[ok], "entry_mode": name, "side": side, "stop": st,
                    "sig": sig[ok].astype(np.int16), "entry": entry[ok], "risk": risk[ok],
                    "risk_pct": (100 * risk / entry)[ok], "cost_R": (COST / risk)[ok],
                    "atr_pct": (100 * atr / entry)[ok], "rvol": rvol[ok], "gap": gap[ok],
                    "or_pct": (100 * (orh - orl) / entry)[ok],
                    **{k: v[ok] for k, v in rs.items()}})
                rows.append(df)
    return pd.concat(rows) if rows else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="bt_cache")
    ap.add_argument("--glob", default="alpaca-sip-*-5m-2021-01-04-2026-10-27.json.gz")
    ap.add_argument("--out", default="bt_results/orb_grid.parquet")
    a = ap.parse_args(argv)
    parts = []
    files = sorted(glob.glob(os.path.join(a.cache, a.glob)))
    for i, p in enumerate(files):
        sym = os.path.basename(p).split("-")[2]
        if sym == "SPY":
            continue
        dates, arr = load_symbol(p)
        df = run_symbol(sym, dates, arr)
        if df is not None:
            for c in df.columns:
                if df[c].dtype == np.float64:
                    df[c] = df[c].astype(np.float32)
            parts.append(df)
        if (i + 1) % 10 == 0:
            print("  %d/%d" % (i + 1, len(files)), flush=True)
    out = pd.concat(parts, ignore_index=True)
    for c in ("sym", "entry_mode", "stop"):
        out[c] = out[c].astype("category")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    out.to_parquet(a.out)
    print("rows", len(out))


if __name__ == "__main__":
    main()
