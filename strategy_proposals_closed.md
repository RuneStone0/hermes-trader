"""The weekly review's settled findings — questions that are ANSWERED.

Purpose: the nightly self-improve routine read `lessons_learned.md` and
`strategy_proposals.md` and re-derived the same handful of proposals from them
every night for two weeks (the same ATR-stop, sector-relative-strength and
"config drift" items, re-filed unchanged, several of them not even testable as
written). This file is handed to that routine as `settled_findings` with the
instruction not to re-propose anything here without naming the new measurement
that overturns the recorded verdict.

Rule for adding an entry: a verdict may only be added or changed by a
MEASUREMENT, and the measurement must be named. "We think this is fixed" is not
a verdict. Entries carry: the question, the verdict, and the evidence.

Newest first.

---

## 1. "YOLO enters with noise-tight stops — scale stops to ATR / add a low-vol guard"
**VERDICT: FIXED IN CODE 2026-09-17. Do not re-propose.** Measured and closed
2026-09-28.

Evidence — stop distance in ATR per trade, measured from live 5-min bars
(`reports/r_distribution_2026-09-28.md`):
- trades opened 2026-09-08..09-10: 0.143, 0.243, 0.312, 0.385, 0.542, 0.802 ATR
- trades opened 2026-09-14..09-24: 1.107, 1.28, 1.26, 1.576, 1.365, 1.492, 1.523,
  2.07, 2.743, 2.866, 2.989 ATR

The four sub-0.6-ATR stops are exactly the trades whose MFE exceeded +1R and
which still closed red (GLD MFE +1.087R -> realised -1.07R; XLU MFE +2.052R ->
realised -1.04R). Every trade opened since 2026-09-14 is at 1.1-3.0 ATR. The
2026-09-17 fix that validates bracket geometry against the LIVE price with a
minimum distance is visible in the data and working.

## 2. "Gate long entries on own-instrument trend + non-negative 5d RS vs SPY (when breadth is narrow)"
**VERDICT: TESTED 2026-09-28 — NOT SUPPORTED. Do not re-propose without new data.**

Evidence — `backtest_mr.py` §12, variant `CTRL_trend_rs`, same universe (19 ETFs),
same 2.5x ATR stop, same 3-bar hold, same fee/slippage model, fresh bars fetched
2026-09-28 (window to 2026-09-25):

| variant | n | win% | avgR | totR | maxDD_R |
|---|---|---|---|---|---|
| CTRL_long_beta (no gate) | 2445 | 56.1% | +0.036 | +87.82 | 31.1 |
| CTRL_trend_rs (+gate) | 1016 | 56.5% | +0.038 | +38.54 | 24.4 |

Difference +0.002R per trade, Welch t = +0.10, 95% CI [-0.035, +0.040] — straddles
zero. The gate removes 58% of the trades to buy the same expectancy per trade
(it does cut max drawdown 31.1 -> 24.4 R and halves time in market; that is an
exposure choice, not an edge). Unconditional version tested, NOT the
narrow-breadth-conditional version — a conditional test needs a breadth series
and has not been run. If you re-open this, state that you are testing the
CONDITIONAL version and produce its numbers.

## 3. "Config drift: notes say YOLO sits at the floors (0.05 / 0.01 / 1) but live knobs read 0.25 / 0.02 / 4.0"
**VERDICT: NOT DRIFT — a misreading of the tuning bounds as a live state. Closed
2026-09-28.**

`config.YOLO_TUNE` bounds are `max_position_pct (0.05, 0.9)`,
`max_risk_pct (0.01, 0.1)`, `max_concurrent_positions (1, 10)`. Those are the
tuner's MINIMUM/MAXIMUM *allowed* values, not a floor the bot sits at. The live
knobs (0.25 / 0.02 / 4.0) sit at no bound, and no code reads them as "at the
floors". Nothing to reconcile; the phrase "we are already at the floors" was never
true and must not be used as an argument. Cite `config.YOLO_TUNE`, never the
lessons tail, when discussing sizing bounds.

## 4. "No entries into HIGH-impact event windows"
**VERDICT: ALREADY IMPLEMENTED — verify, do not propose.** `config.EVENT_GUARD`
is enabled with `blackout_min 30`, `blackout_importance 1`,
`event_day_importance 1`, `event_day_size_cut 0.5`,
`block_overnight_into_fomc True`, and `guards.entry_gate()` /
`guards.size_factor()` / `guards.fomc_block_new_overnight()` are wired into
`daily_run.py`, `mr_run.py` and `weekly_run.py` on every entry path. Observed
working live: 2026-09-23 the YOLO entry was cut to 50% size and journaled as
"50% on an event day". A proposal to add this is a duplicate; if the guard is
judged insufficient, the proposal must name the specific event it failed to
block.

## 5. "Hold all parameter changes until the 30-closed-trade gate clears"
**VERDICT: ALREADY ENFORCED mechanically.** `config.MIN_CLOSED_TRADES_TO_TUNE = 30`
and the tuner's own gate (`self_improve._tuning_gate`) refuses to apply a change
below it. This is process, not a proposal; re-proposing it changes nothing.

## 6. "Fee/cost drag is a source of the loss"
**VERDICT: DISPROVED.** Fees measured across all three books: $0.26 DAILY + $0.10
MR + $0.41 YOLO against a YOLO net of -$206.28 and -9.09R. Cost drag explains
none of the result. Do not spend a cycle on cost.

## 7. "The -1.74R tail is an execution defect (fills beyond the stop)"
**VERDICT: MEASURED 2026-09-28 — a gap through a noise-tight stop, not an
execution defect.** The trade in question (SLV, 24 sh, entry 61.0000, stop
59.47012, exit 58.3392, opened 2026-09-09 16:33, closed 2026-09-10 13:34) shows
MFE +0.451R / MAE -1.902R: it was carried overnight and the next session opened
through the stop, so it filled ~1.1 below a stop that was only 0.802 ATR wide.
The resting stop did what a resting stop can do. Any future execution proposal
must first show that the fill was worse than the first trade PRINTED beyond the
stop, which this trade does not.

---

Settled findings are also mirrored at
`/data/trading/strategy_proposals_closed.md` inside the `hermes-trader`
container — that volume copy is the one the nightly routine reads.
