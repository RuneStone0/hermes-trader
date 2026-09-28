# Backtest report - live strategies vs mean-reversion alternatives (2026-09)

*Generated 2026-09-28 12:08 UTC by `backtest_mr.py` (stdlib only). Every number below is program output; none is hand-entered.*

## 1. Data actually obtained (no assumptions)

Requested `start=2019-01-01`, `timeframe=1Day`, feed = free IEX (the only feed the paper keys can read). True window returned per symbol:

| symbol | bars | first bar | last bar | error |
|---|---|---|---|---|
| SPY | 1944 | 2019-01-02 | 2026-09-25 | None |
| QQQ | 1944 | 2019-01-02 | 2026-09-25 | None |
| IWM | 1944 | 2019-01-02 | 2026-09-25 | None |
| DIA | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLE | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLF | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLK | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLV | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLI | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLY | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLP | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLU | 1944 | 2019-01-02 | 2026-09-25 | None |
| XLB | 1944 | 2019-01-02 | 2026-09-25 | None |
| SMH | 1944 | 2019-01-02 | 2026-09-25 | None |
| GLD | 1944 | 2019-01-02 | 2026-09-25 | None |
| SLV | 1944 | 2019-01-02 | 2026-09-25 | None |
| TLT | 1944 | 2019-01-02 | 2026-09-25 | None |
| IEF | 1944 | 2019-01-02 | 2026-09-25 | None |
| HYG | 1944 | 2019-01-02 | 2026-09-25 | None |

* **Daily bars:** 2019-01-02 -> 2026-09-25 (all 19 symbols, 1944 bars each - one page, no truncation).

* **5-min SPY bars:** 15766 bars, 2026-06-01T08:00 -> 2026-09-28T11:50. The free IEX feed caps 5-min history at roughly the last 120 calendar days - a request starting 2019-01-01 returned only 2019-01-01..2019-01-18 (2,249 bars, the first page ascending), so 5-min history cannot be extended backwards. The ORB variant therefore has its own ~4-month window.

* **Signal window:** `2019-01-02` -> `2026-09-24`. The final daily bar (`2026-09-25`) is the still-forming session at run time and is excluded from signals. The final 5-min session (`2026-09-28`) is likewise incomplete and is excluded from ORB sessions.


## 2. Regime definition

`chop` at bar *i* = the 20-day realised range `(max(high,20) - min(low,20)) / close` is below **4.0%** **and** `|close / SMA20 - 1|` is below **2.0%**, computed on the symbol's own bars. Everything else is `trend`. The threshold pair is a judgement call, so the sweep below shows how much the label changes with it:

| chop definition | share of bars labelled chop (per symbol) |
|---|---|
| range<3.5% & |px-sma20|<2.0% | SPY 8.0% QQQ 0.4% IWM 0.6% GLD 7.6% TLT 13.2% XLE 0.0% |
| range<4.0% & |px-sma20|<2.0% | SPY 13.6% QQQ 2.4% IWM 1.5% GLD 13.4% TLT 20.7% XLE 0.0% |
| range<4.5% & |px-sma20|<2.0% | SPY 21.9% QQQ 6.5% IWM 4.0% GLD 22.3% TLT 30.1% XLE 0.5% |
| range<5.0% & |px-sma20|<2.5% | SPY 31.0% QQQ 10.7% IWM 6.9% GLD 31.7% TLT 39.9% XLE 1.6% |

Under the primary definition SPY is in chop on **265/1943 days (13.6%)** of the 7.7-year window. The Sep 1-17 2026 window the live bots traded reads: 20-day range 3.57% of price, close +0.43% from SMA20 - i.e. **chop** under this definition.


## 3. Main results - full available window

`avgR`/`totR` are net of fees and slippage. `hold` = bars (sessions) held on average. `t` = expectancy / (stdev / sqrt(n)) - a crude significance check, **not** corrected for the fact that five rule variants were tried (see §7). `%bars` = share of eligible bars with a position open.

