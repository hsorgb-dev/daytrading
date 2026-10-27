---
name: trader-timemachine
description: >-
  Time Machine. Replays a planned day trade before you risk a dollar: 10,000
  simulated sessions plus every past time the setup fired, with honest
  uncertainty. Takes the plan from trader-exit (entry, stop, target), rebuilds
  the rest of the day 10,000 times from runs of real 5-minute bars of past
  days that had the same setup, takes costs and gaps through the stop into
  account, and re-runs the same rule on every past session. Prints the odds of
  target first, stop first, or neither by the close, the average net R, the
  history with 95 % intervals, and one honest verdict, then hands off to
  /trader-budget. Use when the user says "run the time machine", "replay this
  trade", "what are the odds", "simulate this trade", "monte carlo",
  "backtest this setup", "how often does this hit the target", "is this a
  coin flip", or after /trader-exit writes a plan, before /trader-budget.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the past days that looked
like today, how often does this plan hit the target first, the stop first,
or neither, and how sure can anyone be about that? It answers with 10,000
replays, with the real record of the same rule on every past session, and
with 95 % intervals that show how little 60 days can prove. It is a
simulation built from the stock's own recent bars, not a prediction. Day
trading loses most people money, and a good-looking replay does not change
that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:55      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --cost 0.03        # your real round-trip cost per share
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol TSLA --date 2026-10-19 \
    --entry 364.18 --stop 358.32 --target 370.04 --entry-time "10:15 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --classic          # the original Time Machine, for comparison
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--method block|iid` (default block), `--block-bars N` (average run
length, default 6 = 30 minutes), `--pool setup|all` (default setup),
`--cost X` (round-trip cost per share, default 0.02), `--no-gap-fill`,
`--classic` (= `--method iid --pool all --cost 0 --no-gap-fill`, no
intervals: the original numbers bit for bit), `--offline FILE` (a saved
Yahoo chart JSON, `.json` or `.json.gz`), `--now "2026-10-19 10:15"` (New
York time) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Yahoo chart API | one request: 60 days of 5-minute bars (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history, verdict, verdict_notes, evidence}`. `mc` holds the odds, the net and gross average R, `ci95` (the day-level ranges), percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close. `history` holds every past trade with `R` and `R_net`, and `ci95` |

## How it works

1. **The data.** It keeps only FULL sessions before the plan's date: all 78
   five-minute bars, 9:30 to 4:00 New York time. Half days, gaps and today
   are left out. Every bar becomes four ratios to the bar before it:
   open, high, low and close over the previous close.
2. **The pool: days like today.** With an opening-range-breakout plan, only
   the past sessions where the same rule had fired by the plan's entry bar
   go into the pool (`--pool setup`). Today is a breakout day, so the replays
   are built from breakout days, not from quiet ones. With fewer than 15 such
   days it falls back to all sessions and says so.
3. **One replay: runs of real bars.** Start at the entry price. Take a run of
   consecutive bars from ONE past session, at the same time of day (plus or
   minus 2 bars), scaled to the current price. Each bar, a new run from
   another random day starts with probability 1 / 6, so runs last about 30
   minutes (`--block-bars`). This is a stationary block bootstrap. Inside a
   run the real order of the bars is kept, so a breakout's follow-through,
   or its failure, survives. The original Time Machine (`--method iid`) drew
   every bar on its own; that turns every replay into a random walk with the
   stock's time-of-day volatility and loses exactly the momentum an
   opening-range breakout lives on.
4. **Bar by bar** it checks the stop first, then the target. A bar that
   touches both counts as the stop. A bar that OPENS beyond the stop fills at
   its open, not at the stop: a gap through a stop order (`--no-gap-fill`
   turns that off). A bar that opens beyond the target fills at the target.
5. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 4:00 PM, the
   close, or 1:00 PM on an NYSE early-close day). Green, which means target
   first or above the entry at `--until`. R is the target's R at the target,
   the fill's distance at the stop (-1R unless it gapped), and otherwise the
   distance from the entry at `--until`, all divided by the risk. The round-
   trip cost (`--cost`, default 0.02 a share for fees, spread and slippage)
   comes off every R: that is the net R.
6. **The noise of the days.** 10,000 replays of 24 days still hold only 24
   days of information. So it resamples the pool's DAYS 200 times (with
   replacement), runs a small simulation on each set, and reports the 95 %
   range of the target's share and of the average net R. That range, not the
   10,000, is how sure the simulation can be.
7. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every past session, with the same costs and
   gap fills. That is what really happened, not a simulation. It reports a
   Wilson 95 % interval for the target's share of the trades that hit one
   side, and a bootstrap 95 % interval for the average net R.
8. **The verdict** is one line from the simulation. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even, costs need it to
   win more: (1 + cost in R) in (1 + target R). That is 50 % at 1R without
   costs, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even: "Close
   to a coin flip" at 1R without costs, "Close to break-even" otherwise. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. But when the day-level range from
   step 6 still contains break-even, the verdict is "No clear lean, within
   the noise of N days", however far the point estimate leans.
9. **Below the verdict**: the day-level range, a line when more than 40 % of
   the replays hit neither, the average R when its sign goes against the
   lean, and the **evidence line** from the history: a positive edge, a
   negative edge, or no edge that can be told apart from zero (its 95 %
   interval of the average net R contains zero). Fewer than 20 past trades:
   too few to tell an edge from luck.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:55` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
   Use the user's real costs with `--cost` when they know them.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the close, the average net R, the history line and
   its intervals, then the verdict line and every line under it exactly as
   printed. Say the seed.
4. Lead with the evidence line and the day-level range when they say the
   data cannot tell an edge from zero. That is the most important thing the
   user can learn here. Never present 10,000 replays as 10,000 pieces of
   evidence. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Earnings, news and halts can do things those 60 days never did. Check
   `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: the reel

TSLA, Monday 2026-10-19. The 15-minute opening range is 354.47 to 362.18.
The 10:10 bar closes above it: entry 364.18 at 10:15 AM (bar 8), stop 358.32
(the range midpoint, rounded down), target 370.04 (1R, risk 5.86 a share).

```
10,000 replays of the rest of the session, 10:15 AM to 4:00 PM (69 five-minute bars), seed 19.
Bars are drawn in runs of about 6 consecutive bars (30 min) ... out of the 24 of its last 59
full sessions where the setup had fired by 10:15 AM.
  target first   29.1 %
  stop first     22.3 %
  neither        48.5 %   still open at 4:00 PM
  Average R: +0.062
History: the same rule on each of those 59 real sessions fired 33 times:
13 target, 15 stop, 5 neither. Average R -0.062.
  after costs -0.069; 95 % interval of the average -0.38 to +0.25; target share 30 % to 64 %.
No clear lean, within the noise of 24 days: at 1R after costs the target has to come first
in 50 % of the replays that hit one side; it did in 57 % ...
Resampling the 24 past days moves the target's share between 34 % and 76 % (break-even 50 %)
and the average net R between -0.19 and +0.34.
History: no edge that can be told apart from zero (average net R -0.069, 95 % interval -0.38
to +0.25, 33 trades).
```

The point estimate of the replays leans toward the target, the real history
leans slightly the other way, and both ranges sit squarely across zero. 60
days of TSLA cannot tell this plan from a coin flip. The original Time
Machine (`--classic`) said "Close to a coin flip" too, but for the wrong
reason: its replays were random walks by construction.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The 95 % ranges come from the days, not from the 10,000 replays. Never
  quote the replay odds as if they were measured to a tenth of a percent.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R and the costs.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
