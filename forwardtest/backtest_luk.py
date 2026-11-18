#!/usr/bin/env python3
"""Backtest der Variante L (Luk-Pullback-Swing) auf Yahoo-Tageskerzen. Nicht Teil des Journals.

    python3 forwardtest/backtest_luk.py download            # Tageskerzen 2015-heute fuer das Universum (Cache)
    python3 forwardtest/backtest_luk.py run [--out DATEI]   # Laeufe rechnen, Markdown nach stdout / DATEI

Universum: heutige Nasdaq-Trader-Liste wie im Tageslauf, Vorfilter heutiges Volumen >= 300k. Damit fehlen
delistete Titel (Survivorship Bias: Ergebnisse eher zu gut). Laeufe:
  repo  Parameter des Referenz-Repos (rootchunhe/martin-luk-backtesting, "full"): 50 Mio. $ Dollar-Volumen,
        bis 280 % brutto, 10 Positionen, 100.000 $, 2016-01-04 bis 2026-08-31. Zum Abgleich der Umsetzung.
  L     Parameter der Variante L aus config.json, 10.000 $, Gewinne reinvestiert, 2016-01-04 bis heute.
  ref   Variante L wie die anderen Varianten als Referenz: 23.09.-16.11.2026, feste Basis 10.000 $.
Kosten wie im Forward-Test (0,05 % Slippage auf Market-Fills, 0,005 $/Aktie, mind. 1 $); keine Margin-Zinsen,
kein Zins auf Cash (das Referenz-Repo rechnet beides).
"""

import argparse
import copy
import datetime
import importlib.util
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import luk  # noqa: E402

_spec = importlib.util.spec_from_file_location("ft_daily_run", os.path.join(HERE, "daily_run.py"))
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)
scan = dr.scan

CACHE = os.path.join(HERE, ".cache", "luk_daily")
START = datetime.date(2015, 1, 2)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def universe():
    today = scan.to_eastern(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).date()
    data = dr.Data(os.path.join(HERE, ".cache", today.isoformat()))
    syms = data.universe()
    sp = data.spark_daily(syms)["data"]
    keep = [s for s in syms if sp.get(s) and ((sp[s].get("meta") or {}).get("regularMarketVolume") or 0) >= 300000]
    return sorted(set(keep) | {"QQQ"})


def _path(sym):
    return os.path.join(CACHE, scan.to_yahoo(sym) + ".json")


def fetch(sym):
    p = _path(sym)
    if os.path.exists(p):
        return sym, "cache"
    p1 = int(datetime.datetime(START.year, START.month, START.day).timestamp())
    p2 = int(time.time()) + 86400
    path = "/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d" % (scan.to_yahoo(sym), p1, p2)
    try:
        s = scan.parse_chart(scan.yahoo_get(path), sym)
        doc = {k: s.get(k) for k in ("t", "o", "h", "l", "c", "v")}
    except scan.ScanError as e:
        doc = {"error": str(e)}
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    time.sleep(0.1)
    return sym, "error" if "error" in doc else "ok"


def download(workers=4):
    os.makedirs(CACHE, exist_ok=True)
    syms = universe()
    log("%d Titel" % len(syms))
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for sym, st in ex.map(fetch, syms):
            done += 1
            if done % 250 == 0:
                log("  %d/%d" % (done, len(syms)))
    log("fertig")


def rows_of(sym):
    p = _path(sym)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    if "error" in d or not d.get("t"):
        return None
    out = {}
    for i, ts in enumerate(d["t"]):
        vals = [d[k][i] if d.get(k) else None for k in ("o", "h", "l", "c")]
        if None in vals or vals[3] <= 0:
            continue
        out[dr.et_date(ts)] = (vals[0], vals[1], vals[2], vals[3], float(d["v"][i] or 0) if d.get("v") else 0.0)
    return [(k,) + out[k] for k in sorted(out)]


class Market:
    """Signals and bars of every symbol for one parameter set."""

    def __init__(self, P, syms):
        self.sig, self.bars, self.ema = {}, {}, {}
        qrows = rows_of("QQQ")
        qf = luk.features(qrows)
        self.days = [r[0] for r in qrows]
        self.mkt = {}
        for k in range(1, len(qrows)):
            e9, e21 = qf["ema9"][k - 1], qf["ema21"][k - 1]
            self.mkt[qrows[k][0]] = e9 is not None and e21 is not None and e9 > e21
        for n, sym in enumerate(syms):
            if sym == "QQQ":
                continue
            rows = rows_of(sym)
            if not rows or len(rows) < 70:
                continue
            f = luk.features(rows)
            hit = False
            for i in range(64, len(rows)):
                s = luk.signal(rows, f, i, P)
                if s:
                    s["symbol"] = sym
                    self.sig.setdefault(rows[i][0], []).append(s)
                    hit = True
            if hit:
                self.bars[sym] = {r[0]: r[1:5] for r in rows}
                self.ema[sym] = {rows[i][0]: f["ema9"][i] for i in range(len(rows))}
            if n % 1000 == 0:
                log("  Signale: %d/%d Titel" % (n, len(syms)))


