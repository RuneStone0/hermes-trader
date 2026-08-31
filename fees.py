"""Alpaca fee model (regulatory pass-throughs) — modelled explicitly so paper
P/L reads like a real live account. Alpaca paper trading does NOT simulate fees.

Rates are named constants; update them in one place when Alpaca's fee schedule
changes (the SEC section-31 rate is reset periodically; CAT is new for 2026).

Reference: https://alpaca.markets/disclosures  (brokerage fee schedule)
"""
from __future__ import annotations

import math

# --- Regulatory fees (equities / ETFs) ---
SEC_FEE_PER_MILLION = 27.80        # SEC §31: dollars per $1,000,000 of SALE principal
TAF_PER_SHARE = 0.000166           # FINRA Trading Activity Fee, per share SOLD
TAF_CAP_PER_TRADE = 8.30           # FINRA TAF per-trade cap
CAT_PER_SHARE = 0.000015           # Consolidated Audit Trail (2026), per share, buys AND sells
# NOTE: confirm CAT rate against the current schedule — it is volume-based and new.

# --- Options fees (per contract) ---
OPTION_ORF_PER_CONTRACT = 0.03     # Options Regulatory Fee (buys + sells)
OPTION_OCC_PER_CONTRACT = 0.02     # OCC clearing fee (buys + sells)
OPTION_CAT_PER_CONTRACT = 0.001    # CAT for options (approx., per contract)


def round_up_penny(x: float) -> float:
    """Regulatory fees round UP to the nearest cent (per Alpaca)."""
    if x <= 0:
        return 0.0
    return math.ceil(x * 100) / 100.0


def equity_fees(shares: float, sell_price: float | None,
                buy_price: float | None, sold: bool) -> float:
    """Estimated regulatory fees for an equity/ETF leg (or round trip).

    SEC + TAF apply to SELLS only; CAT applies to both buys and sells.
    Returns total fee in dollars.
    """
    total = 0.0
    if sold and sell_price is not None:
        principal = shares * sell_price
        total += (principal / 1_000_000.0) * SEC_FEE_PER_MILLION   # SEC §31
        total += min(shares * TAF_PER_SHARE, TAF_CAP_PER_TRADE)    # FINRA TAF (capped)
        total += shares * CAT_PER_SHARE                            # CAT (sell)
    if buy_price is not None:
        total += shares * CAT_PER_SHARE                            # CAT (buy)
    return round_up_penny(total)


def option_fees(contracts: int, sides: int = 2) -> float:
    """Estimated regulatory fees for an options trade (sides = open + close legs)."""
    total = contracts * (OPTION_ORF_PER_CONTRACT + OPTION_OCC_PER_CONTRACT
                         + OPTION_CAT_PER_CONTRACT) * sides
    return round_up_penny(total)


def round_trip_equity_fees(shares: float, buy_price: float, sell_price: float) -> float:
    """Full round-trip equity fee (buy at buy_price, sell at sell_price)."""
    return equity_fees(shares, sell_price=sell_price, buy_price=buy_price, sold=True)


def fee_adjusted_rr(entry: float, target: float, stop: float, side: str,
                    shares: float) -> dict:
    """Compute the realized reward:risk ratio after modelled fees.

    Gross R:R is (target-entry)/(entry-stop). Net subtracts round-trip fees from
    the reward and adds them to the risk. Returns a dict with gross_rr, net_rr,
    est_fees, and whether the trade clears MIN_NET_RR.
    """
    import config

    if side == "long":
        risk_per_share = entry - stop
        reward_per_share = target - entry
    else:
        risk_per_share = stop - entry
        reward_per_share = entry - target

    if risk_per_share <= 0:
        return {"gross_rr": 0.0, "net_rr": 0.0, "est_fees": 0.0, "clears": False}

    gross_rr = reward_per_share / risk_per_share
    fees = round_trip_equity_fees(shares, buy_price=entry, sell_price=target)
    fees_per_share = fees / shares
    net_rr = (reward_per_share - fees_per_share) / (risk_per_share + fees_per_share)
    return {
        "gross_rr": round(gross_rr, 3),
        "net_rr": round(net_rr, 3),
        "est_fees": round(fees, 4),
        "clears": net_rr >= config.MIN_NET_RR,
    }
