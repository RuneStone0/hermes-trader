# Exit attribution and post-exit run-on: 2026-11-09

Question this settles: **is YOLO's realised capture low because the EXIT is too
tight?** Three pending proposals (2026-11-03, 2026-11-05, 2026-11-06) and the
2026-11-02 #19 follow-up all asked a version of this, and all deferred it because
the book published no exit_reason split and no post-exit run-on. Both were
measured on 2026-11-09 against the live DB and live bars. Read-only; no orders.

## 1. Exit-reason split (47 closed YOLO trades, all live)

| close_reason | n | ΣR | median MFE (R) | median give-back (MFE − realised R) |
|---|---|---|---|---|
| target | 8 | **+16.66** | 2.08 | 0.07 |
| stop | 8 | **−8.27** | 0.13 | 1.15 |
| market (LLM close) | 26 | **−1.24** | 0.29 | 0.53 |
| NULL (pre-telemetry broker exits) | 5 | **−5.90** | 0.33 | 1.40 |

Every cent of the book's +1.24R comes from 8 target hits. The 26 discretionary
"market" closes, 55% of all closes, net **−1.24R for the lot** (avg −0.048R):
the LLM's exit judgement is worth zero in aggregate, not negative. The 5 NULL
rows are the pre-2026-10-26 broker-side exits that were never journaled; they
are the worst cohort (−1.18R each) and they are history, not a live behaviour.

There is **no `time` exit at all** in this book: the reasons are stop, target,
market, NULL. So the pending proposal's "exit_reason 'time' at over 40% of
closes" condition is not merely unmet, the category does not exist.

## 2. Post-exit run-on (the decisive measurement)

For each of the 47 closes: the first 5 five-minute RAW bars after the exit fill,
converted to R with the trade's own entry-to-stop distance. Raw, not adjusted,
because the fills are raw (see references/exit-quality-telemetry.md trap 3).

| cohort | n | median run-on | mean | share > 0 | ≥ 0.5R | ≥ 1.0R |
|---|---|---|---|---|---|---|
| all closes | 47 | **+0.09R** | +0.13R | 87% | 2 | 1 |
| exit = market | 26 | **+0.04R** | +0.06R | 81% | 0 | 0 |
| exit = target | 8 | +0.12R | +0.13R | 88% | 0 | 0 |
| exit = stop | 8 | +0.13R | +0.13R | 100% | 0 | 0 |
| MFE ≥ 1R cohort | 14 | **+0.07R** | +0.16R | 86% | 1 | 1 |

The falsifier written into the 2026-11-06 proposal was: "if exit_reason 'time' is
under 40% of closes AND the 1R-reaching cohort has median post-exit run-on below
0.5R, then the capture gap is not an exit-timing problem at all". Time exits are
0% and the 1R cohort's median run-on is **0.07R**, fourteen times below the
threshold. **The falsifier fired. The exit-timing hypothesis is dead on this
book**: after these exits print, the tape offers a median of seven hundredths of
an R, on the winners and the losers alike.

## 3. Trailing-stop replay (the mechanism that remains plausible)

Run-on measures the tape AFTER the exit. A trailing stop is a different claim: it
tightens the GIVE-BACK inside the trade (market exits give back a median 0.53R
from their peak). So it was replayed on the same 47 entries, on the same 5-min
bars, walking bar by bar:

- initial stop and target unchanged; trail level = peak − frac × risk, never
  tighter than the original stop;
- the trail level in force during a bar is computed from the peak as of the
  PREVIOUS bar (no intra-bar look-ahead; v1 of this probe had that bug and it
  inflated the tight fractions);
- a bar that touches both the stop and the trail is booked at the STOP;
- trades the trail never triggers keep the outcome they actually got.

One caveat kept honest: it is "trail OR the LLM exit, whichever comes first", so
it is a fair representation of ADDING a trail to the existing book.

| trail frac | ΣR | Δ vs baseline |
|---|---|---|
| baseline (actual exits) | +1.34 | |
| 0.25R | **+3.26** | +1.92 |
| 0.35R | +1.79 | +0.45 |
| 0.50R | **−0.99** | −2.33 |
| 0.75R | **−3.77** | −5.12 |
| 1.00R | +0.61 | −0.74 |
| 1.50R | +1.80 | +0.45 |

(ΣR here is gross of fees so all variants share units; the actual book is +1.24R
net, so the baseline reproduces.)

**Verdict: REJECT, on the proposal's own pre-registered test.** The best cell
(0.25R) improves the book by +1.92R total = **+0.041R per trade**, against a 95%
CI half-width of **±0.29R** on the mean per-trade delta (sd of the delta 1.02R,
n=47). The improvement is one seventh of its own noise. The drop-top-2 test does
pass (baseline −3.26R over the remaining 45, trail +1.55R, so the gain is not two
outliers), but a test that passes while the effect is 7x smaller than the
confidence interval does not justify a live change.

The stronger argument is the SHAPE of the response curve: +1.92, +0.45, −2.33,
−5.12, −0.74, +0.45 as the fraction rises. A real mechanism varies smoothly with
its parameter and keeps its sign; this one flips sign twice across adjacent
fractions. The "best" cell is a threshold coincidence, not an edge. This is the
same failure mode as settled #19, at a different threshold.

## 4. What this closes, and what it does not

Closed: the question "are the exits leaving money on the table?" is answered NO
for this book at n=47, by a measurement that would have shown a large effect if
one existed (median run-on 0.09R against a 0.31R median offered excursion).

Not closed, and deliberately not turned into a rule: the entry side. The book's
median MFE is 0.31R against an average stop that costs 1.0R, so 4 of 5 trades
never offer a full R of favourable excursion. That is an entry-quality statement
and no exit rule can repair it.

Reproduce:

    python3 /opt/data/profiles/trader/scripts/send_probe.py probes/post_exit_runon.py
    python3 /opt/data/profiles/trader/scripts/send_probe.py probes/replay_trail_full.py
