# Trading System — README

An Alpaca paper-trading bot system running on Hermes (profile: `trader`).
Three independent traders, each on its own Alpaca **paper** account.

## Accounts (all paper, $10k equity each)
| Trader | Account key | Strategy | Brain |
|---|---|---|---|
| Daily | `daily` | SPY opening-range breakout | deterministic rules + LLM gate |
| Weekly | `weekly` | SPY trend-following pullback | deterministic rules + LLM gate |
| YOLO | `yolo` | full autonomy (retail assets) | LLM agent via Alpaca MCP |
| Copy | `copy` | mirrors positions posted by @fullportnik on X | LLM read of each post |

The Copy follower is the only one whose **signals come from outside the market
data**: it watches an X account that posts trade positions and mirrors them.
It is in **shadow mode** until an Alpaca account is supplied for it (see below).

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
- `xfeed.py` — the X post feed (xAI `x_search`; official X API as a drop-in).
- `x_copy_run.py` — the copy follower: reads his posts, mirrors the positions.
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
| x-post-watch | `*/5 * * * *` | no-agent script (`scripts/x_signal_watch.sh`) |

## The Copy follower (`copy`)

Watches **@fullportnik** on X and mirrors the positions he posts. He replies
constantly and publishes occasionally, so **replies are ignored**: only original
posts are read. He had posted no trades at the time this was built, so the bot
was built to be watching before he starts.

How it works:

1. `xfeed.py` asks the xAI `x_search` tool a **narrow** question — *the newest
   original post* — then walks backwards one post at a time until it reaches a
   post it already holds. A wide "list his last N posts" question was measured
   at 2–6 of 6 posts across identical runs (`probes/probe_feed_recall.py`) and
   is not used: for a copier, a missed post is a missed trade. Everything an
   index silently drops is caught by a bounded **sweep** every 6 hours.
2. Each new post is read by the LLM and classified. Anything that is not a
   stated position (commentary, market takes, profit screenshots, a post whose
   trade is only visible in an image) is journalled with a plain-English reason
   and dropped.
3. A stated position is sized from **our** risk rules, never his conviction:
   size from the account's own risk budget, stop from the post when usable and
   from ATR otherwise, and **no stop derivable means no trade**. Entries wait
   for the next market open instead of chasing a price that has already moved.
   His targets are recorded for the journal but never become our exits.
4. A post can never fire twice (`x_posts` keyed by post id), so a feed that
   re-serves old posts is harmless.

**Shadow mode.** There is no fourth Alpaca paper account — a paper login caps at
3 and all 3 are in use — so until `ALPACA_COPY_*` keys appear in `.env` the bot
sizes the trade it *would* take and journals it as an order it did **not**
place, tracking its own book from the posts. It never invents a fill or a P&L
and holds no broker client at all. Dropping the two keys into `.env` makes it
live on the next poll, with no code change.

**Feeding it a key.** `XAI_API_KEY` in `.env` is what lets it see the timeline.
Without it the bot journals a read failure and the watchers alert — it never
reports "he posted nothing" when it is actually blind.

**Alerts.** The Hermes cron job `x-post-watch` reads the container's `/signals`
endpoint every 5 minutes and stays **silent unless there is something to say**:
a new post and what the bot did with it, or a feed that has stopped polling.
`python3 scripts/x_signal_watch.py --status` prints feed health on demand.

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
