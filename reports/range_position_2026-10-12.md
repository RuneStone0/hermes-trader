# Study: entry position in the 20-day range vs forward follow-through

*Generated 2026-10-12 12:08 UTC by `backtest_mr.py --range-study` (stdlib only; every number is program output).*

Mechanical long at each session close (universe of 19 ETFs), stop = 2.5x ATR14, target = 2.0x risk, horizon 10 sessions, stop resolved before target, fees/slippage not applied (this measures EXCURSION, not a tradable equity curve).

Overlapping windows: the same session appears in the forward window of earlier rows, so the effective sample is far smaller than n and the t below is optimistic.

### All sessions (n=35701)

| in-range bucket | n | med MFE R | MFE>=1R | avg R | win% |
|---|---|---|---|---|---|
| <0.2 | 5513 | +0.72 | 1789/5513 | +0.151 | 57.6% |
| 0.2-0.4 | 5182 | +0.64 | 1445/5182 | +0.096 | 53.9% |
| 0.4-0.6 | 5481 | +0.62 | 1506/5481 | +0.089 | 54.2% |
| 0.6-0.8 | 6946 | +0.65 | 1921/6946 | +0.114 | 55.5% |
| 0.8-0.9 | 5030 | +0.66 | 1435/5030 | +0.146 | 57.6% |
| 0.9-1.0001 | 7549 | +0.61 | 1942/7549 | +0.116 | 56.4% |

- entries at >=0.90 of range: n=7549 avgR=+0.116  (MFE>=1R 1942/7549)
- entries below 0.90        : n=28152 avgR=+0.119  (MFE>=1R 8096/28152)
- difference -0.002R, Welch t=-0.21, 95% bootstrap CI [-0.023, +0.019]

### Sessions in an up-trend (close > SMA50, SMA50 rising) (n=19945)

| in-range bucket | n | med MFE R | MFE>=1R | avg R | win% |
|---|---|---|---|---|---|
| <0.2 | 608 | +0.66 | 167/608 | +0.067 | 54.1% |
| 0.2-0.4 | 1427 | +0.60 | 378/1427 | +0.044 | 48.8% |
| 0.4-0.6 | 2734 | +0.63 | 764/2734 | +0.069 | 52.5% |
| 0.6-0.8 | 4952 | +0.65 | 1409/4952 | +0.106 | 54.5% |
| 0.8-0.9 | 3976 | +0.66 | 1143/3976 | +0.130 | 56.4% |
| 0.9-1.0001 | 6248 | +0.61 | 1630/6248 | +0.104 | 55.4% |

- entries at >=0.90 of range: n=6248 avgR=+0.104  (MFE>=1R 1630/6248)
- entries below 0.90        : n=13697 avgR=+0.097  (MFE>=1R 3861/13697)
- difference +0.007R, Welch t=+0.53, 95% bootstrap CI [-0.019, +0.033]

