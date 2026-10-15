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

**MOOT — CLOSED 2026-10-15: the whole sleeve was deleted (see the 2026-10-15 entry
at the end of this file). Do not action this.**

**PENDING, re-scoped by measurement (2026-10-11): give the copy reader eyes.**
Both "full port play" posts carried the ticker only in the attached image, and
the text reader correctly parks them as `review` rather than guessing. The
existing feed **cannot** be extended to read them, and that is now measured
rather than assumed: asked directly about that exact image post, the feed's
search tool reports `image_present: true, image_readable: false` with the tool
verifiably called (`usage: x_search_calls: 1, x_posts_fetched: 4`) — see
`probes/probe_image_read.py`. It can see THAT media exists, not what is in it.
The remaining path is the official X API (`attachments.media_keys` → media URLs,
needs a paid bearer token) feeding a vision model, and it faces the SAME bar the
text path had to clear: a recall measurement on a labelled set before it may
trade. **No deploy without that measurement.** Note the scale of what is hidden:
25 of the 31 posts in the live table carry media, so an image is the main place
the actionable part of a post would sit.

**Also fixed:** `tests/test_x_copy.py`'s reply check had been failing since
2026-10-02 — it hard-coded 2026-09-28 timestamps and `_finish_posts` drops
anything older than `X_FEED['lookback_days']` (4). A permanently-red suite is
where a real regression hides; the dates are now relative to now.

**Watch next week:** whether the ≥+1R MFE rate keeps rising with sane stop
geometry (5/16 now), and whether QQQ (MFE +1.816R, MAE +0.001R — never went
adverse) reaches its target or gives the profit back. Two good closes are not a
trend and 16 trades is not 30.


---

## 2026-10-12 12:05 UTC — WEEKLY REVIEW (week of 2026-10-05 → 10-09)

**Books in R and against SPY buy-and-hold.** SPY 774.97 (10-05 close) → 778.51
(10-09 close) = **+0.46%** for the week; 764.16 (2026-09-08, the bots' first
session) → 778.51 = **+1.88%** since go-live. Every number below is program output
from the live container's trade DB and broker API (`probes/probe_week_2026-10-12.py`,
`probe_week2_2026-10-12.py`, `probe_entry_quality.py`).

| book | closed this week | week R | week net $ | equity over the week | all-time R (n) | vs SPY since 09-08 |
|---|---|---|---|---|---|---|
| yolo | 3 — QQQ **+2.23R** (target), NVDA −1.26R (stop), SMH −1.00R (stop) | **−0.03R** | −$27.86 | 9927.31 → 9887.72 = **−0.40%** | **−5.46R (19)** | −1.12% vs +1.88% = **−3.00pp** |
| mr_rsi2 (daily acct) | 1 — XLV **+0.46R** (LLM close) | **+0.46R** | +$15.86 | 10030.84 → 10039.25 = **+0.08%** | +0.79R (3) | +0.39% vs +1.88% = −1.49pp |
| daily_orb | 0 — 9 × `no_go`, 4 × `skip` in 21 days | — | — | — | +0.21R (1) | — |
| weekly_pullback | 0 — ran every weekday 14:45, never triggered | — | — | 10000.00 flat | — | 0.00% |
| x_copy | 0 — shadow (no broker account); 9 posts read, all "no trade" | — | — | — | — | — |

YOLO's week was **flat in R** (−0.03R) and −0.86pp behind buy-and-hold; its equity
moved more than its closed trades because four positions were held through the week
(all four in modest profit at Friday's close: MSFT +$18.31, XLE +$6.79, XLV +$2.98,
AMZN +$0.69).

**Classification: (b) sample too small to tell.** Four closed trades this week
(3 YOLO + 1 MR) and 19 lifetime YOLO trades against the 30-trade gate. The
arithmetic says the book is *sitting on its own break-even line*, not that it has
no edge: YOLO's realised avg win is **+1.760R** and avg loss **−0.833R**, so it
needs a **32.1%** win rate to break even and has realised **21.1%** — and
P(≤4 wins in 19 | p=0.321) = **21.9%**, i.e. entirely ordinary variance for a
genuinely profitable 2:1 system. Not (a): the tape was a trend tape (SPY above its
20d, +0.46% for the week) and the one evidenced sleeve printed +0.46R. Not (c): the
two defect-shaped items were both audited and both cleared (below).

**The measurement that mattered this week — it killed a proposal before it cost
anything.** The only entry-side feature separating YOLO's winners from its losers
was *where in its own 20-day range the entry sat*: winners' median **0.60**, losers'
**0.83**, and the five entries taken at **≥0.90 of the range went 0 for 5, −3.59R**.
Tested properly at scale (`backtest_mr.py --range-study`, report
`reports/range_position_2026-10-12.md`, 19-ETF universe, **35,701 symbol-sessions**):
entries at ≥0.90 of range **+0.116R** vs **+0.119R** below it — difference
**−0.002R, Welch t=−0.21, 95% CI [−0.023, +0.019]**, straddling zero; flat in
up-trend sessions too (+0.007R, t=+0.53). **The live 0-for-5 was noise**
(P = 14.5%). Verdict filed as settled finding #12 so it is not re-derived from the
same 19 rows. This is the fourth time the range/RS family of entry gates has been
tested and the fourth time it has come back empty — the entry-quality problem is
real but it is not visible in any of the features measured so far.

