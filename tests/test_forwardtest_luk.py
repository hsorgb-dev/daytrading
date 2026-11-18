import datetime
import unittest

from _load import load

luk = load("forwardtest", "luk")

COSTS = {"slippage_pct": 0.05, "commission_per_share": 0.005, "commission_min": 1.0}
D0 = datetime.date(2025, 1, 1)


def series(n=200, spike=None):
    """Steady uptrend (+1 %/day, 6 % daily range, 2M shares); optional spike high at index `spike`."""
    rows, c = [], 20.0
    for i in range(n):
        c *= 1.01
        h = c * 1.03 * (1.3 if i == spike else 1.0)
        rows.append((D0 + datetime.timedelta(days=i), c * 0.995, h, c * 0.97, c, 2e6))
    return rows


def with_pullback(rows):
    f = luk.features(rows)
    lvl = f["ema9"][-1]
    lo = lvl * 0.995
    c = lo * 1.025
    rows = rows + [(rows[-1][0] + datetime.timedelta(days=1), lvl * 1.001, c * 1.004, lo, c, 3e6)]
    return rows, luk.features(rows)


class Signal(unittest.TestCase):
    def test_pullback_reclaim_is_a_signal(self):
        rows, f = with_pullback(series(spike=190))
        P = luk.params({})
        self.assertTrue(luk.leader(rows, f, len(rows) - 2, P))
        s = luk.signal(rows, f, len(rows) - 1, P)
        self.assertIsNotNone(s)
        self.assertAlmostEqual(s["stop"], rows[-1][3])
        self.assertAlmostEqual(s["entry"], rows[-1][4])

    def test_red_day_is_no_signal(self):
        rows, f = with_pullback(series(spike=190))
        d, o, h, lo, c, v = rows[-1]
        rows[-1] = (d, c * 1.01, h * 1.02, lo, c, v)
        self.assertIsNone(luk.signal(rows, luk.features(rows), len(rows) - 1, luk.params({})))

    def test_stop_wider_than_half_adr_is_skipped(self):
        rows, f = with_pullback(series(spike=190))
        d, o, h, lo, c, v = rows[-1]
        rows[-1] = (d, o, c * 1.05, lo, lo * 1.045, v)  # 4.5 % > 1/2 ADR (3 %)
        self.assertIsNone(luk.signal(rows, luk.features(rows), len(rows) - 1, luk.params({})))


class Step(unittest.TestCase):
    P = luk.params({})

    def sig(self, entry=100.0, stop=97.0):
        return {"symbol": "XYZ", "entry": entry, "stop": stop, "risk": entry - stop, "level": 98.0, "adr": 0.06,
                "ret63": 0.5, "advd": 5e7}

    def open_pos(self, cap=10000.0):
        st = luk.new_state(cap)
        luk.step(st, "d0", {}, [self.sig()], True, {}, self.P, COSTS)
        return st

    def test_sizing_risk_and_position_cap(self):
        st = self.open_pos()
        p = st["positions"]["XYZ"]
        # 0.5 % of 10,000 / 3 $ = 16 shares; 30 % cap = 30 shares
        self.assertEqual(p["shares0"], 16)

    def test_market_filter_blocks_entries(self):
        st = luk.new_state(10000.0)
        _, entries, skipped, _ = luk.step(st, "d0", {}, [self.sig()], False, {}, self.P, COSTS)
        self.assertEqual(entries, [])
        self.assertIn("Marktfilter", skipped[0]["why"])

    def test_gap_through_stop_fills_at_open(self):
        st = self.open_pos()
        closed, _, _, _ = luk.step(st, "d1", {"XYZ": (95.0, 96.0, 94.0, 95.5)}, [], True, {"XYZ": 99.0}, self.P, COSTS)
        self.assertEqual(closed[0]["reason"], "stop_gap")
        self.assertAlmostEqual(closed[0]["exit_price"], 95.0)
        self.assertEqual(st["positions"], {})

    def test_stop_before_partial_on_the_same_day(self):
        st = self.open_pos()
        closed, _, _, _ = luk.step(st, "d1", {"XYZ": (100.0, 110.0, 96.0, 105.0)}, [], True, {}, self.P, COSTS)
        self.assertEqual(closed[0]["reason"], "stop")
        self.assertEqual(closed[0]["partial"], [])

    def test_partial_then_ema_exit_next_open(self):
        st = self.open_pos()
        # +3R = 109: partial 15 % of 16 = 2 shares; close stays above the EMA
        luk.step(st, "d1", {"XYZ": (101.0, 110.0, 100.0, 108.0)}, [], True, {"XYZ": 104.0}, self.P, COSTS)
        p = st["positions"]["XYZ"]
        self.assertEqual(p["shares"], 14)
        self.assertTrue(p["partialed"])
        # close below the 9 EMA: exit is pending for the next open
        luk.step(st, "d2", {"XYZ": (107.0, 107.5, 103.0, 103.5)}, [], True, {"XYZ": 104.5}, self.P, COSTS)
        self.assertEqual(st["positions"]["XYZ"]["pending"], "ema9")
        closed, _, _, _ = luk.step(st, "d3", {"XYZ": (104.0, 105.0, 102.0, 104.0)}, [], True, {}, self.P, COSTS)
        t = closed[0]
        self.assertEqual(t["reason"], "ema9")
        self.assertEqual(t["shares"], 16)
        self.assertAlmostEqual(t["gross"], 2 * 9.0 + 14 * 4.0)
        self.assertLess(t["net"], t["gross"])

    def test_max_hold(self):
        st = self.open_pos()
        for k in range(1, 15):
            luk.step(st, "d%d" % k, {"XYZ": (101.0, 102.0, 100.5, 101.5)}, [], True, {"XYZ": 100.0}, self.P, COSTS)
        self.assertEqual(st["positions"]["XYZ"]["pending"], "max_hold")

    def test_close_all(self):
        st = self.open_pos()
        out = luk.close_all(st, "d1", {"XYZ": 102.0}, COSTS)
        self.assertEqual(out[0]["reason"], "segment_end")
        self.assertEqual(st["positions"], {})
        self.assertAlmostEqual(luk.nav(st), 10000.0 + out[0]["net"], places=6)


if __name__ == "__main__":
    unittest.main()