| variant | n | win% | avgR | totR | totR/1%expo | hold | t | maxCL | maxDD_R | %bars |
|---|---|---|---|---|---|---|---|---|---|---|
| ORB_5min (SPY) | 59 |   47.5% | +0.03 | +1.59 | +0.0 | 62.7 | +0.17 | 6 | 9.2 |   57.9% |
| ORB_daily_PROXY (SPY) | 329 |   43.2% | -0.06 | -18.83 | -0.3 | 3.5 | -0.64 | 16 | 42.3 |   59.8% |
| PULLBACK_weekly (SPY) | 76 |   47.4% | +0.13 | +10.14 | +0.7 | 3.6 | +0.89 | 5 | 8.1 |   14.2% |
| MR_rsi2 (19 syms) | 775 |   72.8% | +0.12 | +90.19 | +13.5 | 3.2 | +6.35 | 4 | 15.9 |    6.7% |
| MR_atr_dip (19 syms) | 177 |   67.2% | +0.21 | +36.49 | +14.3 | 5.3 | +3.60 | 5 | 11.9 |    2.6% |
| CTRL_trend_rs (19 syms, +gate) | 1016 |   56.5% | +0.04 | +38.54 | +4.7 | 2.9 | +2.35 | 10 | 24.4 |    8.1% |
| * CTRL_long_beta (19 syms, benchmark) | 2445 |   56.1% | +0.04 | +87.82 | +4.5 | 3.0 | +3.46 | 10 | 31.1 |   19.6% |

`* CTRL_long_beta` is **not a strategy candidate** - it is the beta benchmark: long-only, same universe, same 2.5x ATR stop, same fee/slippage model, but entries are mechanical (every 10th bar while close > 200d SMA) and exits after 3 bars, with no mean-reversion condition. It answers the only question that matters for the MR claim: how much R does 'long US equity ETFs in an uptrend' produce on its own? Control avgR = **+0.036** over n=2445 trades vs MR_rsi2 +0.116 (n=775) and MR_atr_dip +0.206 (n=177). MR_rsi2 adds **+0.080R per trade** over the timer entry; MR_atr_dip adds **+0.170R**. That difference - not the raw positive expectancy - is the claimed mean-reversion edge.


### 3.1 Exit mix and win/loss asymmetry

| variant | n | stops | targets | rule-exits | time-stops | flat-at-end | avg win R | avg loss R | profit factor |
|---|---|---|---|---|---|---|---|---|---|
| ORB_5min (SPY) | 59 | 31 | 28 | 0 | 0 | 0 | +1.25 | -1.08 | +1.05 |
| ORB_daily_PROXY (SPY) | 329 | 175 | 153 | 0 | 0 | 1 | +1.28 | -1.07 | +0.91 |
| PULLBACK_weekly (SPY) | 76 | 40 | 36 | 0 | 0 | 0 | +1.48 | -1.08 | +1.23 |
| MR_rsi2 (19 syms) | 775 | 85 | 0 | 684 | 4 | 2 | +0.37 | -0.56 | +1.76 |
| MR_atr_dip (19 syms) | 177 | 38 | 0 | 131 | 8 | 0 | +0.68 | -0.77 | +1.82 |
| CTRL_trend_rs (19 syms, +gate) | 1016 | 80 | 0 | 0 | 932 | 4 | +0.39 | -0.42 | +1.21 |
| CTRL_long_beta (19 syms, benchmark) | 2445 | 171 | 0 | 0 | 2263 | 11 | +0.39 | -0.42 | +1.20 |

A high win rate with a thin average R means the losses are fat when they come: the MR variants win ~70% of trades but the average loss is roughly 0.56R against an average win of 0.37R. That shape is fragile to a regime where stops are hit more often (see §5).


### 3.2 ORB_5min (the live rule on real 5-min bars)

Trades: **59** over 82 complete sessions (2026-06-01 -> 2026-09-25). Exits: stop=31, target=28, flat-at-end=0. Sessions with a breakout: 80/82; median opening range 2.51 index points. `hold` for this variant is in 5-min bars (78 per session), so 63 bars is about 0.8 sessions - the live rule allows overnight holds, and did.


## 4. The motivating window: Sep 1-17 2026

SPY: 12 sessions, 760.12 -> 760.71 (+0.08%), range 747.74-772.11 (3.26% wide). Regime label on those days: **12 of 12** were chop, 0 trend.

