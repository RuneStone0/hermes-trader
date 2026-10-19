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

---

## 2026-10-19 12:00 UTC — WEEKLY REVIEW (week of 2026-10-12 → 10-16)

### The week in R, and against SPY buy-and-hold

| book | closed this wk | sumR wk | net $ wk | closed all-time | sumR all-time | equity | SPY since 2026-09-08 |
|---|---|---|---|---|---|---|---|
| yolo | 4 | **+3.52** | +$25.13 | 23 | **−1.94** | 9,901.12 (−0.99%) | −0.11% → **−0.88pp** |
| mr_rsi2 (daily acct) | 1 | −1.00 | −$36.40 | 4 | −0.21 | 9,985.10 (−0.15%) | **−0.04pp** |
| daily_orb (daily acct) | 0 | — | — | 1 | +0.21 | (same account) | — |
| weekly_pullback | 0 | — | — | 0 | — | 10,000.00 (0.00%) | **+0.11pp** (in cash) |
| **book** | **5** | **+2.52** | **−$11.27** | **28** | **−1.94** | — | **SPY −1.89% this week** |

SPY closed 778.03 (10-12) → 763.34 (10-16), **−1.89%** — its worst week of the
period. YOLO was **+3.52R / +$25.13** against it (three winners: XLV +2.09R at its
target, XLE +1.97R at its target, IWM +0.23R; one loser AMZN −0.77R). The book's
net dollars are negative for the week only because the single MR stop-out carried
**5.5× the risk** of the largest YOLO trade (−$36.40 at $36.33 risk vs +$15.52 at
$7.42) — which is the sizing point below, not a signal about either rule.

### Classification: (b) SAMPLE TOO SMALL — and one (c) DEMONSTRABLE DEFECT, found and fixed

Every bucket is below `MIN_CLOSED_TRADES_TO_TUNE` (30): yolo 23, mr_rsi2 4,
daily_orb 1, weekly 0. No parameter may be tuned, and nothing was. YOLO's −1.94R
over 23 trades is **t = −0.32**: statistically indistinguishable from zero. This
week's +3.52R is **four trades** and proves nothing on its own.

The defect of the week is not in the strategies — it is in the machinery that is
supposed to notice when a strategy stops working.

**MEASURED (live, in-container, 2026-10-19): the nightly self-improvement loop has
been returning nothing for 15 consecutive nightly runs (2026-09-28 → 2026-10-16,
counted from `/data/logs/self_improve.log`; the last productive run was
2026-09-25) because its answer was being
TRUNCATED, and the truncation was laundered into "nothing to say".**
Rebuilding the routine's exact payload (15,095-char user message, 5,983 prompt
tokens) against the live model:

| max_tokens | finish_reason | reasoning tokens | content | result |
|---|---|---|---|---|
| 4,000 (the live setting) | `length` | 2,559 | 5,235 chars, **cut mid-JSON** | `_extract_json` → `{}` → `isinstance(dict)` passes → **"appended 0 lessons; 0 proposals", exit 0** |
| 8,000 (same payload) | `stop` | 5,253 | 6,812 chars | **8 lessons + 4 proposals** |

`advisor.chat()` returned only `content`, so a starved call and a genuinely empty
one were the same string. That is the identical failure class the decision path
fixed with `_DECISION_MAX_TOKENS = 8000` in September — never applied to the
review call, which is the *only* mechanism this system has for adapting. The
dashboard showed nothing, `lessons_learned.md` had not been appended since
2026-09-25, and every bot's prompt was carrying September-era lessons while the
loop reported healthy every night.

**Fixed and deployed in v1.6.5** (no trading behaviour touched, no backtest
needed — this is telemetry/observability):
- `advisor.chat_full()` returns `content`, `reasoning_content`, `finish_reason`
  and `usage`; `chat()` is the unchanged content-only wrapper.
- the review budget is 9,000 (reasoning PLUS answer); a cut-off answer is retried
  once with the model's own truncated turn handed back and asked for the JSON
  alone (the `advisor.decide` recovery pattern);
- an unreadable answer now journals an **error** event and writes nothing; a
  GENUINE empty answer journals a visible **no-op** ("Nothing new") event. Failure
  and silence are no longer the same row.
- `weekly_run.py` printed nothing on its two silent return paths, so
  `weekly.log` was a wall of empty 1-second entries indistinguishable from a
  crashed run; both now print why.
- `tests/test_self_improve.py` pins all three modes (unreadable → error, empty →
  no-op, readable → written) end-to-end with a stubbed LLM.

### Is YOLO broken, or merely near breakeven? The shape says near breakeven

