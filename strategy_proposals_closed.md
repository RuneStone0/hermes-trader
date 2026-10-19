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

## 8. "The copy bot has produced no trade — the feed or the reader must be broken"
**VERDICT: NOT BROKEN. The source published nothing copyable. Measured 2026-10-05
(14 posts, `x_posts` table + journal).**

Every post the bot has seen since it went live (2026-09-29), dispositioned:
11 → `ignored` "no trade" (profit brags, follower thanks, trading philosophy,
Robinhood-summit chatter, two NYC sunsets); 2 → `review` (his two "next full port
play" teases — the ticker existed only in the attached image, so the text reader
parked them **on purpose** instead of guessing); 1 → `rejected` (2026-10-01
"$AMZN full ported, then exited at small profit" — an exit of something the bot
never held). Zero entries, and correctly so: he has not yet named a ticker in
text. If a future review calls this bot idle-and-broken, it must first show a post
whose TEXT named an instrument and produced no action.

## 9. "A read can be missing from the journal even though it happened"
**VERDICT: DEFECT FOUND AND FIXED 2026-10-05 (`bdef56d`, VERSION 1.6.2).**

The `kind == "close"` branch of `x_copy_run.run()` wrote the outcome to the
`x_posts` table and journaled nothing when the exit was not mirrored — the only
terminal branch that did. Measured: **14 `x_posts` rows against 13 disposition
journal lines**; the missing one is exactly the 2026-10-01 AMZN rejected exit
(state=`rejected`, kind=`close`, confidence 0.75). The module's own contract says
a post is "recorded as read-and-ignored with the reason, never quietly dropped" —
that is why this is a defect and not a style choice.

The general rule: **a decision written to a table but not to the journal is
invisible to the person reading the dashboard.** Audit a bot by counting the rows
it dispositioned against the lines it wrote; any gap is a silent drop. A
regression test now pins it (`tests/test_x_copy.py`, fails on the old code).

## 10. "The copy bot should read the ticker out of the attached image"

**VERDICT: the path proposed is measured closed; the idea is not dead, its route
changed.**

