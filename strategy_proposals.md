# Strategy Proposals — REVIEW QUEUE

Proposed changes that affect trading decisions. The nightly self-improvement
routine writes here; it does NOT apply these. A human reviews and approves.

Each proposal: what / why / evidence / expected impact.

> **WHERE THE LIVE QUEUE LIVES (read this first).** The routine inside the
> container writes its proposals to `/data/trading/strategy_proposals.md` (the
> `trader_trader-data` volume) — NOT to this repo file, which is a *copy* kept
> under version control. As of 2026-09-18 the volume copy held 14 pending
> structural items while this file was still empty, i.e. the weekly review was
> being pointed at a file the routine never writes. The weekly digest now prints
> the volume copy (see `scripts/trader_week_digest.py`), and dispositions are
> recorded below so the next review starts from the decisions, not from scratch.

---

## 2026-09-21 — REVIEW DISPOSITION of the 14 pending items in the live queue

Verdicts from the weekly strategy review of 2026-09-21. Evidence cited is either
a backtest (`reports/backtest_2026-09.md`) or live data read from the container
that day. Where the verdict is "no evidence", nothing was deployed.

| # | proposal (queue date) | verdict | reason / evidence |
|---|---|---|---|
| 1 | Baseline trend-following 20-period-high breakout, 1h, SPY/QQQ/GLD/EURUSD, no take-profit (09-01) | **REJECTED** | Superseded by the deployed sleeves. Its closest measurable form (`ORB_daily_PROXY`, n=327) is avgR **-0.06** (t -0.69) and -0.15R in the out-of-sample half; the "no take-profit, trailing exit" half is the same unevidenced exit change as item 8. EURUSD is not a retail-tradable spot pair on this broker (floor: retail instruments only). |
| 2 | Minimum sample-size gate before any parameter change — 30 closed trades (09-01) | **CLOSED — DONE** | Already implemented: `config.MIN_CLOSED_TRADES_TO_TUNE = 30`, enforced in `self_improve.py` per bucket (refusals are journaled as "HELD BY SAMPLE GATE"). |
| 3 | Time-of-day / session filter (London/NY open, exclude Asia) (09-01) | **REJECTED** | Not applicable: the book is US-session-only by construction (ORB 09:30-10:00 ET, weekly 14:45 UTC, MR 15:30-15:55 ET) and every instrument is a US-listed ETF. No backtest can rank a filter on sessions that are never traded. |
| 4 | YOLO intraday circuit breaker after 3 consecutive losses in a session (09-10) | **DEFERRED — inert on current evidence** | It would have fired **0 times**: across 11 live sessions the worst YOLO day had 2 losses (Sep 8, Sep 10), never 3. |
| 5 | YOLO daily loss cap / entry pause after a fixed loss threshold (09-14) | **DEFERRED — prefer tightening the existing cap** | A daily-loss cap already exists in `RISK_GOV` (`daily_loss_cap_pct`). At YOLO's *realised* size (~0.2% of equity per trade) it needs ~10 losses in a day to bind, so it is currently inert too. If intraday damage ever needs bounding, tighten that existing cap to a reachable level rather than adding a second overlapping control — and only with evidence about the level. |
| 6 | Mandatory YOLO loss-attribution note: exit type, stop distance in ATR, ATR context (09-10) | **ACCEPTED — telemetry (see item 10)** | Partly present today: stop_price, rr_planned, entry_time, close_reason and the LLM's symbol snapshot are persisted. Missing: stop distance in ATR units and realised MFE/MAE. |
| 7 | Hold YOLO at floors until ≥20 closed trades show positive expectancy (09-14) | **REJECTED as written** | Redundant and freeze-inducing. Unevidenced size *increases* are already blocked from the automatic path (config caps + drawdown governor + the 30-trade gate on the auto-tuner); the review is the evidence-gated place where a size decision may be *changed*. The Sep 17 post-mortem is explicit that the deterministic governor — not a discretionary freeze — is the correct response to a losing streak (`0/8` is ~4% likely for a 2:1 system). |
| 8 | Keep YOLO risk increases gated on positive expectancy, ≥20 closed trades (09-16) | **REJECTED as written** | Duplicate of item 7; same reasoning. |
| 9 | Gate any YOLO size increase on demonstrated positive expectancy, n≥30 (09-18) | **REJECTED as written** | The 30-trade gate already exists in code and blocks the *automatic* path (`MIN_CLOSED_TRADES_TO_TUNE`, per bucket); a second prospective freeze adds nothing and would also block an evidence-backed decision by the review. |
| 10 | Exit-reason + stop-distance telemetry for every YOLO close (exit type, stop distance in ATR at entry, MFE/MAE in R, sector RS) (09-18) | **ACCEPTED — highest-value next change** | This is the change that makes the ≥30-trade verdict possible at all, and it is telemetry (no behaviour change), so it needs no backtest. Evidence it is needed: 5 of 8 closed YOLO rows have `close_reason = NULL`, and the "no YOLO trade ever reached +1R" claim was only answerable by re-measuring 5-min bars by hand this week (2 of 8 did reach +1R MFE — see below). Implementation sketch: at close, fetch 5-min bars from `entry_time` (fall back to `created_at`) to `exit_time`, store `stop_dist_atr`, `mfe_r`, `mae_r`, `exit_type` on the trade row; sector RS already exists in the entry snapshot. Method + pitfalls: skill `references/llm-decision-layer.md` §4. |
| 11 | Audit the -1.74R outlier: stop liveness / fill quality (09-18) | **CLOSED — audited** | SLV 2026-09-09→10: stop **59.470125**, exit **58.3392** — the fill was 1.9% through the stop because the position was held overnight into a gap, not because the stop was absent or asleep. The broker order record shows a live `sell stop @ 59.47` in status `filled`. The stop rule itself was never modified. Mitigation already in place: single names reporting within `block_earnings_within_days = 1` are refused (ETFs, which never report, are unaffected — that is the residual gap risk of holding overnight, accepted by design). |
| 12 | YOLO entry filter: require trend/sector alignment in `trend_up` (above 20d/50d, RS≥0) (09-18) | **DEFERRED — unmeasurable as written** | YOLO's entries are LLM decisions, so there is no deterministic rule to replay and no filter can be backtested (the backtest report cannot reproduce a single YOLO trade — §4.3). The signal is already supplied to the model every cycle: live output now cites sector RS and breadth directly ("narrow 18% breadth … reduced size"). A hard filter becomes reviewable only if YOLO ever gets a deterministic entry rule. |
| 13 | Low-volatility guard: skip or ATR-scale entries when SPY ATR%ile < 20 (09-18) | **REJECTED — premise contradicted by the existing split** | The sleeve with the evidence already scales volatility by construction (MR stop = 2.5×ATR(14)), and the regime split in the backtest shows MR *better* in quiet tapes (chop expR **+0.15**, n=162 vs trend **+0.11**, n=613). For the ORB the same split rests on n=15 and its CI straddles zero (n=57, avgR -0.01) — noise, not a signal. Today's SPY ATR%ile is 14.4, so deploying this would have stopped MR — the evidenced sleeve — in the current tape. |
| 14 | Reconcile written config with live knobs before the next cycle (09-18) | **CLOSED — reconciled** | Verified live 2026-09-21: `config.YOLO` defaults 0.25 / 0.02 / 4 AND the runtime `strategy_overrides.json` 0.25 / 0.02 / 4 AND the container's live read `0.25 0.02 4.0` all agree. The drift the note described was repaired by the 2026-09-17 restore. Standing rule (already in the skill): a config DEFAULT change is a silent no-op while the volume's override file holds the old value — always read the override file, never `config.YOLO[...]`, when diagnosing. |