23 closed: **7W/16L (30.4%)**, avg win **+1.62R**, avg loss **−0.83R**. At that
shape the breakeven win rate is **33.9%** — it is running 0.8 trades short of
breakeven, i.e. one more winner in 23 would flip the book to positive. For a 2:1
system P(≤7 wins | p=1/3, n=23) = **48%**, so this is an ordinary draw, not a
conviction. Realised losses average −0.83R rather than −1.00R because the
discretionary exits cut losers early — which is also why the shape needs 34% and
not 50% wins.

Exit quality has genuinely improved: **8 of 23** trades reached ≥ +1R MFE and
**6 of those 8 converted** to ≥ +1R realised (XLV +2.09, XLE +1.97, QQQ +2.23,
SMH +2.02, TLT +1.74, XLU +1.05). The one that did not (GLD 09-08, MFE +1.09R →
−1.07R) is from the pre-2026-09-17 noise-stop era. So the current problem is
**not exit capture** — it is that only 35% of entries ever offer a 1R excursion.

### PENDING (filed, NOT deployed): risk per trade varies 16.4× — the dollar result is the sizing, not the edge

YOLO's realised initial risk spanned **$3.18 → $52.24** (0.03% → 0.52% of equity;
median $14.13) across 23 trades on the same $10k account. Consequence: **two SLV
trades (−1.74R and −1.00R) cost −$116.20, which is 104% of the book's −$111.98
net loss.** At the median risk the same R stream would be ≈ −$27. corr(risk$, R)
= **−0.19**, winners' average risk $14.43 vs losers' $19.62 — anti-correlated,
but n=23 and driven by those two trades, so **it is not convictable**.

**Proposal:** size every YOLO entry from a fixed risk budget (`risk_pct × equity /
stop_distance`) and treat the LLM's `size_pct` as a *conditional multiplier only*
(0.25–1.0), so the journal's R and the account's dollars measure the same thing.
**Why not deployed:** it does not change expectancy per trade — only variance and
measurability — so a backtest of the R stream cannot rank it, and with n=23 the
anti-correlation is not evidence. **Falsifier:** if at n≥30 corr(risk$, R) ≥ 0
then the LLM's sizing carries no information and normalising it is pure hygiene;
if corr stays ≤ −0.2 AND the two largest-risk trades are again the largest dollar
losers, the sizing lever is actively harmful and should be removed outright rather
than clamped.

### PENDING (filed, NOT deployed): mr_rsi2's shape needs a 79% win rate

4 closed: 3W (+0.11, +0.22, +0.46R) against one **−1.00R** stop. At an average win
of +0.26R the rule must win **79%** of the time to break even. Its winners are
capturing what they are offered (MFE +0.16/+0.39/+0.47 vs realised +0.11/+0.22/
+0.46 — capture 67%/57%/97%), so this is the rule's designed exit (RSI normalises
into a small profit) meeting a full-size stop, not a leak. The backtest measured
this profile as *positive* in chop (+0.15R, n=162), and n=4 cannot overturn it.
**Watch, do not tune:** the next MR loser and whether the win:loss R ratio
regresses toward the backtest's.

### Settled this week (recorded in strategy_proposals_closed.md)

- **"The self-improve loop has genuinely found nothing for two weeks."** → **FALSE,
  it was truncated at max_tokens=4000.** See the table above. Any future claim
  that the loop is "running and returning empty" must first check
  `finish_reason`/content length, not the summary line.
- **"weekly_pullback is dead — zero trades in 7 weeks."** → **NOT BROKEN, RARE.**
  It executes every weekday at 14:45 UTC (weekly.log, rc=0), and its trigger —
  price within 1×ATR(14) of the 100d SMA — fired on **76 of 1,944 sessions
  (3.9%)** in the backtest, i.e. ~10 setups/year, so 0 setups in ~35 looks has
  has p = **0.25** — ordinary, not broken. It is a low-frequency sleeve that will
  take **~3 years** to reach 30 closed trades. Do not call it dead, and do not
  expect it to become tunable.
- **"The journal's event counts tell you how often a bot ran."** → **NO.** `dedup=True`
  refreshes the timestamp of the last identical event, so the digest's
  `weekly_pullback skip n=2` represents ~35 evaluated runs and
  `daily_orb skip n=4` a whole month of ticks. Read the *age* of the last event
  (and the job log), never the count, to decide whether a sleeve is alive.

### Watch next week

1. **YOLO reaches the 30-trade gate first** (~4 closed/week → around 10-30). That
   is the first bucket where a parameter change becomes legal; until then every
   number above is an estimate with a confidence interval that straddles zero.
2. **The first two nights of the repaired loop** — the 10-19 21:30 UTC run should
   append lessons for the first time since 2026-09-25, and its journal row should
   read "Nothing new" only if it genuinely produced nothing. If it errors, the
   error names the finish_reason. Watch `lessons_learned.md` grow and read the
   lessons before trusting them: 15 nights of no review means the first batch is
   unreviewed output, and the auto-apply path is still gated at 30 trades.