def simulate(mk, P, costs, capital, start, end, fixed=False):
    st = luk.new_state(capital)
    trades, navs = [], []
    days = [d for d in mk.days if start <= d <= end]
    for d in days:
        held = list(st["positions"])
        bars = {s: mk.bars[s][d] for s in held if d in mk.bars[s]}
        ema = {s: mk.ema[s].get(d) for s in held}
        sigs = mk.sig.get(d, [])
        for s in sigs:
            bars.setdefault(s["symbol"], mk.bars[s["symbol"]][d])
        closed, _, _, _ = luk.step(st, d, {s: bars[s] for s in held if s in bars}, sigs, mk.mkt.get(d, False),
                                   ema, P, costs, account=capital if fixed else None)
        trades += closed
        navs.append((d, luk.nav(st), luk.gross(st)))
    if fixed and days:
        closes = {s: mk.bars[s][days[-1]][3] for s in st["positions"] if days[-1] in mk.bars[s]}
        trades += luk.close_all(st, days[-1], closes, costs)
        navs[-1] = (days[-1], luk.nav(st), 0.0)
    return trades, navs


def stats(trades, navs, capital):
    n = len(trades)
    rets = [t["net"] / (t["shares"] * t["entry"]) for t in trades]
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    yrs = max((navs[-1][0] - navs[0][0]).days / 365.25, 1e-9) if navs else 1
    end = navs[-1][1] if navs else capital
    dd = peak = 0.0
    for _, v, _ in navs:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    dr_ = [navs[i][1] / navs[i - 1][1] - 1 for i in range(1, len(navs))]
    mu = sum(dr_) / len(dr_) if dr_ else 0
    sd = math.sqrt(sum((x - mu) ** 2 for x in dr_) / (len(dr_) - 1)) if len(dr_) > 1 else 0
    pnl = sorted((t["net"] for t in trades), reverse=True)
    net = sum(pnl)
    gp = sum(x for x in pnl if x > 0)
    gl = -sum(x for x in pnl if x < 0)
    rs = [t["r_net"] for t in trades]
    return {"trades": n, "per_year": n / yrs, "win": len(wins) / n if n else 0,
            "avg_win": sum(wins) / len(wins) if wins else 0, "avg_loss": sum(loss) / len(loss) if loss else 0,
            "avg_r": sum(rs) / n if n else 0, "net": net, "end": end,
            "cagr": (end / capital) ** (1 / yrs) - 1 if end > 0 and yrs >= 1 else end / capital - 1,
            "maxdd": dd, "sharpe": mu / sd * math.sqrt(252) if sd else 0, "pf": gp / gl if gl else float("inf"),
            "top10": sum(pnl[:10]) / net if net > 0 else None,
            "invested": sum(g / v for _, v, g in navs if v > 0) / len(navs) if navs else 0,
            "hold_win": _median([t["hold_days"] for t in trades if t["net"] > 0]),
            "hold_loss": _median([t["hold_days"] for t in trades if t["net"] <= 0]),
            "first_day_stops": sum(1 for t in trades if t["hold_days"] <= 2 and t["reason"].startswith("stop")) / n if n else 0}


def _median(x):
    x = sorted(x)
    return x[len(x) // 2] if x else None


def pct(x, d=1):
    return "–" if x is None else ("%+." + str(d) + "f %%") % (100 * x)


def by_year(trades, navs, capital):
    out = []
    years = sorted({d.year for d, _, _ in navs})
    for y in years:
        nv = [x for x in navs if x[0].year == y]
        prev = [x for x in navs if x[0].year < y]
        base = prev[-1][1] if prev else capital
        tr = [t for t in trades if t["exit_date"].year == y]
        peak, dd = base, 0.0
        for _, v, _ in nv:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1)
        w = sum(1 for t in tr if t["net"] > 0)
        out.append((y, nv[-1][1] / base - 1, len(tr), w / len(tr) if tr else 0, dd))
    return out


