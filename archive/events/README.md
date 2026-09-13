# Archived journal events

Snapshots of decision-journal events that were exported and then purged from the
live `events` table. Produced with [`purge_events.py`](../../purge_events.py).

The live journal sits in the container's state volume (`trader_trader-data`),
which is **not backed up** — these files are the off-host, versioned audit record.
They're kept so the "why" behind a bot's decisions survives even after the journal
has been tidied.

| File | Events | Why they were purged |
|---|---|---|
| `purged_error_events_20260913T031208Z.json` | 18 | All already resolved: transient upstream blips (Alpaca `5xx`, DNS name-resolution failures, LLM read-timeouts) plus two real bugs fixed by the `trader-autofix` cron (403 bracket-legs-held close, 422 stop geometry). Removed so the journal reads clean — the fixes themselves live in git history. |

## Purging more

```bash
python3 purge_events.py --keep       # export first, review the file
python3 purge_events.py              # export + delete from the journal
```

Then copy the new `purged_events_<stamp>.json` into this directory and commit it.