3. **Tue 2026-10-20 12:30Z HIGH: Building Permits + Housing Starts** — the event
   guard should cut size 50%; confirm it journals "Sizing at 50%".
4. **ORB and weekly can never reach 30 closed trades in a year** at their current
   rates (1 trade / 6 weeks; ~10 setups / year). That is a structural question
   about what "tuned" can ever mean for a low-frequency sleeve — to be decided
   with a backtest, not this week.

### Addendum (12:35 UTC, same review): split the book by STOP QUALITY — the loss is the pre-fix cohort

The single most useful cut of the 23 YOLO rows, and it changes the reading of the
aggregate. Cohort = stop distance in ATR(14) at entry (telemetry, populated 23/23):

| cohort | n | sumR | avgR | t | capture (realised ÷ offered MFE) | MFE ≥ 1R |
|---|---|---|---|---|---|---|
| **sane stops ≥ 1.0 ATR** (every trade opened since 2026-09-14) | 16 | **+6.00** | **+0.375** | +1.19 (95% CI ±0.62 → [−0.24, +1.00]) | **+0.434** | 6/16 |
| noise stops < 1.0 ATR (the pre-2026-09-17 defect) | 7 | **−7.94** | −1.134 | −9.88 | −1.909 | 2/7 |
| all | 23 | −1.94 | −0.084 | −0.32 | −0.108 | 8/23 |

So "YOLO is near breakeven" is a **mixture of two different rules**: the seven
noise-stop trades lost almost exactly 1R each, as a stop sitting inside the noise
must, and they account for **−7.94R of the −1.94R total**; the sixteen trades taken
under the current geometry are **+6.00R with a +0.43 capture**. The 2026-09-17 fix
therefore has a real before/after, and the live rule is the positive half.

**Caveat, stated plainly:** the split is confounded with time and regime (all seven
noise-stop trades are from 2026-09-08..10, a falling tape), n=16 gives t=+1.19, and
the CI still includes zero. This is the best available reading, not a proven edge —
and it is the number to watch when the bucket passes 30.

### Disposition of the 2026-10-19 nightly batch (the repaired loop's first output)

The loop's first real output in 15 nights is in this queue above (3 structural
proposals, 8 lessons). Dispositions, each with the measurement:

1. **"Raise MIN_NET_RR from 0.5 to 1.0"** → **REJECTED AS INERT.** Measured
   `rr_planned` on all 23 closed YOLO rows: 2.00–2.27 (0 of 23 below 1.0, 0 below
   2.0; the only NULLs are the 3 pre-Sep-17 rows). The 0.5 net-RR floor never
   admitted anything this sleeve actually takes, so raising it changes no decision
   and no trade. Its own falsifier resolves on the "floor is not binding" branch.
2. **"Index-regime throttle: halve new entries when SPY is below its 20d and VIX is
   rising"** → **REJECTED — wrong sign on the only data that exists.** SPY vs its
   20d-at-entry, reconstructed from SPY daily closes for the 16 rows that carry an
   `entry_time` (7 pre-Sep-17 rows are NULL): below-20d **n=8, avgR +0.536** (3/8
   MFE≥1R) vs above-20d **n=8, avgR +0.208** (3/8). The throttle would have cut the
   *better* half. Difference +0.33R, t≈0.5 — no evidence in either direction, so
   nothing is deployed; the premise is contradicted in sign and the sample cannot
   settle it. Re-open only with n≥30 and a regime field persisted at entry (the
   journaled snapshot currently carries none — this cut had to be reconstructed).
3. **"Instrument exits: log exit reasons + MFE, shadow-track hold-to-target"** →
   **ALREADY IMPLEMENTED — duplicate of settled finding #10.** `mfe_r`, `mae_r` and
   `stop_atr` are populated for 23/23 YOLO, 4/4 MR and 1/1 ORB closed rows
   (probe 2026-10-19), and the capture column above is computed from them. The
   lesson that seeded it ("exit_telemetry is absent for YOLO/DAILY") is **false**:
   telemetry is absent only for `weekly_pullback`, which has no trades. Recorded as
   settled finding #17 so it is not re-proposed a third time.
4. **Kept from the batch:** the lesson that the stored lessons tail carries **stale
   regime numbers** (it said VIX 14.9 / ATR%ile 14 / breadth 27%; live is VIX 18.8 /
   ATR%ile 24 / breadth 73%) is a genuine finding — lessons must not present an old
   reading as the present state. Rule added to the operator skill.
5. **Note for the next review:** 7 of 23 YOLO rows still have `entry_time = NULL`
   (all pre-2026-09-17), which is why the regime split covers 16 of 23. Not worth a
   deploy; do not quote a "23-trade regime split" without saying so.
