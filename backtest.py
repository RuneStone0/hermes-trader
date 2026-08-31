"""Backtest the daily Opening Range Breakout (ORB) rule on SPY.

Validates whether the underlying SETUP has positive expectancy — before the LLM
decision layer filters individual trades. The LLM can only SKIP trades, so this
backtest is an upper bound on trade count and a sanity check on the edge.

Run:  python3 backtest.py [--days 120]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import fees
from alpaca_rest import AlpacaClient

ET = ZoneInfo("America/New_York")


def _et_hm(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    return dt.strftime("%H:%M")


def fetch_5min_bars(client: AlpacaClient, days: int) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    # Date-only format — avoids timezone-offset mangling in the query string.
    # NOTE: no `end` param — the free IEX tier 403s on `end` (routes to paid SIP).
    start_s = start.strftime("%Y-%m-%d")
    bars: list[dict] = []
    page_token = None
    while True:
        params = ["timeframe=5Min", "limit=10000", "adjustment=split",
                  f"start={start_s}"]
        if page_token:
            params.append(f"page_token={page_token}")
        resp = client._request("GET", f"/v2/stocks/SPY/bars?{'&'.join(params)}",
                               base=client.data_base_url)
        batch = resp.get("bars") or []
        bars.extend(batch)
        page_token = resp.get("next_page_token")
        if not page_token or not batch:
            break
    bars.sort(key=lambda b: b["t"])
    return bars


def group_by_et_date(bars: list[dict]) -> dict[str, list[dict]]:
    days: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        dt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        days[dt.date().isoformat()].append(b)
    return days


def simulate_day(day_bars: list[dict], params: dict) -> dict | None:
    orb = [b for b in day_bars if _et_hm(b["t"]) <= "10:00"]
    post = [b for b in day_bars if _et_hm(b["t"]) > "10:00"]
    if not orb or not post:
        return None

    orb_high = max(b["h"] for b in orb)
    orb_low = min(b["l"] for b in orb)
    rng = orb_high - orb_low
    if rng <= 0:
        return None

    entry = side = None
    entry_idx = None
    for i, b in enumerate(post):
        if b["c"] > orb_high:
            entry, side, entry_idx = orb_high, "long", i
            break
        if b["c"] < orb_low:
            entry, side, entry_idx = orb_low, "short", i
            break
    if entry is None:
        return None

    stop = orb_low if side == "long" else orb_high
    target = entry + 2 * rng if side == "long" else entry - 2 * rng

    exit_price = None
    exit_reason = None
    for b in post[entry_idx:]:
        if side == "long":
            if b["l"] <= stop:
                exit_price, exit_reason = stop, "stop"
                break
            if b["h"] >= target:
                exit_price, exit_reason = target, "target"
                break
        else:
            if b["h"] >= stop:
                exit_price, exit_reason = stop, "stop"
                break
            if b["l"] <= target:
                exit_price, exit_reason = target, "target"
                break
    if exit_price is None:
        exit_price = post[-1]["c"]
        exit_reason = "flat"

    # position sizing: risk 1% of equity over the range, whole shares
    equity = 10000.0
    risk_dollars = equity * config.RISK_PCT_PER_TRADE
    shares = max(1, int(risk_dollars / rng))

    pnl = (exit_price - entry) * shares if side == "long" else (entry - exit_price) * shares
    f = fees.round_trip_equity_fees(shares, buy_price=entry, sell_price=exit_price)
    net = pnl - f

    return {"side": side, "entry": entry, "exit": exit_price, "reason": exit_reason,
            "range": rng, "shares": shares, "gross": pnl, "fees": f, "net": net,
            "r": pnl / (rng * shares)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()

    client = AlpacaClient("daily")
    bars = fetch_5min_bars(client, args.days)
    days = group_by_et_date(bars)
    print(f"Fetched {len(bars)} 5-min bars across {len(days)} trading days "
          f"({args.days} calendar days lookback)")

    trades = [t for d in sorted(days) if (t := simulate_day(days[d], config.DAILY))]

    if not trades:
        print("No trades simulated (insufficient data?).")
        return

    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross = sum(t["gross"] for t in trades)
    net = sum(t["net"] for t in trades)
    fees_tot = sum(t["fees"] for t in trades)
    win_rate = len(wins) / len(trades)
    avg_win = sum(t["net"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["net"] for t in losses) / len(losses) if losses else 0.0
    expectancy = net / len(trades)
    profit_factor = (sum(t["net"] for t in wins) / abs(sum(t["net"] for t in losses))
                     if losses and sum(t["net"] for t in losses) != 0 else float("inf"))
    n_stop = sum(1 for t in trades if t["reason"] == "stop")
    n_target = sum(1 for t in trades if t["reason"] == "target")
    n_flat = sum(1 for t in trades if t["reason"] == "flat")

    print(f"\n=== Daily ORB backtest (SPY, {len(trades)} trades over {len(days)} days) ===")
    print(f"Win rate:          {win_rate:.1%}")
    print(f"Avg win:           ${avg_win:>8.2f}")
    print(f"Avg loss:          ${avg_loss:>8.2f}")
    print(f"Expectancy/trade:  ${expectancy:>8.2f}")
    print(f"Profit factor:     {profit_factor:>8.2f}")
    print(f"Gross P/L:         ${gross:>8.2f}")
    print(f"Total fees:        ${fees_tot:>8.2f}")
    print(f"NET P/L:           ${net:>8.2f}   (on $10k equity, {args.days}d)")
    print(f"Exits: stop={n_stop}  target={n_target}  flat={n_flat}")
    print(f"Avg R multiple:    {sum(t['r'] for t in trades)/len(trades):.2f}")


if __name__ == "__main__":
    main()
