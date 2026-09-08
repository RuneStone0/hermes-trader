"""Thin, dependency-free REST client for the Alpaca Trading API.

Uses only the Python standard library (urllib + json) so the deterministic
strategies run anywhere with `python3` and no pip installs. Every call is
synchronous and short-lived — ideal for cron invocations.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import config


class AlpacaError(Exception):
    pass


def _round2(x: float) -> str:
    return f"{x:.2f}"


class AlpacaClient:
    def __init__(self, account: str):
        acct = config.ACCOUNTS[account]
        self.account_name = account
        # Normalize: accept base URLs with or without the trailing "/v2".
        base = acct["base_url"].rstrip("/")
        if base.endswith("/v2"):
            base = base[:-3]
        self.base_url = base
        self.data_base_url = config.DATA_BASE_URL.rstrip("/")
        self.key = acct["api_key"]
        self.secret = acct["secret_key"]
        if not self.key or not self.secret:
            raise AlpacaError(f"Missing API key/secret for account '{account}'")

    # -- low-level ---------------------------------------------------------- #
    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int = 30, base: str | None = None):
        url = (base or self.base_url) + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("APCA-API-KEY-ID", self.key)
        req.add_header("APCA-API-SECRET-KEY", self.secret)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise AlpacaError(f"HTTP {e.code} {method} {path}: {detail}") from e
        except urllib.error.URLError as e:
            raise AlpacaError(f"URLError {method} {path}: {e.reason}") from e

    # -- account ------------------------------------------------------------ #
    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    # -- positions ---------------------------------------------------------- #
    def positions(self) -> list:
        return self._request("GET", "/v2/positions")

    def position(self, symbol: str) -> dict:
        return self._request("GET", f"/v2/positions/{symbol}")

    def close_position(self, symbol: str) -> dict:
        return self._request("DELETE", f"/v2/positions/{symbol}")

    def close_all(self) -> list:
        return self._request("DELETE", "/v2/positions")

    # -- orders ------------------------------------------------------------- #
    def orders(self, status: str = "open") -> list:
        return self._request("GET", f"/v2/orders?status={status}")

    def cancel_all(self) -> list:
        return self._request("DELETE", "/v2/orders")

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/v2/orders/{order_id}")

    def submit_order(self, order: dict) -> dict:
        return self._request("POST", "/v2/orders", body=order)

    def order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/orders/{order_id}")

    def orders_all(self, limit: int = 100) -> list:
        """Recent orders across ALL statuses. NOTE: ?status=open silently omits
        'held' bracket legs — never use it for coverage/protection scans."""
        return self._request("GET", f"/v2/orders?status=all&limit={int(limit)}&direction=desc")

    def bracket_order(self, symbol: str, qty: float, side: str,
                      stop_price: float, target_price: float,
                      entry_price: float | None = None,
                      entry_type: str = "market",
                      time_in_force: str = "day") -> dict:
        """Submit a bracket order: entry + take-profit + stop-loss (2:1 R/R).

        side is 'buy' (long) or 'sell' (short). Alpaca infers the opposite side
        for the take-profit/stop-loss legs from the entry side.
        """
        order: dict = {
            "symbol": symbol,
            "qty": str(int(qty)) if float(qty).is_integer() else str(qty),
            "side": side,
            "type": entry_type,
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "take_profit": {"limit_price": _round2(target_price)},
            "stop_loss": {"stop_price": _round2(stop_price)},
        }
        if entry_type == "limit":
            order["limit_price"] = _round2(entry_price or 0.0)
        return self.submit_order(order)

    # -- market data -------------------------------------------------------- #
    def latest_trade(self, symbol: str) -> dict | None:
        """Live last-trade for a symbol (data host, free tier OK).

        Returns {'price': float, 'time': str} or None (unknown symbol / feed
        gap). This is the price Alpaca brackets are validated against, so it —
        NOT the last daily close — is the correct execution reference."""
        try:
            r = self._request("GET", f"/v2/stocks/{symbol}/trades/latest",
                              base=self.data_base_url)
            t = (r or {}).get("trade") or {}
            px = t.get("p")
            if px is None:
                return None
            return {"price": float(px), "time": t.get("t")}
        except AlpacaError:
            return None

    def bars(self, symbol: str, timeframe: str = "1Day", limit: int = 100,
             start: str | None = None, end: str | None = None,
             adjustment: str = "all") -> list:
        # The free IEX data tier returns an empty first page (bars: null) for
        # limit-only requests — back-compute a start date far enough back to
        # cover `limit` bars when neither start nor end is given.
        if start is None and end is None and limit:
            bars_per_day = {
                "1Min": 390, "5Min": 78, "15Min": 26, "30Min": 13, "1Hour": 7,
                "1Day": 1,
            }.get(timeframe, 1)
            trading_days = (limit + bars_per_day - 1) // bars_per_day
            calendar_days = int(trading_days * 1.7) + 3  # weekends/holidays + margin
            start = (datetime.now(timezone.utc) - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
            # Second free-tier quirk: with `start` set, the API returns bars in
            # ascending order and truncates to the FIRST `limit` bars of the
            # window — so a window holding more bars than `limit` silently drops
            # the MOST RECENT data (indicators then run on stale closes). The
            # margin above makes the window ~1.2x `limit`, so over-request until
            # the whole window fits, then slice the tail back to `limit` below.
            req_limit = min(
                10000,
                int(calendar_days * 0.72 * bars_per_day) + bars_per_day + 5,
            )
        else:
            req_limit = limit

        params = [f"timeframe={timeframe}", f"limit={req_limit}", f"adjustment={adjustment}"]
        if start:
            params.append(f"start={start}")
        if end:
            params.append(f"end={end}")
        qs = "&".join(params)
        data = self._request("GET", f"/v2/stocks/{symbol}/bars?{qs}",
                             base=self.data_base_url)
        if req_limit > limit and data.get("bars"):
            data["bars"] = data["bars"][-limit:]  # keep the newest `limit`
        return data

    def clock(self) -> dict:
        return self._request("GET", "/v2/clock")
