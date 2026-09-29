#!/usr/bin/env python3
"""Read-only probe: identify the Alpaca paper accounts behind the configured key pairs.
Prints account numbers/equity only -- never any secret material."""
import json, pathlib, urllib.request

KEYS = pathlib.Path("/opt/data/profiles/trader/.alpaca_keys.env")
kv = {}
for line in KEYS.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip()

for bot in ("DAILY", "WEEKLY", "YOLO"):
    key, sec = kv.get(f"ALPACA_{bot}_API_KEY", ""), kv.get(f"ALPACA_{bot}_SECRET_KEY", "")
    base = kv.get(f"ALPACA_{bot}_BASE_URL", "https://paper-api.alpaca.markets/v2")
    if not key:
        print(f"{bot:7s} MISSING KEY")
        continue
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    try:
        req = urllib.request.Request(base.rstrip("/") + "/account", headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        print(f"{bot:7s} acct={d.get('account_number')} status={d.get('status')} "
              f"equity={d.get('equity')} cash={d.get('cash')} "
              f"shorting={d.get('shorting_enabled')} options_lvl={d.get('options_approved_level')} "
              f"crypto={d.get('crypto_status')} pdt={d.get('pattern_day_trader')}")
    except Exception as e:
        print(f"{bot:7s} ERROR {type(e).__name__}: {e}")
    # Is there an account-list endpoint reachable with a retail paper key?
    try:
        req = urllib.request.Request(base.rstrip("/") + "/accounts", headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        n = len(d) if isinstance(d, list) else "?"
        print(f"{bot:7s} /accounts -> {n} entries")
    except Exception as e:
        print(f"{bot:7s} /accounts -> {type(e).__name__} {e}")
