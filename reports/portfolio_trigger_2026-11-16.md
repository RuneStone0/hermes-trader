# Portfolio sleeve: is the trigger alive, and how big is the live-vs-lab timing gap?

2026-11-16, weekly strategy review. Read-only probes against live Alpaca data and
the container DB; no orders, no parameter changes. This settles two questions the
new portfolio sleeve raised in its first week, before either could be re-argued:

1. The sleeve ran twice (2026-11-12, 2026-11-13) and journaled "no setups in 14
   symbols" both times. Is this the `weekly_pullback` death mode repeating?
2. The champion rule was measured on the 16:00 ET **close** with a fill at the
   **next open**. The live job scans at 20:45 UTC (15:45 ET), 15 minutes before
   the close, using an in-progress daily bar. How different is the rule it trades?

## 1. The trigger is alive: 85.3% of trading days produce at least one setup

Replaying `portfolio_run._signal` (SMA50 / ATR14 / 1.0-ATR band, the exact code
path) over **948 daily bars x 14 universe symbols**, 2022-11-18 .. 2026-11-13:

| measure | value |
|---|---|
| symbol-days evaluated | 13,272 |
| signals | 1,828 (**13.8%** of symbol-days) |
| trading days with >= 1 setup | **809 of 948 = 85.3%** |
| signals per signal-day | 1.85 average |
| per-symbol signal rate | 10.3% (SLV) to 17.9% (XLE, XLB) |

The last 30 signal-days run from 2026-09-21 to 2026-11-13, i.e. the trigger kept
firing through the whole trend-down stretch.

**Why the two empty live runs were correct.** On 2026-11-12 no symbol closed
inside the band either. On 2026-11-13 XLE closed at **64.93 against an SMA50 of
64.9108** (dist **+0.01 ATR**), so the close-based rule scores one setup that day;
at the 20:45 scan XLE was still below its SMA50 and correctly invisible.

**Live path verified with real data** (not just "the code arrived"):
`docker exec hermes-trader python3 portfolio_run.py --dry-run --assume-open`

```
[portfolio] 1 setup(s), 8 slot(s), gross $0 of $10,000
[portfolio] BUY XLE x19 @~64.93  stop=62.58 target=68.85  notional=$1,234 (12.3%)  pullback=0.01 ATR
```

This is NOT settled finding #15. That verdict was about a sleeve whose trigger
essentially never coincided with a tradeable price AND which then passed an LLM
veto on top (10 evaluations, 0 trades in its lifetime). This sleeve has no veto
and a trigger that fires on 85% of days.

## 2. The 15-minute timing gap is real but bounded

Measured on the 5-minute history the free IEX tier serves (14 symbols, **81
symbol-days** with a usable 20:40 UTC bar):

| measure | value |
|---|---|
| signal flips (15:45 price vs official close) | **0 of 81** |
| missed setups (close says yes, 15:45 says no) | 0 |
| extra setups (15:45 says yes, close says no) | 0 |
| last-15-minute move, ATR units (n=39) | median **0.019**, p90 0.095, max 0.230 |

A signal can only flip if it sits within roughly a 0.02-ATR move of a band edge,
which is what happened to XLE on 2026-11-13. That is a real fidelity gap, and 81
symbol-days is too few to bound the flip rate tightly, but it is small enough that
it cannot be shown to change the measured edge.

**No timing change was deployed.** Moving the scan after the close (21:05 UTC)
would match the lab's *signal* convention but change the *fill* convention from
"same-day 15:45 market" to "next open", adding an overnight gap to every entry on
a strategy that holds for weeks. That trade needs its own evidence, not two runs.

## 3. Action taken

`portfolio_run.py` (v1.6.8) now prints the nearest misses whenever a scan finds
nothing. Actual output from the deployed container, with XLE removed from the
universe to force the empty branch (2026-11-16, real bars):

```
[portfolio] no setups across 13 symbols; closest: XLB +0.48 ATR outside, TLT +0.57 ATR outside, IWM +0.64 ATR outside
```

Diagnostic only (`_band_gap_atr`), pinned by `tests/test_portfolio_sleeve.py` so it
cannot influence entries: the entry decision still comes from `_signal` alone. A
bare "no setups" line cannot distinguish "nothing qualified" from "the scan is
blind", and that ambiguity is what gets a live sleeve declared dead on no evidence.

## Falsifiers

- The sleeve is **dead**, not quiet, if >= 10 consecutive scheduled runs report no
  setups while a close-based replay of those same days shows >= 1 setup. That
  would be a broken scan path (stale bars / wrong universe), not a rare rule.
- The **timing gap matters** if a longer 5-minute history shows >= 5% of signals
  flipping between 15:45 and the close, or a live run misses a setup whose
  `dist_atr` was > 0.05 ATR at scan time. Then the after-close variant gets its
  own fill-convention study.
