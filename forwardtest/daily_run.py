#!/usr/bin/env python3
"""Paper-Forward-Test des Day-Trader-Skills: ein Handelstag, regelgetreu nachgespielt.

    python3 forwardtest/daily_run.py                    # letzter abgeschlossener US-Handelstag
    python3 forwardtest/daily_run.py --date 2026-11-16  # ein bestimmter Tag
    python3 forwardtest/daily_run.py --backfill 20      # die letzten 20 Handelstage
    python3 forwardtest/daily_run.py --backfill 20 --no-tm   # ohne Time Machine (schneller)

Ablauf je Tag D (nur Daten, die zum jeweiligen Zeitpunkt bekannt waren):
  1. Watchlist vor dem Open: Tages-Setups des Scanners (20-Tage-Hoch, Bull Flag,
     MA20-Rueckeroberung, Oversold, Oversold Bounce) auf den Schlusskursen bis D-1.
     Liquiditaet am Vortag (Kurs >= min_price, Volumen >= min_volume), die
     `watchlist.size` Titel mit dem hoechsten Dollar-Volumen am Vortag.
  2. ORB auf 5-Minuten-Bars von D: Range 9:30-9:45, Trigger = erster Schlusskurs
     ueber dem Range-Hoch zwischen 9:45 und 15:30. Variante B zusaetzlich:
     Trigger-Volumen >= 1,5x Range-Schnitt und SPY ueber seinem Tagesopen.
  3. Plan wie trader-exit: Stop Range-Mitte (abgerundet), Ziel 2R, flat 15:55.
  4. Groesse wie trader-budget: min(max. Verlust / Risiko, Positionslimit,
     max. Order, freies Cash). Max. 3 Einstiege am Tag, chronologisch.
  5. Simulation auf den Bars nach dem Einstieg: Stop vor Ziel im selben Bar
     (konservativ), Gap durch den Stop fuellt zum Open, sonst Glattstellung.
     Kosten: Slippage auf Market-Fills (Einstieg, Stop, Glattstellung) plus
     Kommission je Seite.
  6. Optional die Time Machine fuer jeden Trade (Prognose vs. Ergebnis).

Schreibt forwardtest/journal/trades.csv, forwardtest/journal/days.csv und
forwardtest/logs/<D>.json. Laeuft ein Tag erneut, werden seine Zeilen ersetzt.
Nur Standardbibliothek. Kein Broker, keine Orders.
"""

