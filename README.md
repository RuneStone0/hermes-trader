# Trading System — README

An Alpaca paper-trading bot system running on Hermes (profile: `trader`).
Three independent traders, each on its own Alpaca **paper** account.

## Accounts (all paper, $10k equity each)
| Trader | Account key | Strategy | Brain |
|---|---|---|---|
| Daily | `daily` | SPY opening-range breakout | deterministic rules + LLM gate |
| Weekly | `weekly` | SPY trend-following pullback | deterministic rules + LLM gate |
| YOLO | `yolo` | full autonomy (retail assets) | LLM agent via Alpaca MCP |

## Hard rules (all traders)
- Retail-accessible instruments only (US equities, ETFs, standard single-leg equity options).
- **No crypto except BTC/USD.**
- Stop-loss on every position; ~2:1 reward:risk target; ~1% equity risk per trade.
- Paper trading only for now.

## Layout (`/opt/data/profiles/trader/trading/`)
- `config.py` — keys, risk params, strategy knobs, fee constants, LLM config.
- `alpaca_rest.py` — dependency-free REST client (Trading + Market Data hosts).
- `fees.py` — SEC §31 / FINRA TAF / CAT / options fee model (paper doesn't charge these).
- `db.py` — SQLite trade store (`data/trades.db`).
- `indicators.py` — ATR / SMA / pct_change.
- `advisor.py` — one-call LLM gate (GO / NO-GO / SIZE-DOWN).
- `daily_run.py`, `weekly_run.py` — the two rule-based traders.
- `reconcile.py` — closes trades from Alpaca FILL activities (real P/L).
- `dashboard.py` — dark HTML dashboard (`dashboard/index.html`).
- `backtest.py` — historical expectancy check for the daily ORB rules.
- `lessons_learned.md`, `strategy_proposals.md` — self-improvement artifacts.

## Cron jobs (scheduler runs inside the trader gateway)
| Job | Schedule (UTC) | Mode |
|---|---|---|
| daily-trader | `*/5 13-20 * * 1-5` | no-agent script |
| weekly-trader | `45 14 * * 1-5` | no-agent script |
| reconcile | `*/10 * * * *` | no-agent script |
| dashboard | `*/15 * * * *` | no-agent script |
| yolo-trader | `0 14,17,19 * * 1-5` | LLM agent (MCP) |
| self-improvement | `30 21 * * 1-5` | LLM agent |

## Running / managing
```bash
export HERMES_HOME=/opt/data/profiles/trader
hermes cron list            # jobs
hermes cron run <id>        # trigger one
hermes cron pause <id>      # pause
hermes gateway status       # gateway/scheduler health
```
Scripts: `python3 trading/daily_run.py --dry-run` (no orders) to preview logic.
Dashboard: `trading/dashboard/index.html` (also opened in the preview pane).

## Going live (later — do NOT rush)
1. Build ≥N trades of positive expectancy + contained drawdown on paper.
2. Confirm fee constants against `alpaca.markets/disclosures`.
3. Swap paper keys → live keys, set `ALPACA_PAPER_TRADE=false`, add a real delivery channel.

## Notes
- Free IEX data: intraday bars work with `start` (no `end` param). Full SIP/options/news need a paid data plan.
- LLM decisions gate every daily/weekly trade; YOLO is fully agentic.
- Secrets live in `.env` (DeepSeek) and `.alpaca_keys.env` (Alpaca), never in config.yaml.