def run(out_path=None):
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    costs = cfg["costs"]
    syms = sorted(f[:-5] for f in os.listdir(CACHE) if f.endswith(".json"))
    vL = cfg["variants"]["L"]
    PL = luk.params(vL)
    PR = luk.params(dict(vL, min_dollar_volume=50e6, max_gross=2.8, max_positions=10))
    L = []
    log("Markt fuer Repo-Parameter ...")
    mkR = Market(PR, syms)
    log("Markt fuer L-Parameter ...")
    mkL = Market(PL, syms)
    today = mkL.days[-1]
    mkN = copy.copy(mkL)                         # same signals, QQQ filter switched off (comparison only)
    mkN.mkt = {d: True for d in mkL.days}
    runs = [("repo", mkR, PR, 100000.0, datetime.date(2016, 1, 4), datetime.date(2026, 8, 31)),
            ("L", mkL, PL, 10000.0, datetime.date(2016, 1, 4), today),
            ("L ohne QQQ-Filter", mkN, PL, 10000.0, datetime.date(2016, 1, 4), today)]
    L.append("# Backtest Variante L (Luk-Pullback-Swing)\n")
    L.append("Stand %s · Yahoo-Tageskerzen · %d Titel im Universum (heutige Liste, Survivorship Bias) · "
             "erzeugt von forwardtest/backtest_luk.py\n" % (today, len(syms)))
    L.append("| Lauf | Zeitraum | Trades | /Jahr | Treffer | Ø Gewinn | Ø Verlust | Ø R netto | PF | CAGR | MaxDD | "
             "Sharpe | Ø investiert | Top-10-Anteil | Haltedauer Gew./Verl. |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    yearly = {}
    for name, mk, P, cap, a, b in runs:
        tr, nv = simulate(mk, P, costs, cap, a, b)
        s = stats(tr, nv, cap)
        yearly[name] = by_year(tr, nv, cap)
        L.append("| %s | %s–%s | %d | %.0f | %.0f %% | %s | %s | %+.2f | %.2f | %s | %s | %.2f | %.0f %% | %s | %s / %s |" % (
            name, a, b, s["trades"], s["per_year"], 100 * s["win"], pct(s["avg_win"]), pct(s["avg_loss"]),
            s["avg_r"], s["pf"], pct(s["cagr"]), pct(s["maxdd"], 0), s["sharpe"], 100 * s["invested"],
            "–" if s["top10"] is None else "%.0f %%" % (100 * s["top10"]), s["hold_win"], s["hold_loss"]))
        if name == "L":
            ex = sorted(tr, key=lambda t: -t["net"])
            L_top = ex[:5]
    L.append("\nReferenz-Repo, Lauf full (CRSP/TAQ, NBBO-Kosten, Margin-Zins): 287 Trades (27/Jahr), 29 % Treffer, "
             "Ø Gewinn +14,3 %, Ø Verlust −2,9 %, CAGR 12,1 %, MaxDD −13 %, Sharpe 0,83, Ø investiert 8 %, "
             "Top-10 = 111 % des Netto-Gewinns.\n")
    L.append("## Nach Jahren\n")
    L.append("| Jahr | repo | Trades | Treffer | MaxDD | L | Trades | Treffer | MaxDD |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    ry = {r[0]: r for r in yearly["repo"]}
    del yearly["L ohne QQQ-Filter"]
    for y, ret, n, w, dd in yearly["L"]:
        r = ry.get(y)
        rp = ("%s | %d | %.0f %% | %s" % (pct(r[1]), r[2], 100 * r[3], pct(r[4], 0))) if r else "– | – | – | –"
        L.append("| %d | %s | %s | %d | %.0f %% | %s |" % (y, rp, pct(ret), n, 100 * w, pct(dd, 0)))
    L.append("\n## Referenz wie die anderen Varianten (feste Basis 10.000 $, Positionen am Ende glattgestellt)\n")
    L.append("| Zeitraum | Trades | Netto $ | Ø R netto | Treffer | PF | MaxDD $ |")
    L.append("|---|---|---|---|---|---|---|")
    for lab, mk_, a, b in (("", mkL, datetime.date(2026, 9, 23), datetime.date(2026, 10, 17)),
                           ("", mkL, datetime.date(2026, 10, 20), datetime.date(2026, 11, 16)),
                           ("", mkL, datetime.date(2026, 9, 23), datetime.date(2026, 11, 16)),
                           (" ohne QQQ-Filter", mkN, datetime.date(2026, 10, 20), datetime.date(2026, 11, 16))):
        tr, nv = simulate(mk_, PL, costs, 10000.0, a, b, fixed=True)
        net = sum(t["net"] for t in tr)
        eq = pk = dd = 0.0
        for t in sorted(tr, key=lambda t: t["exit_date"]):
            eq += t["net"]
            pk = max(pk, eq)
            dd = min(dd, eq - pk)
        gp = sum(t["net"] for t in tr if t["net"] > 0)
        gl = -sum(t["net"] for t in tr if t["net"] < 0)
        L.append("| %s–%s%s | %d | %+.0f | %+.2f | %.0f %% | %s | %.0f |" % (
            a.strftime("%d.%m."), b.strftime("%d.%m."), lab, len(tr), net,
            sum(t["r_net"] for t in tr) / len(tr) if tr else 0, 100 * sum(1 for t in tr if t["net"] > 0) / len(tr) if tr else 0,
            "%.2f" % (gp / gl) if gl else "–", dd))
    L.append("\n## Die 5 besten Trades von L\n")
    for t in L_top:
        L.append("- %s %s→%s: %+.0f $ (%+.1f R, %s)" % (t["symbol"], t["entry_date"], t["exit_date"], t["net"],
                                                      t["r_net"], t["reason"]))
    text = "\n".join(L) + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["download", "run"])
    p.add_argument("--out")
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args()
    if a.cmd == "download":
        download(a.workers)
    else:
        run(a.out)


if __name__ == "__main__":
    main()
