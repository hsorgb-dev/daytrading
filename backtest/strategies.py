"""Strategies as pure rules on one session's bars. Each returns a Signal or None.

skill_orb: the rule the trader-entry / trader-exit skills apply, unchanged:
  - opening range = the first `or_minutes` (15) of the session
  - trigger = the first completed bar after the range that CLOSES above the
    range high (long only), at or before no_entries_after (15:30)
  - optional volume check: trigger bar volume >= x times the range bars' average
  - stop = range midpoint, rounded down to the cent (or the range low)
  - target = 2R, flat by 15:55
"""

import random

from .engine import Signal, cents_away

OPEN_MIN = 9 * 60 + 30


def session_ok(bars, step):
    """A usable session: the opening bars are there and no more than 2 bars missing."""
    if not bars or bars[0]["slot"] != 0:
        return False
    end = bars[-1]["slot"] + step
    expected = end // step
    return len(bars) >= expected - 2 and end >= 3 * 60  # at least a 1:00 pm early close


def opening_range(bars, or_minutes):
    rng = [b for b in bars if b["slot"] < or_minutes]
    return max(b["h"] for b in rng), min(b["l"] for b in rng), rng


def skill_orb(bars, step, or_minutes=15, stop_rule="or_mid", target_r=2.0, no_entries_after="15:30",
              min_volume_x=None, side="long"):
    if not session_ok(bars, step):
        return None
    oh, ol, rng = opening_range(bars, or_minutes)
    if len(rng) < or_minutes // step:
        return None
    hh, mm = map(int, no_entries_after.split(":"))
    cutoff = min(hh * 60 + mm, OPEN_MIN + bars[-1]["slot"] + step - 30)
    avg_v = sum(b["v"] for b in rng) / len(rng)
    long = side == "long"
    for i, b in enumerate(bars):
        if b["slot"] < or_minutes:
            continue
        if OPEN_MIN + b["slot"] + step > cutoff:      # bar closes after the cutoff
            return None
        fired = b["c"] > oh if long else b["c"] < ol
        if not fired:
            continue
        if min_volume_x and avg_v > 0 and b["v"] < min_volume_x * avg_v:
            continue     # the skill moves on to the next close above the range
        if stop_rule == "or_mid":
            stop = cents_away((oh + ol) / 2, long)
        else:
            stop = cents_away(ol if long else oh, long)
        return Signal(i, side, stop, target_r=target_r,
                      meta={"or_high": oh, "or_low": ol, "or_pct": round(100 * (oh - ol) / ol, 3)})
    return None


def placebo(bars, step, real, rng, window=None):
    """A random-time long on the same session with the same stop distance (in %)
    and the same R target as a real signal: does the breakout timing add anything
    over just being long that day? window: (first_idx, last_idx) to draw from."""
    lo, hi = window or (real.idx, real.idx)
    i = rng.randint(lo, hi)
    ref = bars[i]["c"]
    frac = real.meta["risk_frac"]
    stop = cents_away(ref * (1 - frac), True)
    return Signal(i, "long", stop, target_r=real.target_r, meta={"placebo": True})
