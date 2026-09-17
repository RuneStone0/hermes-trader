# Backtest report - live strategies vs mean-reversion alternatives (2026-09)

*Generated 2026-09-17 16:10 UTC by `backtest_mr.py` (stdlib only). Every number below is program output; none is hand-entered.*

## 1. Data actually obtained (no assumptions)

Requested `start=2019-01-01`, `timeframe=1Day`, feed = free IEX (the only feed the paper keys can read). True window returned per symbol:

| symbol | bars | first bar | last bar | error |
|---|---|---|---|---|
| SPY | 1938 | 2019-01-02 | 2026-09-17 | None |
| QQQ | 1938 | 2019-01-02 | 2026-09-17 | None |
| IWM | 1938 | 2019-01-02 | 2026-09-17 | None |
| DIA | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLE | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLF | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLK | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLV | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLI | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLY | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLP | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLU | 1938 | 2019-01-02 | 2026-09-17 | None |
| XLB | 1938 | 2019-01-02 | 2026-09-17 | None |
| SMH | 1938 | 2019-01-02 | 2026-09-17 | None |
| GLD | 1938 | 2019-01-02 | 2026-09-17 | None |
| SLV | 1938 | 2019-01-02 | 2026-09-17 | None |
| TLT | 1938 | 2019-01-02 | 2026-09-17 | None |
| IEF | 1938 | 2019-01-02 | 2026-09-17 | None |
| HYG | 1938 | 2019-01-02 | 2026-09-17 | None |

* **Daily bars:** 2019-01-02 -> 2026-09-17 (all 19 symbols, 1938 bars each - one page, no truncation).

* **5-min SPY bars:** 15813 bars, 2026-05-20T08:00 -> 2026-09-17T15:40. The free IEX feed caps 5-min history at roughly the last 120 calendar days - a request starting 2019-01-01 returned only 2019-01-01..2019-01-18 (2,249 bars, the first page ascending), so 5-min history cannot be extended backwards. The ORB variant therefore has its own ~4-month window.

* **Signal window:** `2019-01-02` -> `2026-09-16`. The final daily bar (`2026-09-17`) is the still-forming session at run time and is excluded from signals. The final 5-min session (`2026-09-17`) is likewise incomplete and is excluded from ORB sessions.


## 2. Regime definition

`chop` at bar *i* = the 20-day realised range `(max(high,20) - min(low,20)) / close` is below **4.0%** **and** `|close / SMA20 - 1|` is below **2.0%**, computed on the symbol's own bars. Everything else is `trend`. The threshold pair is a judgement call, so the sweep below shows how much the label changes with it:

| chop definition | share of bars labelled chop (per symbol) |
|---|---|
| range<3.5% & |px-sma20|<2.0% | SPY 8.0% QQQ 0.3% IWM 0.6% GLD 7.6% TLT 13.1% XLE 0.0% |
| range<4.0% & |px-sma20|<2.0% | SPY 13.4% QQQ 2.3% IWM 1.5% GLD 13.5% TLT 20.5% XLE 0.0% |
| range<4.5% & |px-sma20|<2.0% | SPY 21.5% QQQ 6.4% IWM 4.0% GLD 22.4% TLT 29.9% XLE 0.5% |
| range<5.0% & |px-sma20|<2.5% | SPY 30.8% QQQ 10.6% IWM 6.9% GLD 31.8% TLT 39.8% XLE 1.5% |

Under the primary definition SPY is in chop on **259/1937 days (13.4%)** of the 7.7-year window. The Sep 1-17 2026 window the live bots traded reads: 20-day range 3.41% of price, close -1.39% from SMA20 - i.e. **chop** under this definition.


## 3. Main results - full available window

`avgR`/`totR` are net of fees and slippage. `hold` = bars (sessions) held on average. `t` = expectancy / (stdev / sqrt(n)) - a crude significance check, **not** corrected for the fact that five rule variants were tried (see §7). `%bars` = share of eligible bars with a position open.