### 4.1 PULLBACK_weekly trigger frequency - bug or rare setup?

The live weekly bot fired **0 times in 12 sessions**. The trigger requires price to be within 1 ATR(14) of the 100d SMA (`sma < close <= sma + 1*ATR` for the long side). Distance from the SMA in ATR units, last 20 sessions:

| date | close | sma100 | atr14 | (close-sma100)/atr14 | trigger? |
|---|---|---|---|---|---|
| 2026-08-27 | 769.19 | 737.22 | 6.26 | +5.11 | no |
| 2026-08-28 | 767.44 | 738.33 | 6.31 | +4.61 | no |
| 2026-08-31 | 765.15 | 739.26 | 6.19 | +4.18 | no |
| 2026-09-01 | 759.89 | 740.09 | 6.29 | +3.15 | no |
| 2026-09-02 | 763.26 | 740.96 | 6.17 | +3.61 | no |
| 2026-09-03 | 771.25 | 741.85 | 6.36 | +4.62 | no |
| 2026-09-04 | 768.28 | 742.62 | 6.21 | +4.13 | no |
| 2026-09-08 | 764.06 | 743.30 | 6.12 | +3.39 | no |
| 2026-09-09 | 760.51 | 743.92 | 6.04 | +2.75 | no |
| 2026-09-10 | 755.95 | 744.42 | 6.02 | +1.92 | no |
| 2026-09-11 | 762.40 | 744.99 | 6.20 | +2.81 | no |
| 2026-09-14 | 759.00 | 745.57 | 6.21 | +2.16 | no |
| 2026-09-15 | 755.51 | 746.05 | 6.10 | +1.55 | no |
| 2026-09-16 | 752.18 | 746.52 | 6.53 | +0.87 | YES |
| 2026-09-17 | 760.71 | 747.03 | 6.74 | +2.03 | no |
| 2026-09-18 | 761.69 | 747.53 | 6.55 | +2.16 | no |
| 2026-09-21 | 773.50 | 748.18 | 7.02 | +3.61 | no |
| 2026-09-22 | 773.38 | 748.84 | 6.70 | +3.66 | no |
| 2026-09-23 | 767.81 | 749.37 | 6.72 | +2.75 | no |
| 2026-09-24 | 767.18 | 749.87 | 6.64 | +2.61 | no |

Sep 1-17 2026: trigger TRUE on **1/12** sessions (min distance +0.87 ATR, max +4.62 ATR). Over the whole 1844-day history the trigger was TRUE on **234 days (12.7%)** long=132, short=102 - i.e. the live bot's silence is a rare-setup outcome, not evidence of a broken signal path.


*Window note:* the live bots' window is 12 sessions (Sep 1-17). This backtest's signal window ends 2026-09-16, because the 2026-09-17 daily bar is the still-forming session at run time - hence 11 sessions here, not 12.


### 4.2 Live-time replay of the weekly trigger (why 0 fires might be expected anyway)

