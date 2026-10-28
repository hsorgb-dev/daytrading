"""Bar data for the backtester: Yahoo (free, ~60 days of 5-minute bars) and
Alpaca (years of 1-minute bars, needs API keys). Both return the same shape:

    {symbol: {"YYYY-MM-DD": [bar, bar, ...]}}   regular session only, oldest first
    bar = {"t": "HH:MM" (New York start time), "slot": minutes since 9:30,
           "o", "h", "l", "c", "v"}

Everything is cached under a cache folder (one JSON file per symbol, source and
interval), so a rerun never refetches. Stdlib only.
"""

import datetime
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
UA = "Mozilla/5.0 (backtest; hsorgb-dev/daytrading)"


class DataError(Exception):
    pass


def _get(url, headers=None, timeout=30, tries=5):
    """GET with retries on dropped connections and timeouts (HTTP errors are raised
    to the caller, which handles 429s itself)."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def _cache_path(cache, source, sym, interval, tag=""):
    os.makedirs(cache, exist_ok=True)
    return os.path.join(cache, "%s-%s-%s%s.json.gz" % (source, sym.replace(".", "-"), interval, tag))


def _load_cache(path):
    if os.path.exists(path):
        with gzip.open(path, "rt") as f:
            return json.load(f)
    return None


def _save_cache(path, doc):
    with gzip.open(path, "wt") as f:
        json.dump(doc, f)


def _add(days, local, o, h, l, c, v, step):
    """Put one bar into days if it is a complete regular-session bar on the grid."""
    mins = local.hour * 60 + local.minute
    if mins < OPEN_MIN or mins + step > CLOSE_MIN or local.second or (mins - OPEN_MIN) % step:
        return
    if None in (o, h, l, c):
        return
    days.setdefault(local.date().isoformat(), {})[mins - OPEN_MIN] = {
        "t": "%02d:%02d" % (mins // 60, mins % 60), "slot": mins - OPEN_MIN,
        "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": int(v or 0)}


def _finish(days):
    return {d: [s[k] for k in sorted(s)] for d, s in sorted(days.items())}


# ------------------------------------------------------------------ Yahoo

def yahoo_bars(sym, interval="5m", rng="60d", cache="bt_cache", refresh=False, pause=0.4):
    """Yahoo v8 chart, regular session. 5m: up to 60 days. 1m: up to 8 days."""
    path = _cache_path(cache, "yahoo", sym, interval, "-" + rng)
    if not refresh:
        hit = _load_cache(path)
        if hit is not None:
            return hit
    step = int(interval.rstrip("m"))
    ysym = sym.replace(".", "-")
    last = None
    for host in ("query1", "query2"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s&includePrePost=false" % (
            host, urllib.parse.quote(ysym), interval, rng)
        try:
            doc = json.loads(_get(url))
            break
        except (urllib.error.URLError, ValueError) as e:
            last = e
            time.sleep(3)
    else:
        raise DataError("%s: Yahoo failed (%s)" % (sym, last))
    res = (doc.get("chart") or {}).get("result")
    if not res:
        err = ((doc.get("chart") or {}).get("error") or {}).get("description", "no data")
        raise DataError("%s: %s" % (sym, err))
    r = res[0]
    q = r["indicators"]["quote"][0]
    days = {}
    meta_now = r.get("meta", {}).get("regularMarketTime") or time.time()
    for i, ts in enumerate(r.get("timestamp") or []):
        if ts + step * 60 > meta_now:          # the bar still forming
            continue
        local = datetime.datetime.fromtimestamp(ts, UTC).astimezone(NY)
        _add(days, local, q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i], step)
    out = _finish(days)
    _save_cache(path, out)
    time.sleep(pause)
    return out


# ------------------------------------------------------------------ Alpaca

def alpaca_keys():
    k = os.environ.get("APCA_API_KEY_ID")
    s = os.environ.get("APCA_API_SECRET_KEY")
    if not (k and s):
        raise DataError("Alpaca needs APCA_API_KEY_ID and APCA_API_SECRET_KEY in the environment "
                        "(paper-account keys are enough for market data)")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def alpaca_bars(sym, start, end, interval="5m", feed="sip", cache="bt_cache", refresh=False,
                adjustment="all"):
    """Alpaca v2 historical stock bars, regular session, split/dividend adjusted.
    start/end: 'YYYY-MM-DD'. interval '1m' or '5m'. feed 'sip' (all US venues; the
    free plan allows it for data older than 15 minutes) or 'iex'."""
    path = _cache_path(cache, "alpaca-" + feed, sym, interval, "-%s-%s" % (start, end))
    if not refresh:
        hit = _load_cache(path)
        if hit is not None:
            return hit
    headers = alpaca_keys()
    step = int(interval.rstrip("m"))
    tf = "%dMin" % step
    days, token = {}, None
    while True:
        qs = {"timeframe": tf, "start": start + "T00:00:00Z", "end": end + "T23:59:59Z", "limit": 10000,
              "adjustment": adjustment, "feed": feed}
        if token:
            qs["page_token"] = token
        url = "https://data.alpaca.markets/v2/stocks/%s/bars?%s" % (urllib.parse.quote(sym), urllib.parse.urlencode(qs))
        for attempt in range(4):
            try:
                doc = json.loads(_get(url, headers))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                body = e.read().decode("utf-8", "replace")[:200]
                raise DataError("%s: Alpaca HTTP %s %s" % (sym, e.code, body))
        for b in doc.get("bars") or []:
            utc = datetime.datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            _add(days, utc.astimezone(NY), b["o"], b["h"], b["l"], b["c"], b["v"], step)
        token = doc.get("next_page_token")
        if not token:
            break
    out = _finish(days)
    _save_cache(path, out)
    return out


def resample(day_bars, step):
    """1-minute (or any finer) bars of one session -> step-minute bars on the 9:30 grid."""
    out = {}
    for b in day_bars:
        k = b["slot"] // step * step
        cur = out.get(k)
        if cur is None:
            mins = OPEN_MIN + k
            out[k] = dict(b, slot=k, t="%02d:%02d" % (mins // 60, mins % 60))
        else:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
            cur["v"] += b["v"]
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------------ extended hours (futures, pre-market)

def yahoo_bars_ext(sym, interval="5m", rng="60d", cache="bt_cache", refresh=False):
    """Every bar Yahoo has, pre-market and overnight included, as one continuous list
    oldest first: {"d": "YYYY-MM-DD" (New York date), "t": "HH:MM", "o","h","l","c","v"}.
    For ES=F this is the near-continuous futures session; for SPY the pre/post market."""
    path = _cache_path(cache, "yahooext", sym, interval, "-" + rng)
    if not refresh:
        hit = _load_cache(path)
        if hit is not None:
            return hit
    step = int(interval.rstrip("m"))
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s&includePrePost=true" % (
        urllib.parse.quote(sym), interval, rng)
    doc = json.loads(_get(url))
    res = (doc.get("chart") or {}).get("result")
    if not res:
        raise DataError("%s: no data" % sym)
    r = res[0]
    q = r["indicators"]["quote"][0]
    now = r.get("meta", {}).get("regularMarketTime") or time.time()
    seen, out = set(), []
    for i, ts in enumerate(r.get("timestamp") or []):
        if ts % (step * 60) or ts + step * 60 > now or ts in seen:
            continue
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        seen.add(ts)
        loc = datetime.datetime.fromtimestamp(ts, UTC).astimezone(NY)
        out.append({"d": loc.date().isoformat(), "t": loc.strftime("%H:%M"), "o": float(o), "h": float(h),
                    "l": float(l), "c": float(c), "v": int(q["volume"][i] or 0)})
    out.sort(key=lambda b: (b["d"], b["t"]))
    _save_cache(path, out)
    return out


def alpaca_bars_ext(sym, start, end, feed="sip", cache="bt_cache", refresh=False):
    """Alpaca 5-minute bars incl. pre-market (04:00-20:00 ET), continuous list as yahoo_bars_ext."""
    path = _cache_path(cache, "alpacaext-" + feed, sym, "5m", "-%s-%s" % (start, end))
    if not refresh:
        hit = _load_cache(path)
        if hit is not None:
            return hit
    headers = alpaca_keys()
    out, token = [], None
    while True:
        qs = {"timeframe": "5Min", "start": start + "T00:00:00Z", "end": end + "T23:59:59Z", "limit": 10000,
              "adjustment": "all", "feed": feed}
        if token:
            qs["page_token"] = token
        url = "https://data.alpaca.markets/v2/stocks/%s/bars?%s" % (urllib.parse.quote(sym), urllib.parse.urlencode(qs))
        for attempt in range(4):
            try:
                doc = json.loads(_get(url, headers))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise DataError("%s: Alpaca HTTP %s %s" % (sym, e.code, e.read().decode("utf-8", "replace")[:200]))
        for b in doc.get("bars") or []:
            utc = datetime.datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            loc = utc.astimezone(NY)
            out.append({"d": loc.date().isoformat(), "t": loc.strftime("%H:%M"), "o": b["o"], "h": b["h"],
                        "l": b["l"], "c": b["c"], "v": b["v"]})
        token = doc.get("next_page_token")
        if not token:
            break
    _save_cache(path, out)
    return out
