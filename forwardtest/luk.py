"""Variante L: Pullback-Swing nach Martin Luk, mechanisch (Tageskerzen, Haltedauer ueber Nacht).

Regeln uebernommen aus rootchunhe/martin-luk-backtesting (specs.py, BASE, Stand 11/2026), dort auf
CRSP/TAQ getestet. Hier nur mit Yahoo-Tageskerzen umgesetzt; Abweichungen stehen bei den Parametern.

  1. Leader (alles auf dem Schluss des Vortags D-1): Schluss >= 5 $, 20-Tage-Dollar-Volumen >= min_dollar_volume,
     ADR(20) = Mittel((Hoch - Tief) / Schluss) >= 5 %, Rendite 21 oder 63 Tage >= 30 %, EMA 9 > 21 > 50,
     Schluss ueber dem VWAP ab dem hoechsten Hoch der letzten 250 Tage (AVWAP).
  2. Pullback am Tag D: Tief <= EMA9(D-1), aber nicht tiefer als 1 ADR darunter.
  3. Rueckeroberung: Schluss > Open und > EMA9(D-1), Schluss im oberen 60 % der Tagesspanne.
     Einstieg zum Schlusskurs (Ersatz fuer den 15:55-Open im Referenz-Repo).
  4. Stop = Tagestief; Abstand 2-5 % und hoechstens 1/2 ADR, sonst kein Trade.
  5. Groesse: risk_pct vom Konto je Trade (Risiko = Schluss - Tief), hoechstens max_position_pct je Position,
     Brutto hoechstens max_gross x Konto, hoechstens max_positions Positionen, Mindestorder. Bei mehr Signalen
     als Plaetzen zuerst die staerksten 63-Tage-Renditen. Neue Longs nur, wenn bei QQQ EMA9 > EMA21 (D-1).
  6. Ausstieg: 15 % bei +3R (Limit, Fill max(Open, Ziel)); Rest zum naechsten Open nach dem ersten Tagesschluss
     unter der EMA9, spaetestens nach 15 Handelstagen (ebenfalls naechster Open). Stop: Gap durch den Stop fuellt
     zum Open; Stop und 3R am selben Tag: Stop zuerst (Reihenfolge mit Tageskerzen unbekannt).

Nur Standardbibliothek. Der Zustand (offene Positionen, Cash) ist ein JSON-faehiges dict.
"""

import math

DEFAULTS = dict(
    min_price=5.0, min_dollar_volume=20e6, min_adr=0.05, min_run_up=0.30, max_run_up=20.0,
    touch_tolerance_adr=0.0, max_undercut_adr=1.0, min_close_position=0.40,
    min_stop_width=0.02, max_stop_width=0.05, max_stop_width_adr=0.5,
    risk_pct=0.5, max_position_pct=30.0, max_gross=1.0, max_positions=5, min_order_usd=500.0,
    partial_at_r=3.0, partial_fraction=0.15, exit_ema=9, max_hold_days=15, market_filter="QQQ",
)

ADR_W, ADV_W, R1, R2, ANCHOR_W = 20, 20, 21, 63, 250


def params(vcfg):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (vcfg or {}).items() if k in DEFAULTS})
    return p


# ---------------------------------------------------------------- features

def features(rows):
    """rows: sorted list of (date, o, h, l, c, v). Returns dict of lists aligned with rows (None until defined)."""
    n = len(rows)
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    c = [r[4] for r in rows]
    v = [r[5] or 0.0 for r in rows]
    f = {k: [None] * n for k in ("ema9", "ema21", "ema50", "adr", "advd", "ret21", "ret63", "avwap")}
    if not n:
        return f
    a9, a21, a50 = 2 / 10.0, 2 / 22.0, 2 / 51.0
    e9 = e21 = e50 = c[0]
    s_adr = s_adv = 0.0
    cpv = [0.0] * (n + 1)
    cv = [0.0] * (n + 1)
    anchor = 0
    for i in range(n):
        e9 = a9 * c[i] + (1 - a9) * e9
        e21 = a21 * c[i] + (1 - a21) * e21
        e50 = a50 * c[i] + (1 - a50) * e50
        if i >= 8:
            f["ema9"][i] = e9
        if i >= 20:
            f["ema21"][i] = e21
        if i >= 49:
            f["ema50"][i] = e50
        s_adr += (h[i] - lo[i]) / c[i] if c[i] else 0.0
        s_adv += c[i] * v[i]
        if i >= ADR_W:
            s_adr -= (h[i - ADR_W] - lo[i - ADR_W]) / c[i - ADR_W] if c[i - ADR_W] else 0.0
        if i >= ADV_W:
            s_adv -= c[i - ADV_W] * v[i - ADV_W]
        if i >= ADR_W - 1:
            f["adr"][i] = s_adr / ADR_W
        if i >= ADV_W - 1:
            f["advd"][i] = s_adv / ADV_W
        if i >= R1 and c[i - R1]:
            f["ret21"][i] = c[i] / c[i - R1] - 1
        if i >= R2 and c[i - R2]:
            f["ret63"][i] = c[i] / c[i - R2] - 1
        cpv[i + 1] = cpv[i] + (h[i] + lo[i] + c[i]) / 3.0 * v[i]
        cv[i + 1] = cv[i] + v[i]
        if h[i] >= h[anchor]:
            anchor = i
        elif i - anchor >= ANCHOR_W:  # highest high of the last 250 bars, the latest of ties
            best = i - ANCHOR_W + 1
            for j in range(best, i + 1):
                if h[j] >= h[best]:
                    best = j
            anchor = best
        den = cv[i + 1] - cv[anchor]
        f["avwap"][i] = (cpv[i + 1] - cpv[anchor]) / den if den > 0 else None
    return f