The live bot evaluates the trigger on the PARTIAL current daily bar, at the moment it runs. `schedule.py` runs the weekly bot at **14:45 UTC = 10:45 ET**, and its event journal on the trading host (read read-only) logs its last run at 2026-09-17T14:45Z, confirming that. So the trigger is replayed with the 10:45 ET price (today's partial bar as the last bar, SMA100/ATR14 computed on the same sequence the bot would hold). Every session in the live window:

| date | price@10:45 | sma100 | atr14 | dist@10:45 (ATR) | trigger@10:45 | final close | dist at close | trigger at close |
|---|---|---|---|---|---|---|---|---|
| 2026-09-01 | 762.03 | 740.11 | 6.17 | +3.55 | no | 759.89 | +3.21 | no |
| 2026-09-02 | 763.92 | 740.97 | 6.17 | +3.72 | no | 763.26 | +3.61 | no |
| 2026-09-03 | 766.82 | 741.80 | 6.08 | +4.11 | no | 771.25 | +4.84 | no |
| 2026-09-04 | 767.87 | 742.62 | 6.15 | +4.10 | no | 768.28 | +4.17 | no |
| 2026-09-08 | 765.36 | 743.31 | 6.06 | +3.64 | no | 764.06 | +3.42 | no |
| 2026-09-09 | 761.83 | 743.94 | 5.93 | +3.02 | no | 760.51 | +2.79 | no |
| 2026-09-10 | 756.92 | 744.43 | 6.02 | +2.08 | no | 755.95 | +1.91 | no |
| 2026-09-11 | 762.80 | 744.99 | 6.20 | +2.87 | no | 762.40 | +2.81 | no |
| 2026-09-14 | 756.74 | 745.55 | 6.20 | +1.81 | no | 759.00 | +2.17 | no |
| 2026-09-15 | 755.04 | 746.05 | 6.05 | +1.49 | no | 755.51 | +1.56 | no |
| 2026-09-16 | 757.74 | 746.58 | 5.89 | +1.90 | no | 752.18 | +0.95 | YES |
| 2026-09-17 | 759.37 | 747.02 | 6.73 | +1.84 | no | 760.71 | +2.04 | no |

Over all 81 sessions of 5-min history: the trigger was TRUE at 10:45 ET on **0** sessions, but TRUE on the final close of **1** sessions. Checked at the price the bot actually saw, the setup was rarer still than the final-close statistic suggests.


### 4.3 What each rule did inside the live window (entries Sep 1-17 2026)

| variant | trades in window | total R | avg R | symbol:exit reason (first 6) |
|---|---|---|---|---|
| ORB_5min (SPY) | 11 | -0.21 | -0.02 | SPY:stop, SPY:target, SPY:stop, SPY:target, SPY:target, SPY:target... |
| ORB_daily_PROXY (SPY) | 2 | -0.53 | -0.27 | SPY:stop, SPY:eod_flat |
| PULLBACK_weekly (SPY) | 1 | +1.47 | +1.47 | SPY:target |
| MR_rsi2 (19 syms) | 9 | +1.38 | +0.15 | SPY:exit_rule, SPY:exit_rule, DIA:exit_rule, XLF:exit_rule, XLF:eod_flat, XLV:exit_rule... |
| MR_atr_dip (19 syms) | 0 |    n/a |    n/a |  |
| CTRL_trend_rs (19 syms, +gate) | 4 | -0.19 | -0.05 | QQQ:max_hold, XLE:max_hold, XLK:max_hold, SMH:max_hold |
| CTRL_long_beta (19 syms, benchmark) | 13 | -3.63 | -0.28 | SPY:max_hold, QQQ:max_hold, IWM:max_hold, DIA:max_hold, XLE:max_hold, XLF:max_hold... |

Context from the parent run (not reproduced here): the three live bots produced 9 trades in this window - daily ORB 1 trade +$9.27, weekly pullback 0 trades, YOLO 0 wins in 7 (net -$151.42, ~-7.9R at 1% risk). This backtest cannot reproduce the YOLO result at all: YOLO's entries are LLM decisions, not a rule, so there is nothing deterministic to replay.


## 5. Regime split - does the edge depend on regime?

Regime label of the symbol at the entry bar (definitions in §2). Same metrics, restricted to each regime.

| variant / regime | n | win% | avgR | totR | hold | t | maxCL | maxDD_R | PF |
|---|---|---|---|---|---|---|---|---|---|
| ORB_5min / chop | 21 |   52.4% | +0.15 | +3.20 | 55.0 | +0.60 | 5 | 5.2 | +1.31 |
| ORB_5min / trend | 37 |   45.9% | -0.02 | -0.59 | 68.2 | -0.08 | 6 | 9.2 | +0.97 |
| ORB_daily_PROXY / chop | 47 |   42.6% | -0.18 | -8.69 | 2.5 | -0.61 | 5 | 16.5 | +0.76 |
| ORB_daily_PROXY / trend | 280 |   42.9% | -0.04 | -12.16 | 3.7 | -0.47 | 16 | 33.5 | +0.93 |
| PULLBACK_weekly / chop | 4 |   25.0% | -0.39 | -1.57 | 1.2 | -0.63 | 3 | 3.0 | +0.48 |
| PULLBACK_weekly / trend | 72 |   48.6% | +0.16 | +11.71 | 3.8 | +1.05 | 4 | 7.1 | +1.29 |
| MR_rsi2 / chop | 161 |   73.3% | +0.15 | +23.91 | 2.9 | +3.51 | 3 | 4.2 | +2.00 |
| MR_rsi2 / trend | 614 |   72.6% | +0.11 | +66.28 | 3.3 | +5.32 | 4 | 13.3 | +1.70 |
| MR_atr_dip / chop | 29 |   69.0% | +0.24 | +7.10 | 5.2 | +1.54 | 3 | 2.7 | +1.89 |
| MR_atr_dip / trend | 148 |   66.9% | +0.20 | +29.39 | 5.4 | +3.25 | 4 | 9.4 | +1.80 |
| CTRL_trend_rs / chop | 109 |   53.2% | +0.00 | +0.29 | 2.9 | +0.05 | 6 | 6.5 | +1.01 |
| CTRL_trend_rs / trend | 907 |   56.9% | +0.04 | +38.25 | 3.0 | +2.50 | 9 | 21.0 | +1.24 |

### 5.1 Sensitivity of the split to the chop threshold

| threshold (range/dist) | variant | chop regime | trend regime |
|---|---|---|---|
| <3.5%/2.0% | MR_rsi2 | n=119 expR=+0.17 | n=656 expR=+0.11 |
| <3.5%/2.0% | MR_atr_dip | n=23 expR=+0.22 | n=154 expR=+0.20 |
| <3.5%/2.0% | PULLBACK_weekly | n=1 expR=+1.47 | n=75 expR=+0.12 |
| <3.5%/2.0% | ORB_daily_PROXY | n=27 expR=-0.31 | n=300 expR=-0.04 |
| <4.0%/2.0% | MR_rsi2 | n=161 expR=+0.15 | n=614 expR=+0.11 |
| <4.0%/2.0% | MR_atr_dip | n=29 expR=+0.24 | n=148 expR=+0.20 |
| <4.0%/2.0% | PULLBACK_weekly | n=4 expR=-0.39 | n=72 expR=+0.16 |
| <4.0%/2.0% | ORB_daily_PROXY | n=47 expR=-0.18 | n=280 expR=-0.04 |
| <4.5%/2.0% | MR_rsi2 | n=204 expR=+0.11 | n=571 expR=+0.12 |
| <4.5%/2.0% | MR_atr_dip | n=32 expR=+0.26 | n=145 expR=+0.19 |
| <4.5%/2.0% | PULLBACK_weekly | n=6 expR=-0.18 | n=70 expR=+0.16 |
| <4.5%/2.0% | ORB_daily_PROXY | n=75 expR=+0.00 | n=252 expR=-0.08 |
| <5.0%/2.5% | MR_rsi2 | n=251 expR=+0.10 | n=524 expR=+0.12 |
| <5.0%/2.5% | MR_atr_dip | n=44 expR=+0.20 | n=133 expR=+0.21 |
| <5.0%/2.5% | PULLBACK_weekly | n=8 expR=+0.23 | n=68 expR=+0.12 |
| <5.0%/2.5% | ORB_daily_PROXY | n=111 expR=+0.07 | n=216 expR=-0.13 |

## 6. Walk-forward honesty check (split at 2022-11-08)

| variant | first half | n | avgR | totR | t | second half | n | avgR | totR | t |
|---|---|---|---|---|---|---|---|---|---|---|
| ORB_5min | <2026-07-30 | 25 | +0.16 | +3.90 | +0.66 | >=2026-07-30 | 34 | -0.07 | -2.32 | -0.33 |
| ORB_daily_PROXY | <2022-11-08 | 162 | +0.02 | +3.73 | +0.19 | >=2022-11-08 | 167 | -0.14 | -22.56 | -1.04 |
| PULLBACK_weekly | <2022-11-08 | 45 | +0.02 | +0.98 | +0.12 | >=2022-11-08 | 31 | +0.30 | +9.16 | +1.21 |
| MR_rsi2 | <2022-11-08 | 298 | +0.05 | +14.93 | +1.46 | >=2022-11-08 | 477 | +0.16 | +75.26 | +7.73 |
| MR_atr_dip | <2022-11-08 | 86 | +0.09 | +8.08 | +1.07 | >=2022-11-08 | 91 | +0.31 | +28.41 | +4.28 |
| CTRL_trend_rs | <2022-11-08 | 421 | -0.03 | -13.53 | -1.23 | >=2022-11-08 | 595 | +0.09 | +52.08 | +4.32 |

A single split is a weak walk-forward: it is one draw of many possible splits. Read it as 'does the sign survive on unseen data', not as a validated edge.


## 7. Multiple comparisons, uncertainty, and the beta control test

`totR/1%expo` in §3 normalises total R by exposure (R earned per 1% of bars with a position open); it is the fair way to compare a low-exposure MR sleeve with a high-exposure benchmark.


### 7.1 MR vs the beta control - the decisive test

The MR variants' raw positive expectancy is not evidence of a mean-reversion edge: long US equity ETFs in an uptrend make money under almost any entry rule. The claim only survives if MR beats `CTRL_long_beta` on the same universe with the same stop, fees and slippage.

| variant | n | avgR | n ctrl | ctrl avgR | difference | Welch t | 95% CI of difference | CI > 0? |
|---|---|---|---|---|---|---|---|---|
| MR_rsi2 | 775 | +0.12 | 2445 | +0.04 | +0.08 | +3.82 | [+0.04, +0.12] | yes |
| MR_atr_dip | 177 | +0.21 | 2445 | +0.04 | +0.17 | +2.93 | [+0.06, +0.28] | yes |

If the difference CI straddles 0, the mean-reversion timing adds nothing measurable beyond 'be long in an uptrend' on this data.


### 7.2 Selection risk and per-variant uncertainty

Five rule variants were evaluated (not one). Under the null of zero edge, the best of five noisy variants looks good by selection alone; with 5 tests a Bonferroni-corrected two-sided 5% threshold is roughly |t| > 2.58. Best variant here: **MR_atr_dip (19 syms)** avgR=+0.206 t=3.60 (n=177; selected on the highest avgR - note MR_rsi2 carries the higher t at 6.35 on 4x the trades, so 'best' here is a judgement, not a significance result).

| variant | n | avgR | t | 95% CI of mean R (bootstrap) | CI > 0? |
|---|---|---|---|---|---|
| ORB_5min | 59 | +0.03 | +0.17 | [-0.27, +0.31] | no |
| ORB_daily_PROXY | 329 | -0.06 | -0.64 | [-0.23, +0.12] | no |
| PULLBACK_weekly | 76 | +0.13 | +0.89 | [-0.17, +0.42] | no |
| MR_rsi2 | 775 | +0.12 | +6.35 | [+0.08, +0.15] | yes |
| MR_atr_dip | 177 | +0.21 | +3.60 | [+0.09, +0.31] | yes |
| CTRL_trend_rs | 1016 | +0.04 | +2.35 | [+0.01, +0.07] | yes |

Bootstrap CIs assume trades are independent and identically distributed - they ignore regime clustering, symbol correlation and the fact that the rules were chosen after seeing the live results. They are a floor on the uncertainty, not a ceiling.


## 8. MR variants per symbol (pooled tables hide size effects)

| symbol | rsi2 n | rsi2 avgR | rsi2 totR | dip n | dip avgR | dip totR |
|---|---|---|---|---|---|---|
| SPY | 51 | +0.25 | +12.74 | 12 | +0.71 | +8.51 |
| QQQ | 48 | +0.23 | +10.99 | 11 | +0.01 | +0.12 |
| IWM | 38 | +0.08 | +2.94 | 5 | +0.43 | +2.17 |
| DIA | 49 | +0.12 | +5.83 | 10 | -0.19 | -1.90 |
| XLE | 38 | +0.01 | +0.22 | 6 | +0.19 | +1.12 |
| XLF | 42 | +0.14 | +5.70 | 8 | +0.67 | +5.35 |
| XLK | 41 | +0.25 | +10.24 | 11 | +0.13 | +1.46 |
| XLV | 44 | +0.12 | +5.49 | 7 | -0.18 | -1.28 |
| XLI | 44 | +0.06 | +2.45 | 6 | +0.03 | +0.15 |
| XLY | 48 | +0.22 | +10.47 | 10 | +0.34 | +3.43 |
| XLP | 48 | +0.05 | +2.30 | 10 | -0.18 | -1.83 |
| XLU | 41 | +0.03 | +1.27 | 6 | +0.10 | +0.59 |
| XLB | 43 | -0.00 | -0.16 | 11 | +0.34 | +3.71 |
| SMH | 46 | +0.23 | +10.62 | 13 | +0.42 | +5.40 |
| GLD | 34 | +0.12 | +4.04 | 15 | +0.30 | +4.53 |
| SLV | 32 | -0.27 | -8.57 | 11 | -0.15 | -1.68 |
| TLT | 18 | +0.15 | +2.67 | 7 | +0.29 | +2.01 |
| IEF | 24 | +0.13 | +3.06 | 9 | +0.33 | +2.94 |
| HYG | 46 | +0.17 | +7.90 | 9 | +0.19 | +1.69 |

## 9. Benchmark context

| window | dates | sessions | SPY open->close | from running high |
|---|---|---|---|---|
| full window | 2019-01-02 -> 2026-09-24 | 1943 | +249.53% | -1.1% |
| first half | 2019-01-02 -> 2022-11-08 | 972 | +65.23% | -19.1% |
| second half | 2022-11-08 -> 2026-09-24 | 972 | +112.03% | -1.1% |
| Sep 1-17 2026 | 2026-09-01 -> 2026-09-17 | 12 | +0.08% | -1.4% |

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
| 0.0 | CTRL_long_beta | 2445 | +0.043 |
| 1.0 | MR_rsi2 | 775 | +0.116 |
| 1.0 | MR_atr_dip | 177 | +0.206 |
| 1.0 | CTRL_long_beta | 2445 | +0.036 |
| 2.0 | MR_rsi2 | 775 | +0.109 |
| 2.0 | MR_atr_dip | 177 | +0.198 |
| 2.0 | CTRL_long_beta | 2445 | +0.029 |
| 5.0 | MR_rsi2 | 775 | +0.087 |
| 5.0 | MR_atr_dip | 177 | +0.176 |
| 5.0 | CTRL_long_beta | 2445 | +0.008 |

Read the losing point: if the mean R of an MR variant crosses the control's under a plausible (not extreme) slippage assumption, the claimed edge is a cost assumption, not a finding.


## 12. The recurring entry gate, tested

The proposal queue has asked, every night for two weeks, to gate long entries on 'own-instrument trend + non-negative 5d relative strength vs SPY (when breadth is narrow)'. The live YOLO entry is an LLM decision, so there is no rule to replay — but the gate itself is testable on the beta control, which shares the universe, the 2.5x ATR stop, the 3-bar hold and the cost model. `CTRL_trend_rs` is `CTRL_long_beta` plus: close > SMA20 **and** 5-session return >= SPY's 5-session return.

| variant | n | win% | avgR | totR | totR/1%expo | hold | t | maxCL | maxDD_R | %bars |
|---|---|---|---|---|---|---|---|---|---|---|
| CTRL_long_beta (control, no gate) | 2445 |   56.1% | +0.04 | +87.82 | +4.5 | 3.0 | +3.46 | 10 | 31.1 |   19.6% |
| CTRL_trend_rs (control + trend/RS gate) | 1016 |   56.5% | +0.04 | +38.54 | +4.7 | 2.9 | +2.35 | 10 | 24.4 |    8.1% |

Control avgR = **+0.036** (n=2445) vs gated **+0.038** (n=1016); difference **+0.002R** per trade, Welch t = +0.10, 95% CI of the difference [-0.035, +0.040] -> **CI straddles 0**.

Verdict: no measurable gain: the gate cuts trade count without improving expectancy, so it is NOT evidence-backed yet. The gate is applied unconditionally here (not only in narrow-breadth tapes), so this is the gate's AVERAGE value, not its conditional value; a conditional version would need a breadth series and is not tested. Gate kept 1016 of the control's 2445 trades (41.6% of signals), at 8.1% of bars in market vs 19.6%.

