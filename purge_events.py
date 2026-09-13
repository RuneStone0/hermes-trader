"""Archive-and-purge journal events (ops utility).

Exports the selected events to a timestamped JSON file, THEN deletes them from
the live journal. Always keep the export — it is the audit record.

    python3 purge_events.py                    # export + delete 'error' events
    python3 purge_events.py --keep             # export only (no delete)
    python3 purge_events.py --decision error,warn

The export lands in --outdir (default /data inside the container). To keep an
off-host, versioned copy, copy it into archive/events/ and commit it — the
container state volume is NOT backed up.
"""
from __future__ import annotations

import argparse
import datetime
import json

import config  # noqa: F401  (resolves DB_PATH / STATE_DIR before db is used)
import db


def main() -> None:
    ap = argparse.ArgumentParser(description="Archive then purge journal events.")
    ap.add_argument("--decision", default="error",
                    help="comma-separated decisions to purge (default: error)")
    ap.add_argument("--outdir", default="/data", help="where to write the export")
    ap.add_argument("--keep", action="store_true",
                    help="export only; do not delete anything")
    args = ap.parse_args()

    decisions = [d.strip() for d in args.decision.split(",") if d.strip()]
    if not decisions:
        raise SystemExit("no decisions given")

    db.init_db()
    conn = db.connect()
    try:
        ph = ",".join("?" * len(decisions))
        rows = conn.execute(
            f"SELECT * FROM events WHERE decision IN ({ph}) ORDER BY id", decisions
        ).fetchall()
        data = [dict(r) for r in rows]

        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"{args.outdir.rstrip('/')}/purged_events_{stamp}.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"exported {len(data)} event(s) -> {path}")

        if args.keep:
            print("--keep: nothing deleted")
        else:
            conn.execute(f"DELETE FROM events WHERE decision IN ({ph})", decisions)
            conn.commit()
            print(f"deleted {len(data)} event(s) from the journal")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