def leader(rows, f, p_idx, P):
    """Leader conditions on bar p_idx (the day before the signal day)."""
    if p_idx < 63:
        return False
    c = rows[p_idx][4]
    vals = [f[k][p_idx] for k in ("ema9", "ema21", "ema50", "adr", "advd", "avwap")]
    if None in vals:
        return False
    e9, e21, e50, adr, advd, avwap = vals
    r21, r63 = f["ret21"][p_idx], f["ret63"][p_idx]
    run = (r21 is not None and r21 >= P["min_run_up"]) or (r63 is not None and r63 >= P["min_run_up"])
    sane = all(r is None or r < P["max_run_up"] for r in (r21, r63))  # 2,000 % = data error, not a leader
    return (c >= P["min_price"] and advd >= P["min_dollar_volume"] and adr >= P["min_adr"] and run and sane
            and e9 > e21 > e50 and c > avwap)


def max_width(P, adr):
    return min(P["max_stop_width"], P["max_stop_width_adr"] * adr)


def signal(rows, f, i, P):
    """Pullback signal on bar i (needs leader on i-1). Returns dict or None."""
    if i < 1 or not leader(rows, f, i - 1, P):
        return None
    d, o, h, lo, c, v = rows[i]
    level, adr = f["ema9"][i - 1], f["adr"][i - 1]
    if lo > level * (1 + P["touch_tolerance_adr"] * adr):
        return None                              # did not reach the 9 EMA
    if lo < level * (1 - P["max_undercut_adr"] * adr):
        return None                              # broke down through it
    if c <= o or c <= level:
        return None                              # no reclaim: red day or close under the 9 EMA
    if h <= lo or (c - lo) / (h - lo) < P["min_close_position"]:
        return None                              # weak close
    width = c / lo - 1 if lo > 0 else 9
    if width < P["min_stop_width"] or width > max_width(P, adr):
        return None
    return {"date": d, "entry": c, "stop": lo, "risk": c - lo, "level": level, "adr": adr,
            "ret63": f["ret63"][i - 1] or 0.0, "advd": f["advd"][i - 1]}


def market_ok(qrows, qf, day):
    """QQQ EMA9 > EMA21 on the last bar before `day`."""
    idx = [k for k, r in enumerate(qrows) if r[0] < day]
    if not idx:
        return False
    k = idx[-1]
    e9, e21 = qf["ema9"][k], qf["ema21"][k]
    return e9 is not None and e21 is not None and e9 > e21


# ---------------------------------------------------------------- portfolio

def new_state(capital):
    return {"cash": float(capital), "positions": {}}


def _comm(n, costs):
    return max(costs["commission_min"], n * costs["commission_per_share"])


def nav(state):
    return state["cash"] + sum(p["shares"] * p["last_close"] for p in state["positions"].values())


def gross(state):
    return sum(p["shares"] * p["last_close"] for p in state["positions"].values())


def _close_trade(p, sym, day, px, n, reason, how, costs, slip):
    """Final exit of the remaining n shares; returns the closed trade (gross, costs, net over all legs)."""
    fill = px * (1 - slip)
    comm = _comm(n, costs)
    p["legs"].append([day, n, px, fill, comm, reason])
    g = sum(q * (x - p["entry"]) for _, q, x, _, _, _ in p["legs"])
    net = sum(q * (fx - p["entry_fill"]) for _, q, _, fx, _, _ in p["legs"]) - p["entry_comm"] - sum(
        cm for _, _, _, _, cm, _ in p["legs"])
    q_all = sum(q for _, q, _, _, _, _ in p["legs"])
    avg_px = sum(q * x for _, q, x, _, _, _ in p["legs"]) / q_all
    return {"symbol": sym, "entry_date": p["entry_date"], "exit_date": day, "entry": p["entry"], "stop": p["stop0"],
            "risk_ps": p["risk_ps"], "shares": p["shares0"], "exit_price": avg_px, "reason": reason, "how": how,
            "gross": g, "net": net, "costs": g - net, "hold_days": p["hold_days"],
            "partial": [l for l in p["legs"] if l[5] == "partial"], "level": p["level"], "advd": p["advd"],
            "r_net": net / (p["shares0"] * p["risk_ps"]) if p["risk_ps"] > 0 else 0.0}