| variant | n | win% | avgR | totR | totR/1%expo | hold | t | maxCL | maxDD_R | %bars |
|---|---|---|---|---|---|---|---|---|---|---|
| ORB_5min (SPY) | 57 |   45.6% | -0.01 | -0.80 | -0.0 | 64.2 | -0.09 | 6 | 9.2 |   57.2% |
| ORB_daily_PROXY (SPY) | 327 |   42.8% | -0.06 | -20.42 | -0.3 | 3.5 | -0.69 | 16 | 43.7 |   59.7% |
| PULLBACK_weekly (SPY) | 76 |   46.1% | +0.11 | +8.65 | +0.6 | 3.6 | +0.77 | 5 | 8.1 |   14.1% |
| MR_rsi2 (19 syms) | 775 |   72.8% | +0.12 | +90.18 | +13.5 | 3.2 | +6.33 | 4 | 16.0 |    6.7% |
| MR_atr_dip (19 syms) | 177 |   67.2% | +0.21 | +36.48 | +14.2 | 5.3 | +3.60 | 5 | 11.9 |    2.6% |
| * CTRL_long_beta (19 syms, benchmark) | 2434 |   56.4% | +0.04 | +92.53 | +4.7 | 3.0 | +3.65 | 10 | 30.2 |   19.6% |

`* CTRL_long_beta` is **not a strategy candidate** - it is the beta benchmark: long-only, same universe, same 2.5x ATR stop, same fee/slippage model, but entries are mechanical (every 10th bar while close > 200d SMA) and exits after 3 bars, with no mean-reversion condition. It answers the only question that matters for the MR claim: how much R does 'long US equity ETFs in an uptrend' produce on its own? Control avgR = **+0.038** over n=2434 trades vs MR_rsi2 +0.116 (n=775) and MR_atr_dip +0.206 (n=177). MR_rsi2 adds **+0.078R per trade** over the timer entry; MR_atr_dip adds **+0.168R**. That difference - not the raw positive expectancy - is the claimed mean-reversion edge.


### 3.1 Exit mix and win/loss asymmetry

| variant | n | stops | targets | rule-exits | time-stops | flat-at-end | avg win R | avg loss R | profit factor |
|---|---|---|---|---|---|---|---|---|---|
| ORB_5min (SPY) | 57 | 31 | 26 | 0 | 0 | 0 | +1.26 | -1.08 | +0.98 |
| ORB_daily_PROXY (SPY) | 327 | 175 | 152 | 0 | 0 | 0 | +1.29 | -1.07 | +0.90 |
| PULLBACK_weekly (SPY) | 76 | 40 | 35 | 0 | 0 | 1 | +1.48 | -1.05 | +1.20 |
| MR_rsi2 (19 syms) | 775 | 86 | 0 | 684 | 4 | 1 | +0.37 | -0.57 | +1.75 |
| MR_atr_dip (19 syms) | 177 | 38 | 0 | 131 | 8 | 0 | +0.68 | -0.77 | +1.82 |
| CTRL_long_beta (19 syms, benchmark) | 2434 | 170 | 0 | 0 | 2264 | 0 | +0.39 | -0.42 | +1.21 |

A high win rate with a thin average R means the losses are fat when they come: the MR variants win ~70% of trades but the average loss is roughly 0.57R against an average win of 0.37R. That shape is fragile to a regime where stops are hit more often (see §5).


### 3.2 ORB_5min (the live rule on real 5-min bars)

Trades: **57** over 82 complete sessions (2026-05-20 -> 2026-09-16). Exits: stop=31, target=26, flat-at-end=0. Sessions with a breakout: 80/82; median opening range 2.51 index points. `hold` for this variant is in 5-min bars (78 per session), so 64 bars is about 0.8 sessions - the live rule allows overnight holds, and did.


## 4. The motivating window: Sep 1-17 2026

SPY: 11 sessions, 762.01 -> 754.05 (-1.04%), range 749.60-774.03 (3.26% wide). Regime label on those days: **11 of 11** were chop, 0 trend.

### 4.1 PULLBACK_weekly trigger frequency - bug or rare setup?

The live weekly bot fired **0 times in 12 sessions**. The trigger requires price to be within 1 ATR(14) of the 100d SMA (`sma < close <= sma + 1*ATR` for the long side). Distance from the SMA in ATR units, last 20 sessions:

| date | close | sma100 | atr14 | (close-sma100)/atr14 | trigger? |
|---|---|---|---|---|---|
| 2026-08-19 | 769.06 | 731.86 | 7.08 | +5.26 | no |
| 2026-08-20 | 762.60 | 733.16 | 7.07 | +4.16 | no |
| 2026-08-21 | 765.72 | 734.52 | 6.94 | +4.49 | no |
| 2026-08-24 | 763.47 | 735.66 | 6.71 | +4.15 | no |
| 2026-08-25 | 765.91 | 736.79 | 6.49 | +4.48 | no |
| 2026-08-26 | 766.08 | 737.91 | 6.27 | +4.49 | no |
| 2026-08-27 | 771.10 | 739.05 | 6.27 | +5.11 | no |
| 2026-08-28 | 769.35 | 740.16 | 6.33 | +4.61 | no |
| 2026-08-31 | 767.05 | 741.09 | 6.20 | +4.18 | no |
| 2026-09-01 | 761.78 | 741.93 | 6.30 | +3.15 | no |
| 2026-09-02 | 765.16 | 742.80 | 6.19 | +3.61 | no |
| 2026-09-03 | 773.17 | 743.69 | 6.38 | +4.62 | no |
| 2026-09-04 | 770.19 | 744.47 | 6.22 | +4.13 | no |
| 2026-09-08 | 765.96 | 745.14 | 6.14 | +3.39 | no |
| 2026-09-09 | 762.40 | 745.77 | 6.06 | +2.75 | no |
| 2026-09-10 | 757.83 | 746.26 | 6.04 | +1.92 | no |
| 2026-09-11 | 764.29 | 746.84 | 6.22 | +2.81 | no |
| 2026-09-14 | 760.88 | 747.42 | 6.23 | +2.16 | no |
| 2026-09-15 | 757.39 | 747.90 | 6.12 | +1.55 | no |
| 2026-09-16 | 754.05 | 748.38 | 6.54 | +0.87 | YES |

Sep 1-17 2026: trigger TRUE on **1/11** sessions (min distance +0.87 ATR, max +4.62 ATR). Over the whole 1838-day history the trigger was TRUE on **234 days (12.7%)** long=132, short=102 - i.e. the live bot's silence is a rare-setup outcome, not evidence of a broken signal path.


*Window note:* the live bots' window is 12 sessions (Sep 1-17). This backtest's signal window ends 2026-09-16, because the 2026-09-17 daily bar is the still-forming session at run time - hence 11 sessions here, not 12.


### 4.2 Live-time replay of the weekly trigger (why 0 fires might be expected anyway)

