# The X-copy follower — removed 2026-10-15 (why, and what was deleted)

Rune's decision, delivered 2026-10-15 via the politiupdate profile: the
copy-follower sleeve is **removed completely** — not paused, not disabled, not
re-scoped. This directory is what is left of it.

## Why it was removed (measured, not a preference)

All figures from xAI's own console plus the running container, 2026-10-15:

| What | Number |
|---|---|
| "X posts fetched" on the xAI bill (its own line) | **$21.33/week** at $0.005/post |
| Grok tokens for the same loop (3.4M prompt / 2.4M cached / 653.9K reasoning) | **$6.79/week** |
| Total | **~$122/month** |
| Poll cadence | 144x/day, 24/7 (deliberately not market-gated) |
| Billed fetches vs genuinely new posts | 4,300/week bought ~21 new = **0.49% useful** |
| Cost per captured post | **~$1.02** |
| Trades it ever placed | **0** (shadow mode — no Alpaca account was ever configured) |

The mechanism: the poll asked for "his newest original post" with **no date
bound**, so a quiet poll re-fetched (and re-paid for) a post it already held —
every 10 minutes, forever. `X_BEARER_TOKEN` was unset, so it never used the
official X API either (`feed_state.last_provider = xai/trigger`), and xAI was
already answering HTTP 429 `resource-exhausted` (2026-10-09, 10-14, 10-15).

## Exported before deletion

- `x_posts_export.json` — all 48 stored posts with their disposition
  (`state`, `kind`, `symbol`, the LLM's reason and full `analysis_json`).
- `feed_state_export.json` — the feed's bookkeeping keys at the last poll.
- `x_watch_state.json` — the Hermes-side watcher's seen-post cursor
  (`baselined` + 25 post ids). Note: the file itself was unlinked before it could
  be copied out, so this is the full snapshot recovered verbatim from the Hermes
  session store (a read of the file at 2026-10-15T17:25:46Z); `checked_at` is
  that read's timestamp, not the file's last write. Every id in it also appears
  in `x_posts_export.json` (checked).
- A full copy of the live SQLite DB as it stood before the tables were dropped:
  `umbrel@10.21.0.1:/home/umbrel/trader-backups/trades_pre-xcopy-removal-2026-10-15.db`.

## What was deleted

Code: `xfeed.py`, `x_copy_run.py`, `config.COPY` / `COPY_TUNE` / `X_FEED` (plus
the xAI-key plumbing that only it used), `wording.py`'s post/shadow/read lines,
`app.py`'s `x_feed` job and `/signals` endpoint, `db.py`'s `x_posts` +
`feed_state` tables and accessors, the copy page/card/tab in `dashboard.py`,
`monitor_service.py`'s feed-health block, the `copy` entry in `schedule.py`,
`selfheal.py`, `reconcile.py`, `self_improve.py` and `STARTING_CAPITAL`.

Cron / harness: the `x-post-watch` Hermes cron job and
`scripts/x_signal_watch.{sh,py}` that fed it.

Data: the `x_posts` and `feed_state` tables (dropped by `db._migrate` on the
next start) and `data/x_watch_state.json`.

Tests / probes: `tests/test_x_copy.py` and the ten probes that existed only for
this sleeve (`probe_feed_recall`, `probe_find_traders`, `probe_find_traders2`,
`probe_audit_accounts`, `probe_image_read`, `probe_narrow`, `probe_nitter`,
`probe_prev`, `probe_reclassify_live_posts`, `probe_xai_structured`).

## Deliberately left in place

- `trades.signal_post_id` — always NULL, nothing reads it. Dropping the column
  rewrites the whole `trades` table, which holds the live books of the three
  surviving bots; not worth the risk for a cosmetic cleanup.
- The historical `events` rows written by the sleeve (`strategy='x_copy'`) — they
  are the audit trail of what it saw and decided, and no page renders them now
  that the copy account is gone.
- The host's `.env`: the xAI key stays there untouched (other projects on this
  host share it). Nothing in this service reads it any more.
- `strategy_proposals_closed.md` #8/#9/#10 — historical measurements about a
  sleeve that no longer exists; entry #13 records the removal so the nightly
  routine does not re-propose any of it.

## If it is ever re-proposed

The falsifier recorded in `strategy_proposals_closed.md` #13: a captured post
whose TEXT named an instrument, plus positive expectancy over >= 30 closed
trades, at a measured cost per captured post under ~$0.05. Anything less is the
same $122/month for the same 0.5%.

Restoring the code is possible from git history (`git log -- x_copy_run.py`
before commit for this removal), but the cost measurements above are the reason
it was deleted, not a reason to bring it back.
