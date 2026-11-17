#!/usr/bin/env python3
"""Paper-Forward-Test des Day-Trader-Skills: ein Handelstag, regelgetreu nachgespielt.

    python3 forwardtest/daily_run.py                    # letzter abgeschlossener US-Handelstag
    python3 forwardtest/daily_run.py --date 2026-11-16  # ein bestimmter Tag
    python3 forwardtest/daily_run.py --backfill 20      # die letzten 20 Handelstage
    python3 forwardtest/daily_run.py --backfill 20 --no-tm   # ohne Time Machine (schneller)
    python3 forwardtest/daily_run.py --backfill 20 --variants C,D,E  # nur diese Varianten nachrechnen

Ablauf je Tag D (nur Daten, die zum jeweiligen Zeitpunkt bekannt waren):
  1. Watchlist vor dem Open: Tages-Setups des Scanners (20-Tage-Hoch, Bull Flag,
     MA20-Rueckeroberung, Oversold, Oversold Bounce) auf den Schlusskursen bis D-1.
     Liquiditaet am Vortag (Kurs >= min_price, Volumen >= min_volume), die
     `watchlist.size` Titel mit dem hoechsten Dollar-Volumen am Vortag.
  2. ORB auf 5-Minuten-Bars von D: Range 9:30-9:45, Trigger = erster Schlusskurs
     ueber dem Range-Hoch zwischen 9:45 und 15:30. Variante B zusaetzlich:
     Trigger-Volumen >= 1,5x Range-Schnitt und SPY ueber seinem Tagesopen.
     Varianten C-E (gleiche Watchlist, nur Long): C VWAP-Pullback, D Mean Reversion
     (Bollinger 20/2,5 + RSI(2)), E Vortages-Hoch/-Tief (PDH-Ausbruch / PDL-Sweep).
     Sie bringen ihren eigenen Stop mit (D auch das Ziel: SMA20). Details: find_*().
     Variante C2 = C mit Stop 1,5x so weit (stop_mult) und ohne Ziel (no_target);
     Variante C3 = C2 plus Trailing-Stop (trail_r, trail_after_r): ab +1R folgt der
     Stop dem Hoch im Abstand 1,5R, nur nach oben, wirksam ab dem naechsten Bar.
     forward_from verhindert einen Backfill vor ihrem Start.
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
import calendar
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
              "volume_x", "signal", "ref_level", "entry", "stop", "target", "risk_ps", "shares", "notional", "exit_time", "exit_price",
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
        self.mem = {}
        os.makedirs(cache, exist_ok=True)

    def _cached(self, name, fetch):
        if name in self.mem:
            return self.mem[name]
        doc = self._load(name, fetch)
        if not name.startswith(("chart_1m/", "chart_60d_5m/")):  # intraday files are large: not kept
            self.mem[name] = doc
        return doc

    def first_bars(self, sym):
        """First 5-minute bar of each day of the 60-day chart, kept in memory (F needs ~600 a day)."""
        key = ("first", sym)
        if key not in self.mem:
            ch = self.chart(sym, "60d", "5m")
            self.mem[key] = None if "error" in ch else _first_bars(ch)
        return self.mem[key]

    def _load(self, name, fetch):
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

    def chart_1m(self, sym, day):
        """1-minute bars of one session (Yahoo keeps them about 30 days)."""
        name = "chart_1m/%s/%s.json" % (day.isoformat(), scan.to_yahoo(sym))
        p1 = calendar.timegm(datetime.datetime.combine(day, datetime.time(12, 0)).timetuple())

        def fetch():
            time.sleep(self.pause)
            path = "/v8/finance/chart/%s?period1=%d&period2=%d&interval=1m" % (scan.to_yahoo(sym), p1, p1 + 10 * 3600)
            try:
                return scan.parse_chart(scan.yahoo_get(path), sym)
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


def day_bars(series, day, step=300):
    """{slot: bar} of complete regular-session bars on `day` (5-minute, or 1-minute with step=60)."""
    s = scan.session(day)
    if not s:
        return {}
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    o, h, l, c, v = (series.get(k) for k in ("o", "h", "l", "c", "v"))
    out = {}
    for i, ts in enumerate(series.get("t") or []):
        if ts % step or c[i] is None or h is None or h[i] is None or l[i] is None:
            continue
        et = scan.ts_to_eastern(ts)
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        out[slot] = {"slot": slot, "start": et, "end": et + datetime.timedelta(seconds=step),
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
                "or_low": round(ol, 2), "volume_x": round(vx, 2) if vx else None, "signal": "orb"}, ""
    return None, "kein Trigger"


# ---------------------------------------------------------------- further strategies (variants C, D, E)
# All long only, on the same watchlist, same sizing, limits, costs and simulation as A/B.
# A trigger of these carries its own "stop" (and for D its own "target"); see make_plan.

def entry_window(day, cfg, vcfg):
    s = scan.session(day)
    close_dt = datetime.datetime.combine(day, s[1])
    before = datetime.datetime.combine(day, hhmm(vcfg.get("no_entries_before", cfg["no_entries_before"])))
    after = datetime.datetime.combine(day, hhmm(vcfg.get("no_entries_after", cfg["no_entries_after"])))
    if s[1] != scan.CLOSE:
        after = min(after, close_dt - datetime.timedelta(minutes=30))
    return before, after


def _seq(prev_bars, bars):
    """Prior session + today as one 5-minute series; today's bars marked."""
    seq = [dict(prev_bars[k], today=False) for k in sorted(prev_bars or {})]
    return seq + [dict(bars[k], today=True) for k in sorted(bars)]