The live bot evaluates the trigger on the PARTIAL current daily bar, at the moment it runs. `schedule.py` runs the weekly bot at **14:45 UTC = 10:45 ET**, and its event journal on the trading host (read read-only) logs its last run at 2026-09-17T14:45Z, confirming that. So the trigger is replayed with the 10:45 ET price (today's partial bar as the last bar, SMA100/ATR14 computed on the same sequence the bot would hold). Every session in the live window:

| date | price@10:45 | sma100 | atr14 | dist@10:45 (ATR) | trigger@10:45 | final close | dist at close | trigger at close |
|---|---|---|---|---|---|---|---|---|
| 2026-09-01 | 763.92 | 741.95 | 6.18 | +3.55 | no | 761.78 | +3.21 | no |
| 2026-09-02 | 765.82 | 742.81 | 6.18 | +3.72 | no | 765.16 | +3.62 | no |
| 2026-09-03 | 768.72 | 743.65 | 6.09 | +4.11 | no | 773.17 | +4.84 | no |
| 2026-09-04 | 769.78 | 744.46 | 6.17 | +4.10 | no | 770.19 | +4.17 | no |
| 2026-09-08 | 767.26 | 745.16 | 6.08 | +3.64 | no | 765.96 | +3.42 | no |
| 2026-09-09 | 763.73 | 745.78 | 5.95 | +3.02 | no | 762.40 | +2.79 | no |
| 2026-09-10 | 758.80 | 746.27 | 6.04 | +2.07 | no | 757.83 | +1.91 | no |
| 2026-09-11 | 764.69 | 746.84 | 6.22 | +2.87 | no | 764.29 | +2.81 | no |
| 2026-09-14 | 758.62 | 747.40 | 6.21 | +1.81 | no | 760.88 | +2.17 | no |
| 2026-09-15 | 756.91 | 747.90 | 6.07 | +1.48 | no | 757.39 | +1.56 | no |
| 2026-09-16 | 759.62 | 748.43 | 5.90 | +1.90 | no | 754.05 | +0.95 | YES |

Over all 82 sessions of 5-min history: the trigger was TRUE at 10:45 ET on **0** sessions, but TRUE on the final close of **1** sessions. Checked at the price the bot actually saw, the setup was rarer still than the final-close statistic suggests.


### 4.3 What each rule did inside the live window (entries Sep 1-17 2026)

| variant | trades in window | total R | avg R | symbol:exit reason (first 6) |
|---|---|---|---|---|
| ORB_5min (SPY) | 10 | +0.82 | +0.08 | SPY:stop, SPY:target, SPY:stop, SPY:target, SPY:target, SPY:target... |
| ORB_daily_PROXY (SPY) | 1 | -1.01 | -1.01 | SPY:stop |
| PULLBACK_weekly (SPY) | 1 | -0.03 | -0.03 | SPY:eod_flat |
| MR_rsi2 (19 syms) | 8 | +1.86 | +0.23 | SPY:exit_rule, SPY:exit_rule, DIA:exit_rule, XLF:exit_rule, XLV:exit_rule, XLP:exit_rule... |
| MR_atr_dip (19 syms) | 0 |    n/a |    n/a |  |
| CTRL_long_beta (19 syms, benchmark) | 13 | -3.64 | -0.28 | SPY:max_hold, QQQ:max_hold, IWM:max_hold, DIA:max_hold, XLE:max_hold, XLF:max_hold... |

Context from the parent run (not reproduced here): the three live bots produced 9 trades in this window - daily ORB 1 trade +$9.27, weekly pullback 0 trades, YOLO 0 wins in 7 (net -$151.42, ~-7.9R at 1% risk). This backtest cannot reproduce the YOLO result at all: YOLO's entries are LLM decisions, not a rule, so there is nothing deterministic to replay.


## 5. Regime split - does the edge depend on regime?

Regime label of the symbol at the entry bar (definitions in §2). Same metrics, restricted to each regime.

| variant / regime | n | win% | avgR | totR | hold | t | maxCL | maxDD_R | PF |
|---|---|---|---|---|---|---|---|---|---|
| ORB_5min / chop | 15 |   53.3% | +0.18 | +2.73 | 58.5 | +0.59 | 3 | 3.1 | +1.37 |
| ORB_5min / trend | 42 |   42.9% | -0.08 | -3.53 | 66.2 | -0.46 | 6 | 9.2 | +0.87 |
| ORB_daily_PROXY / chop | 46 |   41.3% | -0.20 | -9.29 | 2.4 | -0.65 | 5 | 16.7 | +0.74 |
| ORB_daily_PROXY / trend | 279 |   42.7% | -0.05 | -13.14 | 3.7 | -0.51 | 16 | 34.7 | +0.92 |
| PULLBACK_weekly / chop | 4 |    0.0% | -0.77 | -3.06 | 0.8 | -3.11 | 4 | 3.1 | +0.00 |
| PULLBACK_weekly / trend | 72 |   48.6% | +0.16 | +11.71 | 3.8 | +1.05 | 4 | 7.1 | +1.29 |
| MR_rsi2 / chop | 162 |   72.8% | +0.15 | +23.78 | 2.9 | +3.49 | 3 | 4.2 | +1.99 |
| MR_rsi2 / trend | 613 |   72.8% | +0.11 | +66.40 | 3.3 | +5.31 | 4 | 13.3 | +1.70 |
| MR_atr_dip / chop | 29 |   69.0% | +0.24 | +7.10 | 5.2 | +1.54 | 3 | 2.7 | +1.89 |
| MR_atr_dip / trend | 148 |   66.9% | +0.20 | +29.38 | 5.4 | +3.25 | 4 | 9.4 | +1.80 |

### 5.1 Sensitivity of the split to the chop threshold

| threshold (range/dist) | variant | chop regime | trend regime |
|---|---|---|---|
| <3.5%/2.0% | MR_rsi2 | n=119 expR=+0.17 | n=656 expR=+0.11 |
| <3.5%/2.0% | MR_atr_dip | n=23 expR=+0.22 | n=154 expR=+0.20 |
| <3.5%/2.0% | PULLBACK_weekly | n=1 expR=-0.03 | n=75 expR=+0.12 |
| <3.5%/2.0% | ORB_daily_PROXY | n=26 expR=-0.34 | n=299 expR=-0.05 |
| <4.0%/2.0% | MR_rsi2 | n=162 expR=+0.15 | n=613 expR=+0.11 |
| <4.0%/2.0% | MR_atr_dip | n=29 expR=+0.24 | n=148 expR=+0.20 |
| <4.0%/2.0% | PULLBACK_weekly | n=4 expR=-0.77 | n=72 expR=+0.16 |
| <4.0%/2.0% | ORB_daily_PROXY | n=46 expR=-0.20 | n=279 expR=-0.05 |
| <4.5%/2.0% | MR_rsi2 | n=205 expR=+0.11 | n=570 expR=+0.12 |
| <4.5%/2.0% | MR_atr_dip | n=32 expR=+0.26 | n=145 expR=+0.19 |
| <4.5%/2.0% | PULLBACK_weekly | n=6 expR=-0.43 | n=70 expR=+0.16 |
| <4.5%/2.0% | ORB_daily_PROXY | n=74 expR=-0.01 | n=251 expR=-0.09 |
| <5.0%/2.5% | MR_rsi2 | n=252 expR=+0.10 | n=523 expR=+0.12 |
| <5.0%/2.5% | MR_atr_dip | n=44 expR=+0.20 | n=133 expR=+0.21 |
| <5.0%/2.5% | PULLBACK_weekly | n=8 expR=+0.05 | n=68 expR=+0.12 |
| <5.0%/2.5% | ORB_daily_PROXY | n=109 expR=+0.08 | n=216 expR=-0.14 |

## 6. Walk-forward honesty check (split at 2022-11-03)

| variant | first half | n | avgR | totR | t | second half | n | avgR | totR | t |
|---|---|---|---|---|---|---|---|---|---|---|
| ORB_5min | <2026-07-21 | 28 | +0.02 | +0.60 | +0.10 | >=2026-07-21 | 29 | -0.05 | -1.40 | -0.21 |
| ORB_daily_PROXY | <2022-11-03 | 162 | +0.02 | +3.91 | +0.19 | >=2022-11-03 | 165 | -0.15 | -24.33 | -1.12 |
| PULLBACK_weekly | <2022-11-03 | 44 | +0.05 | +1.99 | +0.24 | >=2022-11-03 | 32 | +0.21 | +6.66 | +0.88 |
| MR_rsi2 | <2022-11-03 | 301 | +0.05 | +15.01 | +1.45 | >=2022-11-03 | 474 | +0.16 | +75.17 | +7.74 |
| MR_atr_dip | <2022-11-03 | 86 | +0.09 | +8.04 | +1.07 | >=2022-11-03 | 91 | +0.31 | +28.44 | +4.28 |

A single split is a weak walk-forward: it is one draw of many possible splits. Read it as 'does the sign survive on unseen data', not as a validated edge.


## 7. Multiple comparisons, uncertainty, and the beta control test

`totR/1%expo` in §3 normalises total R by exposure (R earned per 1% of bars with a position open); it is the fair way to compare a low-exposure MR sleeve with a high-exposure benchmark.


### 7.1 MR vs the beta control - the decisive test

The MR variants' raw positive expectancy is not evidence of a mean-reversion edge: long US equity ETFs in an uptrend make money under almost any entry rule. The claim only survives if MR beats `CTRL_long_beta` on the same universe with the same stop, fees and slippage.

| variant | n | avgR | n ctrl | ctrl avgR | difference | Welch t | 95% CI of difference | CI > 0? |
|---|---|---|---|---|---|---|---|---|
| MR_rsi2 | 775 | +0.12 | 2434 | +0.04 | +0.08 | +3.71 | [+0.04, +0.12] | yes |
| MR_atr_dip | 177 | +0.21 | 2434 | +0.04 | +0.17 | +2.89 | [+0.05, +0.28] | yes |

If the difference CI straddles 0, the mean-reversion timing adds nothing measurable beyond 'be long in an uptrend' on this data.


### 7.2 Selection risk and per-variant uncertainty

Five rule variants were evaluated (not one). Under the null of zero edge, the best of five noisy variants looks good by selection alone; with 5 tests a Bonferroni-corrected two-sided 5% threshold is roughly |t| > 2.58. Best variant here: **MR_atr_dip (19 syms)** avgR=+0.206 t=3.60 (n=177; selected on the highest avgR - note MR_rsi2 carries the higher t at 6.33 on 4x the trades, so 'best' here is a judgement, not a significance result).

| variant | n | avgR | t | 95% CI of mean R (bootstrap) | CI > 0? |
|---|---|---|---|---|---|
| ORB_5min | 57 | -0.01 | -0.09 | [-0.32, +0.29] | no |
| ORB_daily_PROXY | 327 | -0.06 | -0.69 | [-0.24, +0.12] | no |
| PULLBACK_weekly | 76 | +0.11 | +0.77 | [-0.18, +0.40] | no |
| MR_rsi2 | 775 | +0.12 | +6.33 | [+0.08, +0.15] | yes |
| MR_atr_dip | 177 | +0.21 | +3.60 | [+0.09, +0.31] | yes |

Bootstrap CIs assume trades are independent and identically distributed - they ignore regime clustering, symbol correlation and the fact that the rules were chosen after seeing the live results. They are a floor on the uncertainty, not a ceiling.


## 8. MR variants per symbol (pooled tables hide size effects)

| symbol | rsi2 n | rsi2 avgR | rsi2 totR | dip n | dip avgR | dip totR |
|---|---|---|---|---|---|---|
| SPY | 51 | +0.26 | +13.02 | 12 | +0.71 | +8.51 |
| QQQ | 48 | +0.23 | +10.99 | 11 | +0.01 | +0.12 |
| IWM | 38 | +0.08 | +2.94 | 5 | +0.43 | +2.17 |
| DIA | 49 | +0.12 | +5.84 | 10 | -0.19 | -1.90 |
| XLE | 38 | +0.01 | +0.27 | 6 | +0.18 | +1.08 |
| XLF | 42 | +0.16 | +6.91 | 8 | +0.67 | +5.37 |
| XLK | 41 | +0.22 | +8.91 | 11 | +0.13 | +1.47 |
| XLV | 45 | +0.13 | +5.71 | 7 | -0.18 | -1.28 |
| XLI | 44 | +0.06 | +2.46 | 6 | +0.03 | +0.16 |
| XLY | 48 | +0.22 | +10.45 | 10 | +0.34 | +3.42 |
| XLP | 48 | +0.05 | +2.30 | 10 | -0.18 | -1.82 |
| XLU | 40 | +0.03 | +1.01 | 6 | +0.10 | +0.61 |
| XLB | 43 | -0.01 | -0.35 | 11 | +0.34 | +3.71 |
| SMH | 46 | +0.23 | +10.62 | 13 | +0.42 | +5.40 |
| GLD | 34 | +0.12 | +4.04 | 15 | +0.30 | +4.53 |
| SLV | 32 | -0.27 | -8.57 | 11 | -0.15 | -1.68 |
| TLT | 18 | +0.15 | +2.67 | 7 | +0.29 | +2.01 |
| IEF | 24 | +0.13 | +3.06 | 9 | +0.33 | +2.94 |
| HYG | 46 | +0.17 | +7.90 | 9 | +0.19 | +1.69 |

## 9. Benchmark context

| window | dates | sessions | SPY open->close | from running high |
|---|---|---|---|---|
| full window | 2019-01-02 -> 2026-09-16 | 1937 | +242.69% | -3.1% |
| first half | 2019-01-02 -> 2022-11-03 | 969 | +60.48% | -21.4% |
| second half | 2022-11-03 -> 2026-09-16 | 969 | +113.28% | -3.1% |
| Sep 1-17 2026 | 2026-09-01 -> 2026-09-16 | 11 | -1.04% | -2.5% |

## 10. What this does NOT prove

* **The ORB rule is only tested on ~4 months.** 5-min history is capped by the free IEX
  feed, so `ORB_5min` says nothing about 2019-2025, and its sample is far too small for
  significance. `ORB_daily_PROXY` is **not the live rule** - daily bars do not contain the
  09:30-10:00 range; it only probes breakout structure and must not be quoted as a
  validation of the deployed ORB bot.
* **No intra-bar path.** Stops, targets and close-based exits are resolved from OHLC (5-min
  for ORB). When a stop and an exit condition both trigger on one bar, the stop is assumed
  hit first - conservative, but it is an assumption, not data.
* **Entry at the signal bar's close.** The live ORB bot enters intraday and `backtest.py`
  even assumes a fill at the range boundary; here entries are at the confirming close (more
  realistic) but still an idealisation. The weekly bot's limit at the last price is modelled
  as a fill at that close, which flatters it slightly.
* **Pooling across symbols ignores correlation.** MR trades across 19 ETFs are treated as
  independent equal-risk bets with unlimited concurrent capital; in reality their dips
  cluster (a market-wide selloff triggers many at once), so pooled total R overstates a
  tradable portfolio's result.
* **Fees are regulatory only.** Alpaca is commission-free and these ETFs are penny-spread,
  so costs are dominated by the 1 bp/side slippage ASSUMPTION, which is not measured from
  quotes. Doubling it degrades the MR variants roughly in proportion to trade count - rerun
  with `--slip-bps 2` to see the sensitivity.
* **Survivorship/selection in the universe.** The 19 symbols are today's liquid ETFs; a
  universe chosen in 2019 might have differed.
* **One macro sample.** 2019-2026 was a large bull market for US equity ETFs (SPY
  {spyret} opentoclose over the window). Long-only rules inherit that. The beta control in §3
  and the MR-vs-control test in §7.1 are the only defensible answers to it, and they rest on
  a control that is itself a crude proxy (timer entry, 3-bar hold) whose trades OVERLAP the
  MR variants' in time - the two samples are not independent, so §7.1's Welch t understates
  the uncertainty even though its CI already allows for unequal variances.
* **Multiple comparisons.** Five variants were tested on overlapping data; the best one is
  partly selection luck. Nothing here is Bonferroni-clean.
* **No LLM layer, no live execution.** The live bots gate every setup through an LLM advisor
  and a fee-adjusted R:R floor. This measures the RULES only, so it is an upper bound on
  trade count and an estimate of raw setup edge - not a simulation of the bots' behaviour.
* **The MR exits are weak conditions.** "Close above the 5d SMA" (rsi2) and "close above
  the 10d SMA" (atr_dip) are low bars - most MR exits are the rule exit, not the stop or a
  target (see §3.1). What this measures is therefore mostly the ENTRY timing, not an
  optimised exit.


## 11. Reproduce

```
cd /opt/data/profiles/trader/trading
TRADER_HOME=/opt/data/profiles/trader python3 tests/test_backtest_mr.py
TRADER_HOME=/opt/data/profiles/trader python3 backtest_mr.py --cache data/backtest_bars.json
```

Bars cache: `data/backtest_bars.json` (fetched with a bars-only probe running the deployed `alpaca_rest.py` inside the `hermes-trader` container; `1Day` from 2019-01-01, plus the ~120 days of SPY `5Min` the free feed allows).


### 7.3 Cost sensitivity of the MR edge

The edge over the control is thin (+0.08R for rsi2), so it is worth knowing how fast costs eat it. Slippage is the only cost that matters here (the regulatory fees are cents), and it is an ASSUMPTION, not measured from quotes - so the whole grid is recomputed below with the same simulator.

| slip bp/side | variant | n | avgR |
|---|---|---|---|
| 0.0 | MR_rsi2 | 775 | +0.124 |
| 0.0 | MR_atr_dip | 177 | +0.214 |
| 0.0 | CTRL_long_beta | 2434 | +0.045 |
| 1.0 | MR_rsi2 | 775 | +0.116 |
| 1.0 | MR_atr_dip | 177 | +0.206 |
| 1.0 | CTRL_long_beta | 2434 | +0.038 |
| 2.0 | MR_rsi2 | 775 | +0.109 |
| 2.0 | MR_atr_dip | 177 | +0.198 |
| 2.0 | CTRL_long_beta | 2434 | +0.031 |
| 5.0 | MR_rsi2 | 775 | +0.086 |
| 5.0 | MR_atr_dip | 177 | +0.176 |
| 5.0 | CTRL_long_beta | 2434 | +0.010 |

Read the losing point: if the mean R of an MR variant crosses the control's under a plausible (not extreme) slippage assumption, the claimed edge is a cost assumption, not a finding.