def step(state, day, bars, sigs, mkt_ok, ema_today, P, costs, account=None):
    """One trading day. bars: {sym: (o, h, l, c)} for `day`; ema_today: {sym: exit EMA on `day`};
    sigs: list of signal dicts with "symbol". account: fixed sizing basis, None = NAV at the prior close.
    Returns (closed trades, entries, skipped, realized net)."""
    slip = costs["slippage_pct"] / 100.0
    closed, entries, skipped = [], [], []
    acct = account if account is not None else nav(state)       # prior-close marks: positions not yet updated
    pos = state["positions"]
    # 1. open positions
    for sym in sorted(pos):
        p = pos[sym]
        b = bars.get(sym)
        if b is None:
            continue                              # no bar (halt or data gap): stop and pending exit wait
        o, h, lo, c = b
        p["hold_days"] += 1
        exit_ = None
        if p["pending"]:
            exit_ = (o, p["pending"], "open")
        elif o <= p["stop"]:
            exit_ = (o, "stop_gap", "open")
        else:
            tgt = p["entry"] + P["partial_at_r"] * p["risk_ps"] if P["partial_at_r"] else None
            if lo <= p["stop"]:
                exit_ = (p["stop"], "stop", "intraday")          # stop first when the order is unknown
            elif tgt is not None and not p["partialed"] and h >= tgt:
                q = int(math.floor(P["partial_fraction"] * p["shares0"]))
                if 0 < q < p["shares"]:
                    px = max(o, tgt)                              # limit order: no slippage
                    cm = _comm(q, costs)
                    p["legs"].append([day, q, px, px, cm, "partial"])
                    state["cash"] += q * px - cm
                    p["shares"] -= q
                p["partialed"] = True
        if exit_:
            px, reason, how = exit_
            n = p["shares"]
            t = _close_trade(p, sym, day, px, n, reason, how, costs, slip)
            state["cash"] += n * px * (1 - slip) - _comm(n, costs)
            closed.append(t)
            del pos[sym]
            continue
        p["last_close"] = c
        e = ema_today.get(sym)
        if P["exit_ema"] and e is not None and c < e:
            p["pending"] = "ema%d" % P["exit_ema"]
        elif P["max_hold_days"] and p["hold_days"] >= P["max_hold_days"]:
            p["pending"] = "max_hold"
    # 2. new entries at the close
    if not mkt_ok:
        skipped += [{"symbol": s["symbol"], "why": "Marktfilter QQQ EMA9 <= EMA21"} for s in sigs]
        sigs = []
    for s in sorted(sigs, key=lambda x: (-x["ret63"], x["symbol"])):
        sym = s["symbol"]
        if sym in pos:
            skipped.append({"symbol": sym, "why": "schon im Bestand"})
            continue
        if len(pos) >= P["max_positions"]:
            skipped.append({"symbol": sym, "why": "max. Positionen"})
            continue
        e, risk = s["entry"], s["risk"]
        room = P["max_gross"] * acct - gross(state)
        n = min(int(P["risk_pct"] / 100.0 * acct // risk), int(P["max_position_pct"] / 100.0 * acct // e),
                int(max(room, 0) // e))
        if n < 1 or n * e < P["min_order_usd"]:
            skipped.append({"symbol": sym, "why": "Groesse/Brutto-Limit"})
            continue
        fill = e * (1 + slip)
        cm = _comm(n, costs)
        state["cash"] -= n * fill + cm
        pos[sym] = {"entry_date": day, "entry": e, "entry_fill": fill, "entry_comm": cm, "stop": s["stop"],
                    "stop0": s["stop"], "risk_ps": risk, "shares0": n, "shares": n, "partialed": False,
                    "pending": None, "hold_days": 1, "last_close": e, "legs": [], "level": s["level"],
                    "advd": s["advd"], "ret63": s["ret63"]}
        entries.append(dict(s, shares=n))
    return closed, entries, skipped, sum(t["net"] for t in closed)


def close_all(state, day, closes, costs):
    """Close every position at `day`'s close (end of a backfill segment)."""
    slip = costs["slippage_pct"] / 100.0
    out = []
    for sym in sorted(state["positions"]):
        p = state["positions"][sym]
        px = closes.get(sym, p["last_close"])
        n = p["shares"]
        out.append(_close_trade(p, sym, day, px, n, "segment_end", "close", costs, slip))
        state["cash"] += n * px * (1 - slip) - _comm(n, costs)
    state["positions"] = {}
    return out
