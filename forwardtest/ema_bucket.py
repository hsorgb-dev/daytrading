"""EMA12-Open-Bias: Auswertung der bestehenden Journal-Trades nach Abstand des Opens zum Daily-EMA12.

Nur Lesen, aendert weder Regeln noch Journal. Fuer jeden Trade:
  d = (Open Tag D - EMA12 der Schlusskurse bis D-1) / ATR14 bis D-1, mit Vorzeichen der Trade-Richtung.
Buckets: gegenlaeufig (d <= -0,25), neutral (|d| < 0,25), konform (0,25 <= d <= 1,5), ueberdehnt (d > 1,5).

Aufruf (aus dem Repo-Root):  python3 forwardtest/ema_bucket.py [--out ORDNER]
Ausgabe: ema_bucket.md und ema_bucket_trades.csv in ORDNER (Default forwardtest/), Kurs-Cache dort in
.cache_ema_bucket. Die Wochenauswertung ruft es mit einem Ordner ausserhalb des Repos auf.
K ist per Regel nur "konform" und zaehlt deshalb nicht in die gepoolte Zeile.
"""
import argparse
import csv
import datetime
import importlib.util
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FORWARD_FROM = "2026-11-17"
OUT = HERE
CACHE = os.path.join(HERE, ".cache_ema_bucket")
NO_R = {"G", "G2", "GQ", "H", "G-1x", "G2-1x", "GQ-1x", "H-1x"}  # R nicht aussagekraeftig -> Netto-$
ORDER = ["gegenlaeufig", "neutral", "konform", "ueberdehnt", "keine Daten"]


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("eb_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = _load("trader-scanner", "scan")


def daily(sym):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, scan.to_yahoo(sym) + ".json")
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    s = scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, "1y", "1d")), sym)
    with open(p, "w") as fh:
        json.dump(s, fh)
    time.sleep(0.25)
    return s


def bars_by_date(s):
    out = []
    for i, t in enumerate(s["t"]):
        o, h, l, c = s["o"][i], s["h"][i], s["l"][i], s["c"][i]
        if None in (o, h, l, c):
            continue
        # Yahoo-Tageskerzen tragen den Zeitstempel der US-Sitzung; UTC-Datum reicht fuer 1d
        d = datetime.datetime.fromtimestamp(t + 4 * 3600, datetime.timezone.utc).date().isoformat()
        out.append((d, o, h, l, c))
    return out


def ema_atr_open(bars, day):
    """EMA12 und ATR14 (Wilder) aus Bars bis D-1, Open von Tag D."""
    idx = next((i for i, b in enumerate(bars) if b[0] == day), None)
    if idx is None or idx < 30:
        return None
    hist = bars[:idx]
    k = 2 / 13
    ema = hist[0][4]
    for b in hist[1:]:
        ema = b[4] * k + ema * (1 - k)
    trs = [max(b[2] - b[3], abs(b[2] - p[4]), abs(b[3] - p[4])) for p, b in zip(hist[:-1], hist[1:])]
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    return ema, atr, bars[idx][1]


def bucket(d):
    if d is None:
        return "keine Daten"
    if d <= -0.25:
        return "gegenlaeufig"
    if d < 0.25:
        return "neutral"
    return "konform" if d <= 1.5 else "ueberdehnt"


def main(argv=None):
    global OUT, CACHE
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=HERE)
    a = ap.parse_args(argv)
    OUT, CACHE = a.out, os.path.join(a.out, ".cache_ema_bucket")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(HERE, "journal", "trades.csv")) as fh:
        trades = list(csv.DictReader(fh))
    cache, rows = {}, []
    for t in trades:
        sym = t["symbol"]
        if sym not in cache:
            try:
                cache[sym] = bars_by_date(daily(sym))
            except Exception as exc:  # fehlende Daten -> eigener Bucket
                print("keine Daten", sym, exc)
                cache[sym] = []
        r = ema_atr_open(cache[sym], t["date"]) if cache[sym] else None
        if t["stop"]:
            long_ = float(t["entry"]) >= float(t["stop"])
        else:  # H ohne Stop: Richtung aus dem Signal
            long_ = "short" not in t["signal"]
        d = None
        if r and r[1] > 0:
            d = (r[2] - r[0]) / r[1] * (1 if long_ else -1)
        rows.append({"date": t["date"], "variant": t["variant"], "symbol": sym, "side": "long" if long_ else "short",
                     "d": None if d is None else round(d, 2), "bucket": bucket(d),
                     "r_net": float(t["r_net"] or 0), "net_pnl": float(t["net_pnl"] or 0),
                     "phase": "vorwaerts" if t["date"] >= FORWARD_FROM else "backfill"})

    with open(os.path.join(OUT, "ema_bucket_trades.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    def stats(sub):
        n = len(sub)
        if not n:
            return "–"
        r = sum(x["r_net"] for x in sub) / n
        usd = sum(x["net_pnl"] for x in sub)
        hit = sum(1 for x in sub if x["net_pnl"] > 0) / n * 100
        return "%d / %+.2f R / %+.0f $ / %.0f %%" % (n, r, usd, hit)

    variants = sorted({x["variant"] for x in rows}, key=lambda v: (len(v), v))
    lines = ["# EMA12-Open-Bias: Auswertung bestehender Trades", "",
             "Stand: %s. Zelle = Trades / Ø R netto / Netto $ / Trefferquote. Gesamt = Backfill + vorwärts." %
             datetime.date.today().isoformat(),
             "d = (Open − Daily-EMA12 bis D-1) ÷ ATR14, mit Vorzeichen der Trade-Richtung. "
             "Bei %s R nicht aussagekräftig, Netto-$ zählt." % ", ".join(sorted(NO_R)), "",
             "| Variante | " + " | ".join(ORDER) + " |", "|---" * (len(ORDER) + 1) + "|"]
    for v in variants:
        sub = [x for x in rows if x["variant"] == v]
        lines.append("| %s | %s |" % (v, " | ".join(stats([x for x in sub if x["bucket"] == b]) for b in ORDER)))
    pool = [x for x in rows if x["variant"] not in NO_R and not x["variant"].endswith("-1x")
            and x["variant"] not in ("C2", "C3", "K")]
    lines += ["", "**Gepoolt** (R-Varianten ohne -1x, C2, C3 und K, um Doppelzählung zu vermeiden):", "",
              "| Phase | " + " | ".join(ORDER) + " |", "|---" * (len(ORDER) + 1) + "|"]
    for ph in ("backfill", "vorwaerts", None):
        sub = [x for x in pool if ph is None or x["phase"] == ph]
        lines.append("| %s | %s |" % (ph or "gesamt", " | ".join(stats([x for x in sub if x["bucket"] == b]) for b in ORDER)))
    with open(os.path.join(OUT, "ema_bucket.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