def _vol_x(seq, i, n=20):
    prior = [b["v"] for b in seq[max(0, i - n):i]]
    if len(prior) < n or not sum(prior):
        return None
    return seq[i]["v"] / (sum(prior) / float(len(prior)))


def _trig(b, price, stop, signal, ref, vx, target=None):
    t = {"slot": b["slot"], "time": b["end"], "price": round(price, 2), "or_high": None, "or_low": None,
         "volume_x": round(vx, 2) if vx else None, "signal": signal,
         "ref_level": round(ref, 2) if ref is not None else None, "stop": stop}
    if target is not None:
        t["target"] = target
    return t


def find_vwap_pullback(bars, prev_bars, day, cfg, vcfg):
    """C: in an up day (above the day's open, high of day stretched away from VWAP) the first
    5-minute bar that dips to VWAP from above and closes green above it. Stop below the bar
    low / VWAP, target 2R."""
    if 0 not in bars:
        return None, "Eroeffnungs-Bar fehlt"
    before, after = entry_window(day, cfg, vcfg)
    seq = _seq(prev_bars, bars)
    day_open = bars[0]["o"]
    touch = vcfg.get("touch_pct", 0.1) / 100.0
    stretch_min = vcfg.get("min_stretch_pct", 0.5) / 100.0
    buf = vcfg.get("stop_buffer_pct", 0.05) / 100.0
    pv = vol = 0.0
    hod = prev_c = prev_vwap = None
    for i, b in enumerate(seq):
        if not b["today"]:
            continue
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
        vol += b["v"]
        vwap = pv / vol if vol else None
        if (vwap and hod is not None and prev_vwap and before <= b["end"] <= after
                and prev_c > prev_vwap                       # was trading above VWAP
                and hod >= vwap * (1 + stretch_min)          # had moved away from it: up day
                and b["l"] <= vwap * (1 + touch)             # pulled back to it
                and b["c"] > vwap and b["c"] > b["o"]        # held it, green bar
                and b["c"] > day_open):
            stop = floor_cent(min(b["l"], vwap) * (1 - buf))
            return _trig(b, b["c"], stop, "vwap_pullback", vwap, _vol_x(seq, i)), ""
        hod = b["h"] if hod is None else max(hod, b["h"])
        prev_c, prev_vwap = b["c"], vwap
    return None, "kein Trigger"