The three AUTO-APPLIED entries in the queue (09-10, 09-14, 09-16 — the
streak-driven YOLO size cuts to 3/0.25/0.02, then 1/0.10/0.01, then
`max_position_pct` 0.05) are **superseded**: all three were flinch-based
de-risking on 5-7 closed trades, and the 2026-09-17 restore put the knobs back
to 0.25 / 0.02 / 4 under the drawdown governor. They are kept in the record as
the worked example of why the tuner is not allowed to react to a streak.

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


## 2026-09-28 12:20 UTC — WEEKLY REVIEW DISPOSITION (measured, not re-argued)

Five items in this queue are now ANSWERED. They are recorded in
`strategy_proposals_closed.md` (delivered to the nightly routine as
`settled_findings`, with an instruction not to re-file them):

1. **"Noise-tight stops / low-vol guard"** — FIXED, and visible in the data: stop
   distance in ATR was 0.14–0.80 for trades opened 2026-09-08..10 and is 1.1–3.0
   for everything opened since 2026-09-14 (the 2026-09-17 live-price bracket-geometry
   fix). The four sub-0.6-ATR stops are exactly the trades whose MFE exceeded +1R and
   still closed red (GLD +1.087R MFE → −1.07R; XLU +2.052R MFE → −1.04R).
2. **"Gate longs on own-trend + 5d RS vs SPY"** — TESTED AND NOT SUPPORTED.
   `backtest_mr.py` §12, fresh bars to 2026-09-25: control avgR +0.036 (n=2445) vs
   gated +0.038 (n=1016); difference +0.002R, Welch t = +0.10, 95% CI
   [−0.035, +0.040]. The gate discards 58% of trades for the same per-trade
   expectancy (it does cut max drawdown 31.1 → 24.4 R: an exposure choice, not an
   edge). Only the unconditional version was tested; a narrow-breadth-conditional
   version needs a breadth series and has not been run.
3. **"Config drift: live knobs vs floors"** — NOT DRIFT. `config.YOLO_TUNE` bounds
   are (0.05, 0.9) / (0.01, 0.1) / (1, 10): those are the tuner's ALLOWED LIMITS,
   not a floor the bot sits at. Live 0.25 / 0.02 / 4.0 sit at no bound. Stop citing
   the lessons tail for sizing state; cite the config.