import argparse
import csv
import datetime
import importlib.util
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("ft_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = _load("trader-scanner", "scan")

TRADE_COLS = ["date", "variant", "symbol", "setups", "dollar_vol_prev", "or_high", "or_low", "trigger_time",
              "volume_x", "entry", "stop", "target", "risk_ps", "shares", "notional", "exit_time", "exit_price",
              "exit_reason", "gross_pnl", "costs", "net_pnl", "r_net", "tm_target_pct", "tm_stop_pct",
              "tm_neither_pct", "tm_avg_r", "tm_hist_avg_r", "tm_verdict", "data_note"]
DAY_COLS = ["date", "variant", "watchlist", "with_data", "triggers", "taken", "skipped", "spy_open", "spy_close",
            "net_pnl", "note"]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- data (with a per-run cache)

class Data:
    def __init__(self, cache, pause=0.25):
        self.cache = cache
        self.pause = pause
        os.makedirs(cache, exist_ok=True)

    def _cached(self, name, fetch):
        p = os.path.join(self.cache, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        doc = fetch()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return doc

    def universe(self):
        def fetch():
            texts = {f: scan.http_get(scan.NASDAQ_URL + f, timeout=60) for f in scan.NASDAQ_FILES}
            kept, _, _ = scan.build_universe(texts)
            return [s for s, _ in kept]
        return self._cached("universe.json", fetch)

    def spark_daily(self, symbols):
        def fetch():
            log("  Tages-Schlusskurse fuer %d Titel (%d Spark-Abfragen) ..." % (
                len(symbols), (len(symbols) + scan.SPARK_MAX - 1) // scan.SPARK_MAX))
            data, failed = scan.fetch_spark(symbols, "6mo", "1d", self.pause)
            return {"data": data, "failed": failed}
        return self._cached("spark_6mo_1d.json", fetch)

    def chart(self, sym, rng, interval):
        name = "chart_%s_%s/%s.json" % (rng, interval, scan.to_yahoo(sym))

        def fetch():
            time.sleep(self.pause)
            try:
                return scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, rng, interval)), sym)
            except scan.ScanError as e:
                return {"error": str(e)}
        return self._cached(name, fetch)

    def chart_file(self, sym, rng="60d", interval="5m"):
        """Raw Yahoo v8 JSON on disk, for the Time Machine's --offline."""
        p = os.path.join(self.cache, "raw_%s_%s" % (rng, interval), scan.to_yahoo(sym) + ".json")
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            time.sleep(self.pause)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(scan.yahoo_get(scan.chart_path(sym, rng, interval)))
        return p


# ---------------------------------------------------------------- helpers

def et_date(ts):
    return scan.ts_to_eastern(ts).date()


def daily_rows(series):
    """{date: (close, volume)} of a v8 1d chart or spark series (volume None for spark)."""
    out = {}
    v = series.get("v")
    for i, ts in enumerate(series.get("t") or []):
        c = series["c"][i]
        if c is None:
            continue
        out[et_date(ts)] = (float(c), (v[i] if v else None))
    return out


def prev_trading_day(d):
    d -= datetime.timedelta(days=1)
    while not scan.trading_day(d)[0]:
        d -= datetime.timedelta(days=1)
    return d


def last_completed_session(now_utc):
    now = scan.to_eastern(now_utc)
    d = now.date()
    s = scan.session(d)
    if s and now.time() >= (datetime.datetime.combine(d, s[1]) + datetime.timedelta(minutes=15)).time():
        return d
    return prev_trading_day(d)


def hhmm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def day_bars(series, day):
    """{slot: bar} of complete 5-minute regular-session bars on `day`."""
    s = scan.session(day)
    if not s:
        return {}
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    o, h, l, c, v = (series.get(k) for k in ("o", "h", "l", "c", "v"))
    out = {}
    for i, ts in enumerate(series.get("t") or []):
        if ts % 300 or c[i] is None or h is None or h[i] is None or l[i] is None:
            continue
        et = scan.ts_to_eastern(ts)
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // 300
        out[slot] = {"slot": slot, "start": et, "end": et + datetime.timedelta(minutes=5),
                     "o": float(o[i]) if o and o[i] is not None else float(c[i]),
                     "h": float(h[i]), "l": float(l[i]), "c": float(c[i]), "v": (v[i] or 0) if v else 0}
    return out


def r2(x):
    return float("%.2f" % (math.floor(x * 100 + 0.5 + 1e-9) / 100.0))


def floor_cent(x):
    return math.floor(x * 100 + 1e-9) / 100.0


def label(t):
    return t.strftime("%H:%M")


# ---------------------------------------------------------------- one day

def build_watchlist(day, symbols, spark, data, cfg):
    wl = cfg["watchlist"]
    prev = prev_trading_day(day)
    cands = []
    for sym in symbols:
        s = spark.get(sym)
        if not s:
            continue
        rows = daily_rows(s)
        closes = [rows[d][0] for d in sorted(rows) if d < day]
        if len(closes) < scan.MIN_DAILY_BARS or closes[-1] < wl["min_price"]:
            continue
        if max(d for d in rows if d < day) != prev:
            continue  # no close on the day before: halted or new
        tags, _ = scan.find_setups(closes)
        if tags:
            cands.append((sym, tags))
    ranked = []
    for sym, tags in cands:
        ch = data.chart(sym, "6mo", "1d")
        if "error" in ch:
            continue
        rows = daily_rows(ch)
        if prev not in rows or rows[prev][1] is None:
            continue
        c, v = rows[prev]
        if c < wl["min_price"] or v < wl["min_volume"]:
            continue
        ranked.append({"symbol": sym, "setups": tags, "close_prev": round(c, 2), "volume_prev": int(v),
                       "dollar_vol_prev": round(c * v)})
    ranked.sort(key=lambda x: (-x["dollar_vol_prev"], x["symbol"]))
    return ranked[:wl["size"]], len(cands)


def find_trigger(bars, day, cfg, vcfg, spy):
    n_or = cfg["or_minutes"] // 5
    if any(k not in bars for k in range(n_or)):
        return None, "Range-Bars fehlen"
    rng = [bars[k] for k in range(n_or)]
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    avg_v = sum(b["v"] for b in rng) / float(n_or)
    s = scan.session(day)
    close_dt = datetime.datetime.combine(day, s[1])
    before = datetime.datetime.combine(day, hhmm(cfg["no_entries_before"]))
    after = datetime.datetime.combine(day, hhmm(cfg["no_entries_after"]))
    if s[1] != scan.CLOSE:
        after = min(after, close_dt - datetime.timedelta(minutes=30))
    spy_open = spy[0]["o"] if spy and 0 in spy else None
    for k in sorted(bars):
        b = bars[k]
        if k < n_or or b["end"] < before or b["end"] > after:
            continue
        if b["c"] <= oh:
            continue
        vx = b["v"] / avg_v if avg_v else None
        if vcfg.get("min_volume_x") and (vx is None or vx < vcfg["min_volume_x"]):
            continue
        if vcfg.get("spy_above_open"):
            sb = spy.get(k) if spy else None
            if sb is None or spy_open is None or sb["c"] <= spy_open:
                continue
        return {"slot": k, "time": b["end"], "price": round(b["c"], 2), "or_high": round(oh, 2),
                "or_low": round(ol, 2), "volume_x": round(vx, 2) if vx else None}, ""
    return None, "kein Trigger"


def make_plan(trig, cfg):
    entry = trig["price"]
    stop = floor_cent((trig["or_high"] + trig["or_low"]) / 2.0)
    risk = round(entry - stop, 2)
    if risk <= 0:
        return None, "Stop nicht unter Einstieg"
    if risk < entry * cfg["min_risk_pct"] / 100.0:
        return None, "Stop zu eng"
    target = r2(entry + cfg["target_r"] * risk)
    if (target - entry) / risk < cfg["min_reward_risk"] - 1e-9:
        return None, "CRV unter Minimum"
    return {"entry": entry, "stop": stop, "target": target, "risk": risk}, ""


def size(plan, cfg, cash_free):
    e, risk = plan["entry"], plan["risk"]
    n = min(int(cfg["max_loss_per_trade"] // risk + 1e-9),
            int(cfg["capital"] * cfg["max_position_pct"] / 100.0 // e),
            int(cfg["max_order_usd"] // e),
            int(max(cash_free, 0) // e))
    if n < 1:
        return 0, "0 Stueck (Cash/Limit)"
    if n * e < cfg["min_order_usd"]:
        return 0, "Order unter Minimum"
    return n, ""


def simulate(plan, trig, bars, day, cfg):
    s = scan.session(day)
    close_dt = datetime.datetime.combine(day, s[1])
    flat = datetime.datetime.combine(day, hhmm(cfg["flat_by"]))
    if s[1] != scan.CLOSE:
        flat = min(flat, close_dt - datetime.timedelta(minutes=5))
    stop, target = plan["stop"], plan["target"]
    last = None
    for k in sorted(bars):
        if k <= trig["slot"]:
            continue
        b = bars[k]
        if b["start"] >= flat:
            break
        last = b
        if b["o"] <= stop:
            return b["o"], "stop_gap", b["end"], ""
        if b["l"] <= stop:
            return stop, "stop", b["end"], ""
        if b["o"] >= target:
            return target, "target", b["end"], ""  # limit order: no better fill assumed
        if b["h"] >= target:
            return target, "target", b["end"], ""
        if b["end"] >= flat:
            return b["c"], "flat", b["end"], ""
    if last is None:
        return plan["entry"], "flat", trig["time"], "keine Bars nach Einstieg"
    note = "" if last["end"] >= flat else "Bars enden vor flat_by (%s)" % label(last["end"])
    return last["c"], "flat", last["end"], note


def run_tm(data, sym, day, plan, trig, cfg):
    tm = cfg.get("timemachine") or {}
    try:
        f = data.chart_file(sym)
    except scan.ScanError as e:
        return None, str(e)
    cmd = [sys.executable, "-I", os.path.join(ROOT, "trader-timemachine", "timemachine.py"), "run",
           "--symbol", sym, "--date", day.isoformat(), "--entry", "%.2f" % plan["entry"],
           "--stop", "%.2f" % plan["stop"], "--target", "%.2f" % plan["target"],
           "--entry-time", trig["time"].strftime("%I:%M %p").lstrip("0"), "--until", cfg["flat_by"],
           "--offline", f, "--json", "--now", "%s 16:30" % day.isoformat(),
           "--replays", str(tm.get("replays", 10000)), "--seed", str(tm.get("seed", 19))]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return None, "Time Machine Timeout"
    try:
        doc = json.loads(out.stdout)
    except ValueError:
        return None, (out.stdout or out.stderr).strip().splitlines()[-1][:160] if (out.stdout or out.stderr) else "?"
    return doc, ""


def tm_fields(doc):
    if not doc:
        return {}
    mc, hist = doc.get("mc") or {}, doc.get("history") or {}

    def pct(x):
        return None if x is None else round(100.0 * x, 1)
    return {"tm_target_pct": pct(mc.get("p_target")), "tm_stop_pct": pct(mc.get("p_stop")),
            "tm_neither_pct": pct(mc.get("p_neither")), "tm_avg_r": mc.get("avg_R"),
            "tm_hist_avg_r": hist.get("avg_R_net"), "tm_verdict": (doc.get("verdict") or "")[:200]}


def run_day(day, data, symbols, spark, cfg, use_tm, tm_cache):
    log("%s: Watchlist ..." % day.isoformat())
    wl, n_cands = build_watchlist(day, symbols, spark, data, cfg)
    spy = day_bars(data.chart("SPY", "60d", "5m"), day)
    bars_by = {}
    missing = []
    for w in wl:
        ch = data.chart(w["symbol"], "60d", "5m")
        b = day_bars(ch, day) if "error" not in ch else {}
        if not b:
            missing.append(w["symbol"])
        bars_by[w["symbol"]] = b
    trades, days, detail = [], [], {"date": day.isoformat(), "setup_candidates": n_cands,
                                    "watchlist": wl, "missing_5m": missing, "variants": {}}
    spy_o = spy[0]["o"] if 0 in spy else None
    spy_c = spy[max(spy)]["c"] if spy else None
    for vname, vcfg in sorted(cfg["variants"].items()):
        cands = []
        for w in wl:
            b = bars_by[w["symbol"]]
            if not b:
                continue
            trig, why = find_trigger(b, day, cfg, vcfg, spy)
            if trig:
                cands.append((trig["time"], -w["dollar_vol_prev"], w, trig))
        cands.sort(key=lambda x: (x[0], x[1]))
        taken, skipped, open_pos, day_risk = [], [], [], 0.0
        for _, _, w, trig in cands:
            sym = w["symbol"]
            if len(taken) >= cfg["max_trades_per_day"]:
                skipped.append({"symbol": sym, "time": label(trig["time"]), "why": "max. Trades/Tag erreicht"})
                continue
            plan, why = make_plan(trig, cfg)
            if not plan:
                skipped.append({"symbol": sym, "time": label(trig["time"]), "why": why})
                continue
            if day_risk + cfg["max_loss_per_trade"] > cfg["max_daily_loss"] + 1e-9:
                skipped.append({"symbol": sym, "time": label(trig["time"]), "why": "Tagesverlust-Limit"})
                continue
            used = sum(p["notional"] for p in open_pos if p["exit_t"] > trig["time"])
            cash_free = cfg["capital"] * (1 - cfg["cash_buffer_pct"] / 100.0) - used
            n, why = size(plan, cfg, cash_free)
            if not n:
                skipped.append({"symbol": sym, "time": label(trig["time"]), "why": why})
                continue
            px, reason, exit_t, note = simulate(plan, trig, bars_by[sym], day, cfg)
            c = cfg["costs"]
            slip = c["slippage_pct"] / 100.0
            fill_in = plan["entry"] * (1 + slip)
            fill_out = px if reason == "target" else px * (1 - slip)
            comm = 2 * max(c["commission_min"], n * c["commission_per_share"])
            gross = (px - plan["entry"]) * n
            net = (fill_out - fill_in) * n - comm
            costs = gross - net
            row = {"date": day.isoformat(), "variant": vname, "symbol": sym, "setups": "+".join(w["setups"]),
                   "dollar_vol_prev": w["dollar_vol_prev"], "or_high": trig["or_high"], "or_low": trig["or_low"],
                   "trigger_time": label(trig["time"]), "volume_x": trig["volume_x"], "entry": plan["entry"],
                   "stop": plan["stop"], "target": plan["target"], "risk_ps": plan["risk"], "shares": n,
                   "notional": round(n * plan["entry"], 2), "exit_time": label(exit_t), "exit_price": round(px, 2),
                   "exit_reason": reason, "gross_pnl": round(gross, 2), "costs": round(costs, 2),
                   "net_pnl": round(net, 2), "r_net": round(net / (n * plan["risk"]), 3), "data_note": note}
            if use_tm:
                key = (sym, day.isoformat(), plan["entry"], plan["stop"], plan["target"], label(trig["time"]))
                if key not in tm_cache:
                    log("  Time Machine %s %s ..." % (sym, label(trig["time"])))
                    tm_cache[key] = run_tm(data, sym, day, plan, trig, cfg)
                doc, err = tm_cache[key]
                row.update(tm_fields(doc))
                if err:
                    row["data_note"] = (note + "; TM: " + err).strip("; ")
            taken.append(row)
            open_pos.append({"notional": n * plan["entry"], "exit_t": exit_t})
            day_risk += n * plan["risk"]
        trades.extend(taken)
        days.append({"date": day.isoformat(), "variant": vname, "watchlist": len(wl),
                     "with_data": len(wl) - len(missing), "triggers": len(cands), "taken": len(taken),
                     "skipped": len(skipped), "spy_open": spy_o, "spy_close": spy_c,
                     "net_pnl": round(sum(t["net_pnl"] for t in taken), 2),
                     "note": ("5m-Daten fehlen: " + ",".join(missing)) if missing else ""})
        detail["variants"][vname] = {"triggers": [{"symbol": x[2]["symbol"], "time": label(x[0]),
                                                   "price": x[3]["price"]} for x in cands],
                                     "taken": taken, "skipped": skipped}
    return trades, days, detail


# ---------------------------------------------------------------- journal

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


def merge(path, cols, new_rows, dates, sort_key):
    keep = [r for r in read_csv(path) if r["date"] not in dates]
    rows = keep + new_rows
    rows.sort(key=sort_key)
    write_csv(path, cols, rows)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--date", help="YYYY-MM-DD (Default: letzter abgeschlossener Handelstag)")
    p.add_argument("--backfill", type=int, help="die letzten N Handelstage bis --date")
    p.add_argument("--no-tm", action="store_true", help="ohne Time Machine")
    p.add_argument("--config", default=os.path.join(HERE, "config.json"))
    p.add_argument("--cache", help="Cache-Ordner (Default forwardtest/.cache/<heute>)")
    p.add_argument("--limit-universe", type=int, help="nur fuer Tests: erste N Symbole")
    a = p.parse_args(argv)

    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    end = datetime.date.fromisoformat(a.date) if a.date else last_completed_session(now)
    if not scan.trading_day(end)[0]:
        log("%s ist kein Handelstag (%s). Nichts zu tun." % (end, scan.trading_day(end)[1]))
        return 0
    days = [end]
    while a.backfill and len(days) < a.backfill:
        days.insert(0, prev_trading_day(days[0]))
    if (scan.to_eastern(now).date() - days[0]).days > 55:
        log("Yahoo haelt 5-Minuten-Bars nur ca. 60 Tage: %s ist zu alt." % days[0])
        return 1

    cache = a.cache or os.path.join(HERE, ".cache", scan.to_eastern(now).date().isoformat())
    data = Data(cache)
    t0 = time.time()
    log("Universum ...")
    universe = data.universe()
    if a.limit_universe:
        universe = universe[:a.limit_universe]
    sp = data.spark_daily(universe)["data"]
    pre = cfg["watchlist"]
    symbols = [s for s in universe if sp.get(s) and
               ((sp[s].get("meta") or {}).get("regularMarketVolume") or 0) >= pre["prefilter_min_volume"]]
    log("  %d von %d Titeln nach Vorfilter (Volumen >= %s)." % (len(symbols), len(universe),
                                                               pre["prefilter_min_volume"]))
    all_trades, all_days, tm_cache = [], [], {}
    for d in days:
        tr, dy, detail = run_day(d, data, symbols, sp, cfg, not a.no_tm and cfg["timemachine"]["enabled"], tm_cache)
        all_trades += tr
        all_days += dy
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        with open(os.path.join(HERE, "logs", d.isoformat() + ".json"), "w", encoding="utf-8") as fh:
            json.dump(detail, fh, indent=1, default=str)
        for v in dy:
            log("  %s %s: Watchlist %d, Trigger %d, Trades %d, netto %+.2f" % (
                d, v["variant"], v["watchlist"], v["triggers"], v["taken"], v["net_pnl"]))
    dates = {d.isoformat() for d in days}
    merge(os.path.join(HERE, "journal", "trades.csv"), TRADE_COLS, all_trades, dates,
          lambda r: (r["date"], r["variant"], r["trigger_time"], r["symbol"]))
    merge(os.path.join(HERE, "journal", "days.csv"), DAY_COLS, all_days, dates,
          lambda r: (r["date"], r["variant"]))
    log("Fertig in %.0f s: %d Tage, %d Trades." % (time.time() - t0, len(days), len(all_trades)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