**Audits performed (both clean, both now settled findings):**

* **NVDA −1.26R is an overnight gap, not a bad fill** (settled finding #11). The
  2026-10-07 session closed at 237.42; the first 5-min bar of 2026-10-08 opened at
  **234.795**, already below the 235.747 stop, and the fill at 234.1275 was inside
  that bar and better than its low. Both >1R losses in the book (SLV −1.74R, NVDA)
  are this class, and they cost **1.00R = 18.3%** of all YOLO losses — the measured
  price of the overnight hold, which the user enabled on purpose.
* **The copy bot's journal-drop defect has NOT regressed.** 19 `x_posts` rows
  against **19** disposition journal lines since the `bdef56d` fix; the single
  remaining row/line gap in the table is the historical pre-fix 2026-10-01 row.
* **Protection intact on every position.** All five open positions (MSFT, XLE, XLV,
  AMZN, SMH) carry a live `held` stop leg at the recorded stop plus a `new` limit
  target, and there are no live orders on flat symbols. Read with `orders_all()`
  — bracket legs are `held`, so `?status=open` would have shown none of them.

**What was changed: nothing in the strategies, and that is the verdict.** No
parameter may be tuned below the 30-closed-trade gate (YOLO 19, MR 3, ORB 1,
weekly 0), and no structural change earned deployment: the one candidate this week
died on its own backtest (above). Noise control matters more than activity — the
alternative was a YOLO entry filter built on five trades.

**Watch next week:** (1) whether YOLO's 6-of-19 ≥+1R-MFE rate holds as the book
grows past 20 — that number, not the P&L, is what decides whether the entry rule
works; (2) MSFT, open 12 sessions, MFE +0.89R, still under its 560.16 target;
(3) the two X posts parked `review` since 2026-09-29/30 whose tickers exist only in
an image — unchanged and still correctly parked.

**Process note (measured, not a defect):** the nightly `self_improve` routine has
produced **0 lessons and 0 proposals on 10 consecutive runs** (2026-09-28 →
2026-10-09: `[improve] appended 0 lessons; Auto-tuned 0 setting(s); 0 structural
proposal(s) left for review`, from `/data/logs/self_improve.log`). It is running,
calling the model, and returning empty — most likely the settled-findings list
doing its job plus the sample gate blocking every apply. The consequence is that
`lessons_learned.md` has not been appended since **2026-09-25**, so every bot's
prompt still carries September-era lessons and this weekly review is currently the
only source of new thinking. Not changed here: forcing the model to emit lessons
risks polluting the live prompts with filler nightly. If the next review still
shows an empty loop, the fix is to make an empty result a *journaled* event so the
streak is visible on the dashboard rather than buried in a log file.

---

## 2026-10-15 — COPY FOLLOWER DELETED (decision, not a proposal)

**Rune's decision, 2026-10-15 (relayed from the politiupdate profile): the
copy-follower sleeve is REMOVED COMPLETELY — not paused, not shadowed, not
re-scoped.** This is the disposition of the pending item "give the copy reader
eyes": it is **MOOT and must not be actioned** (the paid X API read token it was
waiting on is not to be bought), and nothing in this sleeve may be re-proposed
without overturning the measurement below.

Measurement that settled it (live 2026-10-15, from xAI's own console plus the
running container):

- The X feed (`xfeed.py` — the sole consumer of xAI's server-side `x_search` tool)
  was the entire "X posts fetched" line on the xAI bill: **$21.33/week** at $0.005
  per post fetched, plus **$6.79/week** of Grok tokens = **~$122/month**.
- It polled 144x/day, 24/7 (deliberately not market-gated) and its poll question
  carried no date bound, so a quiet poll re-fetched and re-paid for a post it
  already held: **4,300 billed fetches/week bought ~21 genuinely new posts =
  0.49% useful, ~$1.02 per captured post.**
- It never traded: **shadow mode, no Alpaca account was ever configured for it** —
  0 copy trades and 0 trade rows carrying a `signal_post_id`. No trading value was
  lost by deleting it.
- `X_BEARER_TOKEN` was unset, so it never used the official X API either
  (`feed_state.last_provider = xai/trigger`), and xAI was already answering HTTP
  429 `resource-exhausted` (2026-10-09, 10-14, 10-15).

What was deleted: `xfeed.py`, `x_copy_run.py`, `config.COPY` / `COPY_TUNE` /
`X_FEED`, the `x_feed` job in `app.py` (so the sleeve can never fire again), the
`/signals` endpoint, the copy dashboard page and its account card, the `x_posts`
and `feed_state` tables (dropped by migration; rows exported to
`archive/x-copy-follower/`), the `x-post-watch` Hermes cron and its wrapper, the
copy-only probes, and `tests/test_x_copy.py`. The daily / weekly / yolo / mr
sleeves, their Alpaca accounts and their data were NOT touched.

**Falsifier (what would justify re-opening it):** a captured post whose TEXT named
an instrument, plus positive expectancy over >= 30 closed trades, at a measured
cost per captured post under ~$0.05. Anything less is the same $122/month for the
same 0.5%.