4. **"No entries into HIGH-impact event windows"** — already implemented
   (`EVENT_GUARD enabled, blackout_min 30, event_day_size_cut 0.5,
   block_overnight_into_fomc`) and wired into every entry path via
   `guards.entry_gate/size_factor/fomc_block_new_overnight`; observed working on
   2026-09-23 (entry cut to 50% and journaled as an event day).
5. **"30-trade gate"** — already mechanical (`MIN_CLOSED_TRADES_TO_TUNE = 30` plus
   `self_improve._tuning_gate`). Not a proposal.

The −1.74R tail is also attributed: SLV (24 sh, entry 61.0000, stop 59.47012, exit
58.3392) measured MFE +0.451R / MAE −1.902R — it was carried overnight and the next
session opened through a 0.802-ATR stop. Gap through a noise-tight stop, not an
execution defect; the resting stop did what a resting stop can do.

What is NOT answered, and is the live open question: YOLO's entry quality. Median
MFE is +0.263R, 4 of 13 trades never traded above their entry at all (MFE ≤ 0), and
only 3 of 13 ever reached +1R. That is selection, not exit management — and the
sector-RS gate proposed to fix it is the one just tested as unsupported in its
unconditional form.

---

## 2026-10-05 12:10 UTC — WEEKLY REVIEW DISPOSITION (week of 2026-09-29 → 10-02)

**Books in R, and against SPY buy-and-hold over the same four sessions**
(SPY 765.49 on 09-28 → 769.65 on 10-02 = **+0.54%**):

| book | closed this week | week R | week net $ | all-time R (n) | vs start |
|---|---|---|---|---|---|
| yolo | 3 — SMH +2.02R (target), TLT +1.74R (LLM close), XLV −0.11R (scratch) | **+3.64R** | +$97.03 | **−5.44R (16)** | −0.94% |
| mr_rsi2 | 0 (1 opened: XLV 6sh, 50% size, event day) | — | — | +0.33R (2) | — |
| daily_orb | 0 (2 × no_go; 1 advisor error 09-30, fixed in 1bce541) | — | — | +0.21R (1) | +0.24% |
| weekly_pullback | 0 (never fired — rare trigger, see §4.1 of the backtest) | — | — | — | 0.00% |
| x_copy | 0 — shadow mode, no broker account | — | — | — | — |

YOLO's equity went 9844.95 (09-28) → 9905.93 = **+0.62% vs SPY's +0.54%**: the
autonomous book's **first green week**, and the first week it out-earned
buy-and-hold. All-time YOLO improved from −9.09R (13 trades) on 09-28 to
−5.44R (16).

**The entry-quality question moved, on 3 more trades.** `r_telemetry.py` re-run
live in the container 2026-10-05 12:05: median MFE **0.263R → 0.463R**,
MFE ≥ +1R **3/13 → 5/16**, capture **−1.467 → −0.517**. The two new winners are
the cleanest exits the book has produced: TLT MFE +1.766R → realised +1.74R and
SMH MFE +2.059R → realised +2.02R (capture ≈ 0.98 on both — the exits kept
almost everything the entries were offered).

**Classification: (b) sample too small to tell.** 3 closed trades this week.
n=16 against a 30-trade gate. **No strategy parameter was changed.** One
demonstrable defect was found and fixed — in the copy bot, not in a strategy.

**Demonstrable defect (fixed, `bdef56d`, VERSION 1.6.2): the copy bot dropped a
decision from the journal.** In its first 14 posts it read 11 as "no trade"
(hype, P&L brags, philosophy, sunsets), parked 2 of his "next full port play"
teases as `review` (the ticker was only in the attached image), and dispositioned
1 real trade — 2026-10-01 "$AMZN full ported … then exited at small profit". That
one was read (`kind=close`, AMZN, confidence 0.75) and marked
`rejected — he exited, but this bot does not hold it`, and **journaled nowhere**:
14 posts against 13 journal lines. The close path wrote the state to the table
and skipped the event, so the record read "New post from @fullportnik: 'full
ported $AMZN today…'" and then silence. Fixed; the regression test fails on the
old code and passes on the new one.

**PENDING (unevidenced, NOT deployed): give the copy reader eyes.** Both "full
port play" posts carried the ticker only in the attached image, and the text
reader correctly parks them as `review` rather than guessing. Turning image posts
into signals requires an extraction measured for recall on a labelled set first —
the same bar the text path had to clear (a 2–6-of-6 enumeration was rejected for
this reason). A vision path that invents a ticker is worse than one that parks it.
**No deploy without that measurement.**

**Also fixed:** `tests/test_x_copy.py`'s reply check had been failing since
2026-10-02 — it hard-coded 2026-09-28 timestamps and `_finish_posts` drops
anything older than `X_FEED['lookback_days']` (4). A permanently-red suite is
where a real regression hides; the dates are now relative to now.

**Watch next week:** whether the ≥+1R MFE rate keeps rising with sane stop
geometry (5/16 now), and whether QQQ (MFE +1.816R, MAE +0.001R — never went
adverse) reaches its target or gives the profit back. Two good closes are not a
trend and 16 trades is not 30.