def _rsi(closes, n):
    """Wilder RSI, list aligned with closes (None until defined)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = sum(max(x, 0) for x in ch[:n]) / n
    lo = sum(max(-x, 0) for x in ch[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            x = ch[i - 1]
            g = (g * (n - 1) + max(x, 0)) / n
            lo = (lo * (n - 1) + max(-x, 0)) / n
        out[i] = 100.0 if lo == 0 else 100.0 - 100.0 / (1 + g / lo)
    return out


def find_mean_reversion(bars, prev_bars, day, cfg, vcfg):
    """D: a 5-minute close below the lower Bollinger band (20, 2.5 sigma) with RSI(2) < 5, then
    within 3 bars a close above that setup bar's high. Stop below the lowest low since the
    setup, target the middle band (SMA20) at entry. Indicators run across the prior session."""
    before, after = entry_window(day, cfg, vcfg)
    seq = _seq(prev_bars, bars)
    n, m = vcfg.get("bb_period", 20), vcfg.get("bb_std", 2.5)
    rsi_max, confirm = vcfg.get("rsi_max", 5), vcfg.get("confirm_bars", 3)
    buf = vcfg.get("stop_buffer_pct", 0.05) / 100.0
    closes = [b["c"] for b in seq]
    rsi = _rsi(closes, vcfg.get("rsi_period", 2))
    setup = None
    for i, b in enumerate(seq):
        if i < n - 1 or not b["today"]:
            continue
        win = closes[i - n + 1:i + 1]
        sma = sum(win) / n
        sd = math.sqrt(sum((x - sma) ** 2 for x in win) / n)
        if setup and i - setup[0] > confirm:
            setup = None
        if setup and b["c"] > setup[1]:
            if not before <= b["end"] <= after:
                setup = None  # confirmed outside the entry window: signal used up
                continue
            low = min(x["l"] for x in seq[setup[0]:i + 1])
            stop = floor_cent(low * (1 - buf))
            return _trig(b, b["c"], stop, "bb_rsi2", sma, _vol_x(seq, i), target=r2(sma)), ""
        if sd and b["c"] < sma - m * sd and rsi[i] is not None and rsi[i] < rsi_max:
            setup = (i, b["h"])
    return None, "kein Trigger"


def find_pdh_pdl(bars, prev_bars, day, cfg, vcfg):
    """E: prior-day levels of the regular session. Whichever comes first:
    - PDH breakout: day opened below PDH, a 5-minute close crosses above PDH with volume
      >= 1.5x the last 20 bars. Stop below the breakout bar's low.
    - PDL sweep reversal: day opened above PDL, price trades below PDL and within 6 bars
      a close is back above PDL. Stop below the sweep low.
    Target 2R for both. Long only."""
    if not prev_bars:
        return None, "Vortages-Bars fehlen"
    if 0 not in bars:
        return None, "Eroeffnungs-Bar fehlt"
    before, after = entry_window(day, cfg, vcfg)
    pdh = max(b["h"] for b in prev_bars.values())
    pdl = min(b["l"] for b in prev_bars.values())
    seq = _seq(prev_bars, bars)
    day_open = bars[0]["o"]
    vmin, max_bars = vcfg.get("breakout_volume_x", 1.5), vcfg.get("sweep_max_bars", 6)
    buf = vcfg.get("stop_buffer_pct", 0.05) / 100.0
    sweep = None  # (index, lowest low)
    prev_c = None
    for i, b in enumerate(seq):
        if not b["today"]:
            continue
        ok = before <= b["end"] <= after
        vx = _vol_x(seq, i)
        if day_open < pdh and ok and b["c"] > pdh and (prev_c is None or prev_c <= pdh) and vx and vx >= vmin:
            return _trig(b, b["c"], floor_cent(b["l"] * (1 - buf)), "pdh_breakout", pdh, vx), ""
        if day_open > pdl:
            if sweep and i - sweep[0] > max_bars:
                sweep = None
            if b["l"] < pdl:
                sweep = (sweep[0], min(sweep[1], b["l"])) if sweep else (i, b["l"])
            if sweep and b["c"] > pdl:
                if ok:
                    return _trig(b, b["c"], floor_cent(sweep[1] * (1 - buf)), "pdl_reclaim", pdl, vx), ""
                sweep = None  # reclaimed outside the entry window: signal used up
        prev_c = b["c"]
    return None, "kein Trigger"


STRATEGIES = {"vwap_pullback": find_vwap_pullback, "mean_reversion": find_mean_reversion,
              "pdh_pdl": find_pdh_pdl}


def find_any_trigger(bars, prev_bars, day, cfg, vcfg, spy):
    strat = vcfg.get("strategy", "orb")
    if strat == "orb":
        return find_trigger(bars, day, cfg, vcfg, spy)
    return STRATEGIES[strat](bars, prev_bars, day, cfg, vcfg)


def make_plan(trig, cfg, vcfg=None):
    vcfg = vcfg or {}
    entry = trig["price"]
    if trig.get("stop") is not None:
        stop = trig["stop"]
    else:
        stop = floor_cent((trig["or_high"] + trig["or_low"]) / 2.0)
    if vcfg.get("stop_mult"):  # wider stop, same $ risk (fewer shares)
        stop = floor_cent(entry - vcfg["stop_mult"] * (entry - stop))
    risk = round(entry - stop, 2)
    if risk <= 0:
        return None, "Stop nicht unter Einstieg"
    if risk < entry * cfg["min_risk_pct"] / 100.0:
        return None, "Stop zu eng"
    if vcfg.get("no_target"):  # hold until stop or flat_by
        return {"entry": entry, "stop": stop, "target": None, "risk": risk}, ""
    target = trig["target"] if trig.get("target") is not None else r2(entry + cfg["target_r"] * risk)
    if target <= entry:
        return None, "Ziel nicht ueber Einstieg"
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


def simulate(plan, trig, bars, day, cfg, vcfg=None):
    vcfg = vcfg or {}
    trail, trail_after = vcfg.get("trail_r"), vcfg.get("trail_after_r", 0.0)
    s = scan.session(day)
    close_dt = datetime.datetime.combine(day, s[1])
    flat = datetime.datetime.combine(day, hhmm(cfg["flat_by"]))
    if s[1] != scan.CLOSE:
        flat = min(flat, close_dt - datetime.timedelta(minutes=5))
    stop, target = plan["stop"], plan["target"]
    hi = None
    last = None
    for k in sorted(bars):
        if k <= trig["slot"]:
            continue
        b = bars[k]
        if b["start"] >= flat:
            break
        last = b
        trailed = stop > plan["stop"]
        if b["o"] <= stop:
            return b["o"], "trail_gap" if trailed else "stop_gap", b["end"], ""
        if b["l"] <= stop:
            return stop, "trail" if trailed else "stop", b["end"], ""
        if target is not None and b["o"] >= target:
            return target, "target", b["end"], ""  # limit order: no better fill assumed
        if target is not None and b["h"] >= target:
            return target, "target", b["end"], ""
        if b["end"] >= flat:
            return b["c"], "flat", b["end"], ""
        if trail:  # trailing stop: moves up after this bar, never down, active from +trail_after R
            hi = b["h"] if hi is None else max(hi, b["h"])
            if hi >= plan["entry"] + trail_after * plan["risk"]:
                stop = max(stop, floor_cent(hi - trail * plan["risk"]))
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


# ---------------------------------------------------------------- F and G: published intraday strategies
# Own selection, long AND short, own sizing (paper rules), no Time Machine.

def book(entry, px, n, side, reason, costs):
    """Gross/net P&L of one round trip, side +1 long / -1 short, our cost model."""
    sl = costs["slippage_pct"] / 100.0
    fill_in = entry * (1 + side * sl)
    fill_out = px if reason == "target" else px * (1 - side * sl)
    comm = 2 * max(costs["commission_min"], n * costs["commission_per_share"])
    gross = side * (px - entry) * n
    net = side * (fill_out - fill_in) * n - comm
    return round(gross, 2), round(gross - net, 2), round(net, 2)


def exit_sided(bars, k_entry, stop, side, flat_dt, entry_bar="path"):
    """Stop or close for a position opened inside bar k_entry. Inside the entry bar the order of
    high and low is unknown. "path" (default): a green bar runs open-low-high-close, a red bar
    open-high-low-close; the stop counts only if the price reaches it after the fill.
    "conservative": any touch of the stop in the entry bar counts. "optimistic": never."""
    last = None
    for k in sorted(bars):
        if k < k_entry:
            continue
        b = bars[k]
        if b["start"] >= flat_dt:
            break
        last = b
        hit = b["l"] <= stop if side > 0 else b["h"] >= stop
        if k == k_entry and entry_bar != "conservative":
            green = b["c"] >= b["o"]
            if entry_bar == "optimistic":
                hit = False
            elif side > 0:   # long fill on the way up: green bar has its low before the high
                hit = (b["c"] <= stop) if green else (b["l"] <= stop)
            else:            # short fill on the way down: red bar has its high before the low
                hit = (b["h"] >= stop) if green else (b["c"] >= stop)
        if k > k_entry and (b["o"] <= stop if side > 0 else b["o"] >= stop):
            return b["o"], "stop_gap", b["end"]
        if hit:
            return stop, "stop", b["end"]
        if b["end"] >= flat_dt:
            return b["c"], "flat", b["end"]
    return (last["c"], "flat", last["end"]) if last else (None, "", None)


def _ohlcv_days(series):
    """{date: (o, h, l, c, v)} of a daily series."""
    out = {}
    for i, ts in enumerate(series.get("t") or []):
        vals = [series.get(k)[i] if series.get(k) else None for k in ("o", "h", "l", "c", "v")]
        if None in vals[:4]:
            continue
        out[et_date(ts)] = tuple(float(x or 0) for x in vals)
    return out


def _first_bars(series):
    """{date: first regular 5-minute bar (9:30)} of an intraday series."""
    out = {}
    o, h, l, c, v = (series.get(k) for k in ("o", "h", "l", "c", "v"))
    for i, ts in enumerate(series.get("t") or []):
        et = scan.ts_to_eastern(ts)
        if et.hour == 9 and et.minute == 30 and c[i] is not None and o[i] is not None:
            out[et.date()] = {"o": float(o[i]), "h": float(h[i]), "l": float(l[i]), "c": float(c[i]),
                              "v": (v[i] or 0) if v else 0}
    return out


def run_sip_orb(day, data, symbols, spark, cfg, vcfg, vname):
    """F: 5-minute ORB on Stocks in Play (Zarattini, Barbon, Aziz 2024).
    Universe: open > 5 $, 14-day average volume >= 1M, 14-day ATR > 0.50 $.
    Stocks in Play: the 20 with the highest relative volume of the first 5 minutes
    (vs. the average first 5 minutes of the 14 prior days), at least 100 %.
    Green first candle: long only, stop order at its high. Red: short only, at its low.
    Doji: no trade. Stop 10 % of ATR from the fill. Exit at the 16:00 close.
    Size: 1 % of capital at risk, total exposure at most `leverage` x capital, order >= min_order_usd.
    Entry and exit run on 1-minute bars (the stop is only cents away); without them no trade."""
    look, top = vcfg.get("lookback", 14), vcfg.get("top_n", 20)
    uni = {}
    for sym in symbols:
        sp = spark.get(sym)  # closes only: cheap pre-check on the price
        if not sp:
            continue
        closes = [c for c in (sp.get("c") or []) if c is not None]
        if not closes or max(closes[-look - 1:]) <= vcfg.get("min_price", 5):
            continue
        ch = data.chart(sym, "6mo", "1d")  # daily high/low/volume for ATR and average volume
        if "error" in ch:
            continue
        rows = _ohlcv_days(ch)
        ds = [d for d in sorted(rows) if d < day]
        if len(ds) < look + 1 or ds[-1] != prev_trading_day(day):
            continue
        last = ds[-look:]
        avg_v = sum(rows[d][4] for d in last) / look
        trs = []
        for d0, d1 in zip(ds[-look - 1:-1], last):
            h, l, pc = rows[d1][1], rows[d1][2], rows[d0][3]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / look
        if avg_v >= vcfg.get("min_avg_volume", 1e6) and atr > vcfg.get("min_atr", 0.5) and rows[ds[-1]][3] > 0:
            uni[sym] = atr
    cands = []
    for sym in sorted(uni):
        fb = data.first_bars(sym)
        if not fb or day not in fb or fb[day]["o"] <= vcfg.get("min_price", 5):
            continue
        prior = [d for d in sorted(fb) if d < day][-look:]
        if len(prior) < look:
            continue
        avg0 = sum(fb[d]["v"] for d in prior) / float(look)
        if not avg0:
            continue
        rv = fb[day]["v"] / avg0
        if rv >= vcfg.get("min_rel_volume", 1.0):
            cands.append((rv, sym))
    cands.sort(key=lambda x: (-x[0], x[1]))
    play = cands[:top]
    s = scan.session(day)
    flat_dt = datetime.datetime.combine(day, s[1])
    trigs, skipped = [], []
    for rv, sym in play:
        bars = day_bars(data.chart(sym, "60d", "5m"), day)
        if 0 not in bars:
            skipped.append({"symbol": sym, "why": "Eroeffnungs-Bar fehlt"})
            continue
        b0 = bars[0]
        side = 1 if b0["c"] > b0["o"] else -1 if b0["c"] < b0["o"] else 0
        if not side:
            skipped.append({"symbol": sym, "why": "Doji"})
            continue
        level = b0["h"] if side > 0 else b0["l"]
        m1 = data.chart_1m(sym, day)
        bars = day_bars(m1, day, step=60) if "error" not in m1 else {}
        if not bars:
            skipped.append({"symbol": sym, "why": "1-Min-Daten fehlen"})
            continue
        for k in sorted(bars):
            b = bars[k]
            if k < 5 or b["start"] >= flat_dt:  # stop order is live from 9:35
                continue
            if (side > 0 and b["h"] >= level) or (side < 0 and b["l"] <= level):
                fill = max(b["o"], level) if side > 0 else min(b["o"], level)
                trigs.append((b["start"], -rv, sym, side, round(fill, 2), k, b, b0, bars))
                break
    trigs.sort(key=lambda x: (x[0], x[1]))
    lev, capital = vcfg.get("leverage", 4), cfg["capital"]
    risk_usd = capital * vcfg.get("risk_pct", 1.0) / 100.0
    trades, open_pos = [], []
    for start, nrv, sym, side, entry, k, b, b0, bars in trigs:
        atr = uni[sym]
        dist = vcfg.get("stop_atr", 0.10) * atr
        stop = floor_cent(entry - dist) if side > 0 else math.ceil((entry + dist) * 100 - 1e-9) / 100.0
        risk = round(side * (entry - stop), 2)
        if risk <= 0:
            skipped.append({"symbol": sym, "why": "Stop nicht hinter Einstieg"})
            continue
        used = sum(p["notional"] for p in open_pos if p["exit_t"] > b["start"])
        n = min(int(risk_usd // risk + 1e-9), int(max(capital * lev - used, 0) // entry))
        if n < 1:
            skipped.append({"symbol": sym, "time": label(b["start"]), "why": "0 Stueck (Hebel-Limit)"})
            continue
        if n * entry < cfg["min_order_usd"]:
            skipped.append({"symbol": sym, "time": label(b["start"]), "why": "Order unter Minimum"})
            continue
        px, reason, exit_t = exit_sided(bars, k, stop, side, flat_dt, vcfg.get("entry_bar", "path"))
        gross, costs, net = book(entry, px, n, side, reason, cfg["costs"])
        trades.append({"date": day.isoformat(), "variant": vname, "symbol": sym,
                       "setups": "%s RV %.1fx" % ("long" if side > 0 else "short", -nrv),
                       "dollar_vol_prev": None, "or_high": round(b0["h"], 2), "or_low": round(b0["l"], 2),
                       "trigger_time": label(b["start"]), "volume_x": round(-nrv, 2),
                       "signal": "sip_long" if side > 0 else "sip_short", "ref_level": round(b0["h"] if side > 0 else b0["l"], 2),
                       "entry": entry, "stop": stop, "target": None, "risk_ps": risk, "shares": n,
                       "notional": round(n * entry, 2), "exit_time": label(exit_t), "exit_price": round(px, 2),
                       "exit_reason": reason, "gross_pnl": gross, "costs": costs, "net_pnl": net,
                       "r_net": round(net / (n * risk), 3), "data_note": "1-Min-Bars, Stop-Order ab 9:35"})
        open_pos.append({"notional": n * entry, "exit_t": exit_t})
    day_row = {"watchlist": len(play), "with_data": len(play), "triggers": len(trigs), "taken": len(trades),
               "skipped": len(skipped)}
    detail = {"universe": len(uni), "in_play": [{"symbol": sym, "rv": round(rv, 2)} for rv, sym in play],
              "triggers": [{"symbol": t[2], "time": label(t[0]), "side": t[3], "price": t[4]} for t in trigs],
              "taken": trades, "skipped": skipped}
    return trades, day_row, detail


def run_spy_momentum(day, data, symbols, spark, cfg, vcfg, vname):
    """G: SPY intraday momentum (Zarattini, Aziz, Barbon 2024).
    Noise area per time of day: sigma = mean over 14 prior days of |close(time) / open - 1|.
    Upper = max(open, prior close) * (1 + sigma), lower = min(open, prior close) * (1 - sigma).
    Checks only at HH:00 and HH:30 from 10:00: above upper -> long, below lower -> short.
    Trailing stop max(upper, VWAP) for longs, min(lower, VWAP) for shorts, checked at the same
    times; a stop can reverse into the other side. Flat at the 16:00 close.
    Size: capital * min(4, 2 % / 14-day daily volatility of SPY) / open, fixed for the day.
    Our choices: 5-minute bars (paper: 1-minute); no entry if the price is already beyond VWAP
    against the trade (the stop would sit on the wrong side)."""
    sym = vcfg.get("symbol", "SPY")
    look = vcfg.get("lookback", 14)
    ch = data.chart(sym, "60d", "5m")
    dch = data.chart(sym, "6mo", "1d")
    empty = ([], {"watchlist": 1, "with_data": 0, "triggers": 0, "taken": 0, "skipped": 0}, {"note": ""})
    if "error" in ch or "error" in dch:
        empty[2]["note"] = "SPY-Daten fehlen"
        return empty
    bars = day_bars(ch, day)
    days_in = sorted({et_date(ts) for ts in ch.get("t") or []})
    prior = [d for d in days_in if d < day][-look:]
    drows = _ohlcv_days(dch)
    dcl = [drows[d][3] for d in sorted(drows) if d < day]
    if 0 not in bars or len(prior) < look or len(dcl) < look + 1:
        empty[2]["note"] = "zu wenig Historie oder Eroeffnungs-Bar fehlt"
        return empty
    pbars = {d: day_bars(ch, d) for d in prior}
    rets = [dcl[i] / dcl[i - 1] - 1 for i in range(len(dcl) - look, len(dcl))]
    mu = sum(rets) / look
    vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / (look - 1))
    o, pc = bars[0]["o"], dcl[-1]
    n = int(cfg["capital"] * min(vcfg.get("max_leverage", 4), vcfg.get("target_vol", 0.02) / vol) // o)
    s = scan.session(day)
    open_dt = datetime.datetime.combine(day, s[0])
    flat_dt = datetime.datetime.combine(day, s[1])
    checks = []
    t = datetime.datetime.combine(day, hhmm(vcfg.get("first_check", "10:00")))
    while t < flat_dt:
        checks.append(int((t - open_dt).total_seconds()) // 300 - 1)  # slot of the bar ending at t
        t += datetime.timedelta(minutes=30)
    costs = dict(cfg["costs"], **(vcfg.get("costs") or {}))
    pv = vv = 0.0
    vwap = {}
    for k in sorted(bars):
        b = bars[k]
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
        vv += b["v"]
        vwap[k] = pv / vv if vv else b["c"]
    trades, pos, entries = [], None, 0

    def close(p, px, k, reason):
        g, c, net = book(p["entry"], px, n, p["side"], reason, costs)
        trades.append({"date": day.isoformat(), "variant": vname, "symbol": sym,
                       "setups": "%s Noise Area" % ("long" if p["side"] > 0 else "short"), "dollar_vol_prev": None,
                       "or_high": p["ub"], "or_low": p["lb"], "trigger_time": p["time"], "volume_x": None,
                       "signal": "spy_mom_long" if p["side"] > 0 else "spy_mom_short", "ref_level": p["ref"],
                       "entry": p["entry"], "stop": p["stop"], "target": None, "risk_ps": p["risk"], "shares": n,
                       "notional": round(n * p["entry"], 2), "exit_time": label(bars[k]["end"]),
                       "exit_price": round(px, 2), "exit_reason": reason, "gross_pnl": g, "costs": c, "net_pnl": net,
                       "r_net": round(net / (n * p["risk"]), 3),
                       "data_note": "Hebel %.2fx (Tagesvola %.2f %%)" % (n * o / cfg["capital"], 100 * vol)})

    for k in checks:
        if k not in bars:
            continue
        mv = [abs(pbars[d][k]["c"] / pbars[d][0]["o"] - 1) for d in prior if k in pbars[d] and 0 in pbars[d]]
        if len(mv) < look // 2:
            continue
        sig = sum(mv) / len(mv)
        ub, lb = max(o, pc) * (1 + sig), min(o, pc) * (1 - sig)
        px, vw = bars[k]["c"], vwap[k]
        if pos:
            stop = max(ub, vw) if pos["side"] > 0 else min(lb, vw)
            if (pos["side"] > 0 and px < stop) or (pos["side"] < 0 and px > stop):
                close(pos, px, k, "trail")
                pos = None
        if not pos and n >= 1:
            side = 1 if px > ub else -1 if px < lb else 0
            if side and side * (px - vw) > 0:
                st = max(ub, vw) if side > 0 else min(lb, vw)
                risk = max(round(side * (px - st), 2), 0.01)
                pos = {"side": side, "entry": round(px, 2), "stop": round(st, 2), "risk": risk, "ub": round(ub, 2),
                       "lb": round(lb, 2), "ref": round(ub if side > 0 else lb, 2), "time": label(bars[k]["end"])}
                entries += 1
    if pos:
        last = max(k for k in bars if bars[k]["start"] < flat_dt)
        close(pos, bars[last]["c"], last, "flat")
    day_row = {"watchlist": 1, "with_data": 1, "triggers": entries, "taken": len(trades), "skipped": 0}
    return trades, day_row, {"shares": n, "daily_vol_pct": round(100 * vol, 3), "prior_close": pc, "taken": trades}


SPECIAL = {"sip_orb": run_sip_orb, "spy_momentum": run_spy_momentum}


def run_day(day, data, symbols, spark, cfg, use_tm, tm_cache, frozen=None):
    if frozen:
        log("%s: Watchlist aus dem Log (eingefroren) ..." % day.isoformat())
        wl, n_cands = frozen["watchlist"], frozen.get("setup_candidates")
    else:
        log("%s: Watchlist ..." % day.isoformat())
        wl, n_cands = build_watchlist(day, symbols, spark, data, cfg)
    spy = day_bars(data.chart("SPY", "60d", "5m"), day)
    prev = prev_trading_day(day)
    bars_by, prev_by = {}, {}
    missing = []
    for w in wl:
        ch = data.chart(w["symbol"], "60d", "5m")
        b = day_bars(ch, day) if "error" not in ch else {}
        if not b:
            missing.append(w["symbol"])
        bars_by[w["symbol"]] = b
        prev_by[w["symbol"]] = day_bars(ch, prev) if "error" not in ch else {}
    trades, days, detail = [], [], {"date": day.isoformat(), "setup_candidates": n_cands,
                                    "watchlist": wl, "missing_5m": missing, "variants": {}}
    spy_o = spy[0]["o"] if 0 in spy else None
    spy_c = spy[max(spy)]["c"] if spy else None
    for vname, vcfg in sorted(cfg["variants"].items()):
        if vcfg.get("forward_from") and day.isoformat() < vcfg["forward_from"]:
            continue  # forward-only variant: no backfill before its start
        if vcfg.get("strategy") in SPECIAL:
            tr, drow, det = SPECIAL[vcfg["strategy"]](day, data, symbols, spark, cfg, vcfg, vname)
            trades.extend(tr)
            days.append(dict(drow, date=day.isoformat(), variant=vname, spy_open=spy_o, spy_close=spy_c,
                             net_pnl=round(sum(t["net_pnl"] for t in tr), 2), note=det.get("note", "")))
            detail["variants"][vname] = det
            continue
        cands = []
        for w in wl:
            b = bars_by[w["symbol"]]
            if not b:
                continue
            trig, why = find_any_trigger(b, prev_by[w["symbol"]], day, cfg, vcfg, spy)
            if trig:
                cands.append((trig["time"], -w["dollar_vol_prev"], w, trig))
        cands.sort(key=lambda x: (x[0], x[1]))
        taken, skipped, open_pos, day_risk = [], [], [], 0.0
        for _, _, w, trig in cands:
            sym = w["symbol"]
            if len(taken) >= cfg["max_trades_per_day"]:
                skipped.append({"symbol": sym, "time": label(trig["time"]), "why": "max. Trades/Tag erreicht"})
                continue
            plan, why = make_plan(trig, cfg, vcfg)
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
            px, reason, exit_t, note = simulate(plan, trig, bars_by[sym], day, cfg, vcfg)
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
                   "trigger_time": label(trig["time"]), "volume_x": trig["volume_x"],
                   "signal": trig.get("signal"), "ref_level": trig.get("ref_level"), "entry": plan["entry"],
                   "stop": plan["stop"], "target": plan["target"], "risk_ps": plan["risk"], "shares": n,
                   "notional": round(n * plan["entry"], 2), "exit_time": label(exit_t), "exit_price": round(px, 2),
                   "exit_reason": reason, "gross_pnl": round(gross, 2), "costs": round(costs, 2),
                   "net_pnl": round(net, 2), "r_net": round(net / (n * plan["risk"]), 3), "data_note": note}
            if use_tm and plan["target"] is None:
                row["data_note"] = (note + "; TM: ohne festes Ziel nicht anwendbar").strip("; ")
            elif use_tm:
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
                                                   "price": x[3]["price"], "signal": x[3].get("signal")}
                                                  for x in cands],
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


def merge(path, cols, new_rows, dates, sort_key, variants=None):
    keep = [r for r in read_csv(path) if r["date"] not in dates or (variants and r["variant"] not in variants)]
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
    p.add_argument("--variants", help="nur diese Varianten neu rechnen, z.B. C,D,E. Andere Varianten im "
                   "Journal bleiben unberuehrt; liegt fuer den Tag ein Log vor, wird dessen Watchlist genutzt")
    a = p.parse_args(argv)

    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    only = None
    if a.variants:
        only = {v.strip() for v in a.variants.split(",") if v.strip()}
        unknown = only - set(cfg["variants"])
        if unknown:
            log("Unbekannte Variante(n): %s" % ",".join(sorted(unknown)))
            return 1
        cfg["variants"] = {k: v for k, v in cfg["variants"].items() if k in only}
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
        log_path = os.path.join(HERE, "logs", d.isoformat() + ".json")
        old = None
        if only and os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as fh:
                old = json.load(fh)
        tr, dy, detail = run_day(d, data, symbols, sp, cfg, not a.no_tm and cfg["timemachine"]["enabled"], tm_cache,
                                 frozen=old)
        all_trades += tr
        all_days += dy
        if old:
            old.setdefault("variants", {}).update(detail["variants"])
            detail = old
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(detail, fh, indent=1, default=str)
        for v in dy:
            log("  %s %s: Watchlist %d, Trigger %d, Trades %d, netto %+.2f" % (
                d, v["variant"], v["watchlist"], v["triggers"], v["taken"], v["net_pnl"]))
    dates = {d.isoformat() for d in days}
    merge(os.path.join(HERE, "journal", "trades.csv"), TRADE_COLS, all_trades, dates,
          lambda r: (r["date"], r["variant"], r["trigger_time"], r["symbol"]), only)
    merge(os.path.join(HERE, "journal", "days.csv"), DAY_COLS, all_days, dates,
          lambda r: (r["date"], r["variant"]), only)
    log("Fertig in %.0f s: %d Tage, %d Trades." % (time.time() - t0, len(days), len(all_trades)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
