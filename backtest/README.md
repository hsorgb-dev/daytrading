# backtest

Research tooling for this fork: historical tests of the skill's opening-range
breakout and of the S&P 500 Open Playbook v3.4. Plain Python, no packages.
Results are in R (multiples of the initial risk), after costs.

```bash
# the skill's ORB rule (trader-entry + trader-exit) over 99 liquid US stocks
python3 -m backtest.run skill_orb --source yahoo                     # last ~60 days, 5-minute bars
python3 -m backtest.run skill_orb --source alpaca --start 2021-01-04 --end 2026-10-27 --oos-from 2024-01-01

# Open Playbook v3.4, S&P part
python3 -m backtest.open_playbook --symbol ES=F                      # Yahoo, ~60 days of ES futures
python3 -m backtest.open_playbook --symbol SPY --source alpaca --start 2021-01-04 --end 2026-10-27 --oos-from 2024-01-01
```

Alpaca needs `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in the environment
(paper-account keys are enough for market data). Never commit them.

## What the engine assumes

- **Fills.** Skill ORB: the open of the bar after the signal (the skill's own
  replay fills at the signal close; both are reported). Open Playbook: the
  close of the entry bar, as the playbook says.
- **Stop first** when a bar touches both the stop and a target. A bar that
  opens through the stop fills at its open. Targets never fill better than
  the target.
- **Costs.** Stocks: $0.02 a share round trip (stress: $0.04 + 2 bp).
  ES: 0.5 points round trip (stress: 1.0). SPY gets the ES point rules scaled
  by 0.1 (SPY ≈ S&P 500 / 10).
- **Intervals** resample trading days, not trades: breakouts on the same
  morning win or lose together.
- **Benchmark** (skill ORB): the same stock, the same time of day, the same
  stop distance and target, on random other days without the breakout. A rule
  with an edge has to beat it.

## Known limits

- Yahoo serves ~60 days of 5-minute bars. That is a smoke test, not a verdict.
- The stock universe is a fixed list of liquid large caps (`universe.py`), not
  the scanner's whole market: delisted small caps would need point-in-time
  listings.
- Open Playbook v3.4 leaves the "Abstands- und Risikofilter" beyond the 4-point
  rule and the rejection "Qualitätsfilter" unspecified; they are variants here
  (`max_risk`, `rej_min_pen`), off in the base case.
- ES=F on Yahoo is the rolling front month; the EMA20 sees the roll jump once
  a quarter.

## Parameter search (research/)

Needs numpy, pandas and pyarrow. Runs on the cached Alpaca data, searches on
2021–2023 only and scores every config on 2024–2026 for the record.

```bash
python3 -m backtest.research.orb_grid --cache bt_cache --out bt_results/orb_grid.parquet   # ~1 min
python3 -m backtest.research.orb_search --grid bt_results/orb_grid.parquet --out bt_results/orb_search.csv
python3 -m backtest.research.op_search --cache bt_cache --out bt_results/op_search.csv     # ~10 min
```

`orb_grid` reproduces the engine's base ORB to the cent except for half-cent
rounding of split/dividend-adjusted prices (about 1 % of trades differ by a
cent at entry). `op_search` reproduces the playbook base run exactly.

## Darvas box (research/darvas.py)

Daily bars built from the same 5-minute cache. Rules and the 24 variants were
committed before the first run (see the module docstring); search years
2022-2023, test 2024-2026 once, benchmarks: equal-weight buy and hold of the
same 99 stocks, SPY, and random entries with the same stop distance and holding
period.

```bash
python3 -m backtest.research.darvas --cache bt_cache --out bt_results/darvas   # ~30 s
```
