# Strategy Proposals — REVIEW QUEUE

Proposed changes that affect trading decisions. The nightly self-improvement
routine writes here; it does NOT apply these. A human reviews and approves.

Each proposal: what / why / evidence / expected impact.

---

## 2026-09-21 — PENDING: YOLO exit management (take profit / trail) is unevidenced, so NOT deployed

**What:** Consider exiting a YOLO position at a partial profit (e.g. +1R) or
trailing the stop, instead of always riding the fixed 2R target.

**Why:** Every YOLO loss so far exited at ~-1.0R while the *planned* reward was
+2.0R, so the question is whether the entries ever went anywhere before they
failed. Measured on 5-min bars from the actual entry moment (probe 2026-09-21,
one row per closed trade — MFE = best favourable excursion, MAE = worst
adverse, both in units of the trade's own entry→stop distance):

| trade | stop distance | net R | MFE | MAE |
|---|---|---|---|---|
| GLD 2026-09-08 long | 0.26% of px | -1.07 | **+1.13R** | +1.35R |
| XLB 2026-09-08 long | 0.56% | -1.07 | -0.28R | +2.07R |
| XLU 2026-09-08 short | 0.40% | -1.04 | **+2.05R** | -0.80R |
| SMH 2026-09-09 long | 2.52% | -0.99 | +0.12R | +1.01R |
| SLV 2026-09-09 long | 2.51% | -1.74 | +0.46R | +2.13R |
| XLP 2026-09-09 short | 2.01% | -0.99 | +0.48R | +0.73R |
| XLE 2026-09-15 long | 2.51% | -1.00 | -0.09R | +1.28R |
| META 2026-09-17 long | 4.01% | -0.05 | +0.73R | +0.23R |

So the earlier statement "no YOLO trade ever reached +1R" is **wrong on clean
data**: 2 of 8 did (the two whose stops sat 0.26%/0.40% from entry — i.e. inside
the noise, where MFE and MAE are both >1R and the trade is a coin flip). Among
the 5 trades with a sane stop (2.0-4.0% of price), max MFE was +0.73R and none
reached +1R.

**Evidence:** n=8 closed trades, no backtest possible (YOLO entries are LLM
decisions, so there is nothing deterministic to replay — see
`reports/backtest_2026-09.md` §4.3). n=8 is far below
`config.MIN_CLOSED_TRADES_TO_TUNE` (30) and the split above (2/8 vs 0/5) is not
a finding at this sample size.

**Verdict / expected impact:** NOT deployed — a rule whose exit is judged on 8
observations where the "winners" are the two noise-stop coin flips is exactly
the kind of change that looks like improvement and is not. Kept here so the next
review can re-measure at 30+ closed trades, when the question becomes
answerable. **Watch instead:** whether the trades with a sane stop distance
(≥1×ATR(14)) ever reach +1R.

<!-- proposals below -->
