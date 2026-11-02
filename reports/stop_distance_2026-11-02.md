# Stop distance vs expectancy — where the noise band ends

Measured 2026-11-02 (weekly strategy review). **Question:** does a stop placed
inside the instrument's own daily noise band destroy expectancy, and at what
distance does the effect disappear?

**Verdict:** yes, decisively. A stop closer than ~0.5 ATR(14) is negative at
n≈5,000-7,800; the sign flips at 1.0 ATR and improves monotonically to 2.5 ATR.
This is the measurement behind `config.YOLO["min_stop_atr"] = 1.0` (VERSION 1.6.7).

## Method

Mechanical long, same signal for every variant so the ONLY thing that varies is
stop/target width:

- universe: the 19-ETF universe used by `backtest_mr.py`
- bars: Alpaca `1Day`, 1575 sessions per symbol, 2020-07-27 → 2026-10-30
  (free-tier history starts there; the older 2019 window in
  `reports/backtest_2026-09.md` was fetched on a different plan/endpoint)
- signal: `close > SMA20 and close > SMA50`, entry at the NEXT session's open
- stop: `entry - k*ATR14(prior bar)`; target `entry + 2*k*ATR14` (2:1 at every k)
- hold: max 10 sessions, then exit at the close
- costs: 1 bp adverse slippage per side (`SLIP_BPS` from `backtest_mr.py`); fees
  omitted (measured book fees are $0.83 across 33 trades — immaterial here)
- if a bar's range contains both stop and target, the STOP is taken (conservative)

Script: the probe is reproducible from the review's scratch file; the logic is
40 lines and uses only `indicators.atr` + `alpaca_rest`.

## Results

| k (×ATR14) | n | win% | avgR | se | t | sumR | in-sample avgR (n) | out-of-sample (2023+) avgR (n) |
|---|---|---|---|---|---|---|---|---|
| 0.15 | 7801 | 23.7 | **−0.357** | 0.015 | −24.36 | −2781.1 | −0.337 (2651) | −0.366 (5150) |
| 0.35 | 6384 | 32.5 | −0.099 | 0.018 | −5.36 | −630.1 | −0.082 (2138) | −0.107 (4246) |
| 0.50 | 5213 | 34.4 | −0.041 | 0.021 | −1.96 | −211.3 | −0.039 (1752) | −0.041 (3461) |
| 1.00 | 2887 | 39.9 | **+0.050** | 0.026 | +1.90 | +143.8 | +0.005 (976) | +0.073 (1911) |
| 1.50 | 2190 | 47.2 | **+0.080** | 0.026 | +3.11 | +176.0 | +0.020 (746) | +0.112 (1444) |
| 2.50 | 1866 | 54.1 | **+0.091** | 0.020 | +4.54 | +169.3 | +0.022 (638) | +0.127 (1228) |

Monotone in k, and the ordering holds in both halves. 1.0 ATR vs 0.15 ATR is
**+0.406R per trade**.

Note the mechanism: this is not "wider stops are safer" in the usual
risk sense — every variant risks the same 1R. A tight stop simply converts
normal intraday noise into stop-outs, so the 2:1 target (only 2k×ATR away) is
never reached. Win rate rises from 24% to 54% as k grows while the payout
stays 2:1, which is the signature of "the stop, not the entry, was the problem".

## What it does and does not prove

- It proves the MECHANISM: at ~0.15 ATR (what a 0.2%-of-price floor produces on a
  1.3%-ATR ETF) a 2:1 bracket loses ~0.36R per trade on 7,801 trades.
- It does NOT replay the live YOLO entries (LLM decisions are not reproducible),
  so it cannot price *this* guard exactly. The live book is the second leg of the
  evidence: all 7 entries with `trades.stop_atr < 1.0` lost (0/7, **−7.94R**),
  while the ≥1.0-ATR cohort (n=26) is **+8.17R / +0.314R avg**.
- The guard REJECTS an entry; it does not widen the stop. Widening at the same
  qty would raise dollar risk, which a review may not do. A rejected entry costs
  a trade; it never increases exposure.

## Caveat on the live "wide stop" cohort

The ≥1.5-ATR live cohort (+0.491R, n=13) uses a threshold chosen AFTER seeing the
data, and its CI spans roughly ±0.6R. The deployed floor is 1.0 ATR — the point
where the backtest crosses zero and where the mechanical argument ("a stop inside
one day's average range is inside the noise") is defensible — not 1.5.