Asked directly about the image-only post the reader parked as `review` (id
`2104582454606799186`, "can't wait to blast all of this at my next full port
play"), the feed's own search tool answers `image_present: true,
image_readable: false` — with the tool verifiably called (`x_search_calls: 1`,
`x_posts_fetched: 4`), so it is not a refusal to try. It can see that media
exists; it cannot see what is in it. Probe: `probes/probe_image_read.py`.

Scale of what that leaves on the floor: **25 of the 31 posts** in the live
`x_posts` table carry media. For this account the image is not a corner case, it
is the main place an actionable ticker would sit.

Remaining route: the official X API returns `attachments.media_keys` and media
URLs (paid bearer token), and those URLs can be handed to a vision model as an
image input. That is a NEW source whose availability has to be proven, and it
still faces the original bar — a recall measurement on a labelled set before it
may trade, because a vision path that invents a ticker is worse than one that
parks it. Until then an image-only post stays `review` with the reason saying so,
which is what it does today.

**Related measurement from the same investigation:** with the reader's rules
tightened to see named interests (2026-10-11), re-reading all 31 live posts
yields exactly **1** actionable post — 2026-10-05, "interesting names to maybe
full port: $ORCL … $RDDT … $AMZN". The other 30 stay `none`, correctly: the
mocking "trading $snap stock is literal masochism" is not a short, "sunset from
the full port apartment" is not a signal, and a Druckenmiller quote is not a
position. One in 31 is the honest rate for a source whose real calls are paid,
and it is the reason the mention rules must not be loosened to manufacture more.

---

## 11. "A closed loss worse than −1R means the stop was absent, late, or badly filled (execution defect)"

**VERDICT: MEASURED 2026-10-12 — both >1R losses in the book are overnight gaps
through a resting stop. Not a defect. Do not re-propose without a fill worse than
the first trade printed beyond the stop.**

Two trades in 23 closed rows lost more than one risk unit, and both were carried
overnight:

* **SLV 2026-09-09→10, −1.74R** (closed already in item 7: MFE +0.451R / MAE
  −1.902R, a 0.802-ATR stop).
* **NVDA 2026-10-06→08, −1.26R.** Entry 242.03, stop 235.74696, exit 234.1275.
  Measured from the feed's own 5-min bars: the last print of the 2026-10-07
  session was **237.42**, and the first 5-min bar of 2026-10-08 **opened at
  234.795** — already 0.95 below the stop — with the bar low 233.87. The fill at
  234.1275 is *inside* that first bar and better than its low, so the resting stop
  did what a resting stop can do; the gap is the cost. NVDA's measured MFE is
  +0.102R: the trade never worked, and no exit rule could have saved it.

Cost of the gap class, measured: 0.74R (SLV beyond −1R) + 0.26R (NVDA) =
**1.00R of the book's −5.46R total = 18.3% of all YOLO losses**. That is the price
of the overnight hold the user explicitly enabled — the real mitigation is position
size into a known event window (which the event guard already halves), not a wider
stop. Probe: `probes/probe_week2_2026-10-12.py`.

## 12. "Gate long entries away from the top of the instrument's 20-day range (YOLO buys the high)"

**VERDICT: TESTED 2026-10-12 ON 35,701 SYMBOL-SESSIONS — NOT SUPPORTED. The
effect does not exist at scale. Do not deploy; do not re-propose without a new
mechanism.**

Why it was worth testing: this was the ONLY entry-side feature that separated the
live YOLO book's winners from its losers. In-range position = (entry − 20d low) /
(20d high − 20d low), measured per closed trade from daily bars
(`probes/probe_entry_quality.py`): the 4 winners' median was **0.60**, the 15
losers' **0.83**; and the five entries taken at **≥0.90 of the range went 0 for 5,
sum −3.59R, avg −0.72R**, against avg −0.13R for the other 14. All five of those
top-decile entries also failed to reach +1R MFE. Suspicious-looking — and n=5.

The mechanism, tested properly (`backtest_mr.py --range-study`, report
`reports/range_position_2026-10-12.md`; mechanical long at each session close,
2.5×ATR14 stop, 2:1 target, 10-bar horizon, stop resolved before target, 19-ETF
universe, bars through 2026-09-25):

| in-range bucket | n | med MFE R | MFE≥1R | avg R | win% |
|---|---|---|---|---|---|
| <0.2 | 5513 | +0.72 | 1789/5513 | +0.151 | 57.6% |
| 0.2–0.4 | 5182 | +0.64 | 1445/5182 | +0.096 | 53.9% |
| 0.4–0.6 | 5481 | +0.62 | 1506/5481 | +0.089 | 54.2% |
| 0.6–0.8 | 6946 | +0.65 | 1921/6946 | +0.114 | 55.5% |
| 0.8–0.9 | 5030 | +0.66 | 1435/5030 | +0.146 | 57.6% |
| ≥0.9 (top decile) | 7549 | +0.61 | 1942/7549 | +0.116 | 56.4% |

Entries at ≥0.90 of range: **+0.116R** (n=7549). Entries below 0.90: **+0.119R**
(n=28152). Difference **−0.002R, Welch t=−0.21, 95% bootstrap CI
[−0.023, +0.019]** — straddles zero. Restricted to up-trend sessions (close >
SMA50 and SMA50 rising, the tape YOLO actually selects): +0.104R (n=6248) vs
+0.097R (n=13697), difference **+0.007R, t=+0.53, CI [−0.019, +0.033]** — also
straddles zero. Median MFE is flat (0.61–0.72R) in every bucket.

Conclusion: **buying near the 20-day high is not the defect; the live 0-for-5 was
noise** (P(0 wins in 5 | the book's 32% win rate) = 14.5%). Recording it here so
next week's review does not re-derive it from the same 19 rows. The real open
question is unchanged and unnamed by any feature measured so far: what makes
YOLO's 6-of-19 entries reach +1R MFE at all. Caveat carried by the study:
forward windows overlap, so the effective sample is far below the nominal n.

## 13. "The copy follower is worth keeping — build on it (paid X read token, image reading, mention signals)"
**VERDICT: REMOVED AS A STRATEGY 2026-10-15. Do not re-propose.** Rune's decision,
relayed from the politiupdate profile: the sleeve is deleted outright, not paused.

Measurement: the feed was the sole consumer of xAI's server-side `x_search` tool and
made up the whole "X posts fetched" line on the xAI bill — **$21.33/week** at $0.005
per post fetched plus **$6.79/week** of Grok tokens (**~$122/month**) — for 4,300
billed fetches/week that bought **~21 genuinely new posts (0.49% useful, ~$1.02 per
captured post)**, while polling 144x/day with no date bound on the question. It
never traded: shadow mode, no Alpaca account, 0 copy trades and 0 trade rows with a
`signal_post_id`. Findings #8, #9 and #10 above are therefore historical — there is
no code left for them to apply to (`tests/test_x_copy.py` was deleted with it).

Falsifier: a captured post whose TEXT named an instrument AND a positive net-R book
over >= 30 closed trades, at under ~$0.05 per captured post.

## 14. "The nightly self-improve loop is running and genuinely finds nothing (14+ nights of '0 lessons, 0 proposals')"
**VERDICT: FALSE — the answer was being TRUNCATED at `max_tokens=4000`, and the
truncation was laundered into a no-op. Fixed in v1.6.5 (2026-10-19).**

Measurement, live in-container 2026-10-19 on the routine's real payload
(15,095-char user message, 5,983 prompt tokens, `deepseek-flash`):

| max_tokens | finish_reason | reasoning tokens | content | outcome |
|---|---|---|---|---|
| 4,000 (the live setting) | `length` | 2,559 | 5,235 chars, cut mid-JSON | `_extract_json` → `{}`; `isinstance(out, dict)` passes; routine prints "appended 0 lessons; Auto-tuned 0 setting(s); 0 structural proposal(s)" and exits 0 |
| 8,000 (same payload) | `stop` | 5,253 | 6,812 chars | **8 lessons + 4 proposals** |

`advisor.chat()` returned only `content`, so a starved call and a genuinely empty
one were indistinguishable — the same failure class as the September decision-path
starvation (`_DECISION_MAX_TOKENS = 8000`), never applied to the review call. The
loop is the only adaptation mechanism the system has; `lessons_learned.md` had not
been appended since 2026-09-25.

**Standing rule:** never conclude the loop "has nothing to say" from its summary
line. Check `finish_reason` and the content length. An unreadable answer now
journals an `error`; a genuine empty answer journals a visible `no-op`.

**Falsifier / what a real no-op looks like:** `finish_reason='stop'`, a parseable
object, both lists empty — that is now a distinct journaled row, so the two cases
can be told apart from the database instead of from a log line.

## 15. "weekly_pullback is dead — zero trades in 7 weeks"
**VERDICT: NOT BROKEN — RARE. Do not re-propose 'fix the weekly sleeve' without
naming a broken invariant. (2026-10-19)**

Measurement: the job executes every weekday at 14:45 UTC (`/data/logs/weekly.log`,
rc=0 every run — its log lines were empty because the two skip paths printed
nothing, fixed in v1.6.5). Its trigger (price within 1×ATR(14) of the 100d SMA)
fired on **76 of 1,944 backtest sessions = 3.9%**, i.e. ~10 setups/year, so zero
setups in the ~35 looks since 2026-09-01 has **p = 0.25** — ordinary. At that rate
the sleeve needs **~3 years** to reach `MIN_CLOSED_TRADES_TO_TUNE` (30).

**Falsifier:** a weekday where `weekly.log` has no entry, a run with rc≠0, or a
skipped setup whose reason is anything other than "no pullback to SMA today".

## 16. "The journal's EVENT COUNTS tell you how often a bot ran or took a decision"
**VERDICT: NO — they undercount by construction. Read the last event's AGE and
the job log. (2026-10-19)**

`db.log_event(..., dedup=True)` refreshes the timestamp of the last identical
`(account, strategy, decision, reason)` row instead of inserting a new one, so the
digest's `weekly_pullback skip n=2` represents ~35 evaluated runs and
`daily_orb skip n=4` a month of 5-minute ticks. The dedupe is deliberate (it keeps
the journal readable and makes the dashboard's "age of last decision" meaningful);
the mistake is reading counts as activity. This is the same trap as #2 in the
skill's pitfalls — audit by counting what a bot *dispositioned* against what it
*journaled*, and use the log for run frequency.

---

Settled findings are also mirrored at
`/data/trading/strategy_proposals_closed.md` inside the `hermes-trader`
container — that volume copy is the one the nightly routine reads.

