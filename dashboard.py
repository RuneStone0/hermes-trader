"""Generate the dark-theme HTML dashboard(s) from trades.db.

Two page types:
  - overview ("/"): all-accounts summary, clickable per-account cards, open
    positions, equity curve, and recent closed trades.
  - account ("/daily", "/weekly", "/yolo"): one account's stats, its open
    positions as a table (entry / stop / size incl. % of equity), equity curve,
    recent closed trades, and a collapsed decision journal.

Every position row — open or recently closed — carries a small info icon that
expands the trade's decision-log detail (LLM rationale, setup, regime, risk)
inline. The big decision journal is collapsed by default since the icons cover
the per-trade "why".

Net P/L = gross P/L minus modelled regulatory fees. Self-contained HTML
(inline CSS + inline JS + inline SVG, no CDN).
"""
from __future__ import annotations

import html
import json
import os
import urllib.parse
from datetime import datetime, timezone

import config
import db
import schedule

ACCOUNTS = ("daily", "weekly", "yolo")
_STRATEGY = {"daily": "daily_orb", "weekly": "weekly_pullback", "yolo": "yolo"}
_STRATEGY_LABEL = {"daily_orb": "Daily ORB", "weekly_pullback": "Weekly pullback",
                   "yolo": "YOLO", "selfheal": "Self-heal"}
_DECISION_COLORS = {"go": "#3fb950", "exit": "#58a6ff", "no_go": "#d29922",
                    "skip": "#8b949e", "error": "#f85149",
                    "fix": "#2ea043", "warn": "#d29922"}

# Symbol -> (full name, one-line description) for the hover / tap tooltip.
# Covers the watchlist + common traded instruments; anything else falls back to
# "Traded instrument". Kept server-side so the HTML stays self-contained (no CDN,
# no per-request API calls).
SYMBOL_INFO = {
    "SPY": ("SPDR S&P 500 ETF Trust", "Tracks the S&P 500 index — the broad US large-cap market."),
    "QQQ": ("Invesco QQQ Trust", "Tracks the Nasdaq-100 — the 100 largest non-financial Nasdaq stocks."),
    "IWM": ("iShares Russell 2000 ETF", "Tracks the Russell 2000 — US small-cap stocks."),
    "DIA": ("SPDR Dow Jones Industrial ETF", "Tracks the 30-stock Dow Jones Industrial Average."),
    "GLD": ("SPDR Gold Shares", "Tracks the spot price of gold bullion."),
    "SLV": ("iShares Silver Trust", "Tracks the spot price of silver bullion."),
    "TLT": ("iShares 20+ Year Treasury ETF", "Long-dated US Treasury bonds (20+ yr exposure)."),
    "IEF": ("iShares 7-10 Year Treasury ETF", "Intermediate US Treasury bonds (7-10 yr)."),
    "HYG": ("iShares iBoxx High Yield Corp ETF", "High-yield (junk) corporate bonds."),
    "XLF": ("Financial Select Sector SPDR", "S&P 500 financial sector (banks, insurance)."),
    "XLE": ("Energy Select Sector SPDR", "S&P 500 energy sector (oil & gas)."),
    "XLK": ("Technology Select Sector SPDR", "S&P 500 technology sector."),
    "XLV": ("Health Care Select Sector SPDR", "S&P 500 health care sector."),
    "XLI": ("Industrial Select Sector SPDR", "S&P 500 industrial sector."),
    "XLY": ("Consumer Discretionary SPDR", "S&P 500 consumer discretionary sector."),
    "XLP": ("Consumer Staples Select SPDR", "S&P 500 consumer staples sector."),
    "XLU": ("Utilities Select Sector SPDR", "S&P 500 utilities sector."),
    "XLB": ("Materials Select Sector SPDR", "S&P 500 materials sector."),
    "SMH": ("VanEck Semiconductor ETF", "Semiconductor industry exposure."),
    "AAPL": ("Apple Inc.", "Consumer electronics and services company."),
    "MSFT": ("Microsoft Corporation", "Software and cloud computing company."),
    "NVDA": ("NVIDIA Corporation", "Graphics processors and AI compute."),
    "AMZN": ("Amazon.com, Inc.", "E-commerce, cloud (AWS), and digital media."),
    "GOOGL": ("Alphabet Inc.", "Google search, ads, and cloud (Class A)."),
    "META": ("Meta Platforms, Inc.", "Facebook, Instagram, and WhatsApp."),
    "TSLA": ("Tesla, Inc.", "Electric vehicles and energy storage."),
}


def _sym_tag(symbol: str) -> str:
    """A symbol cell that reveals the instrument's full name + description on
    hover (desktop) or tap (mobile). Data attributes drive a single JS tooltip."""
    name, desc = SYMBOL_INFO.get(symbol, (f"{symbol}", "Traded instrument."))
    s = html.escape(symbol)
    n = html.escape(name)
    d = html.escape(desc)
    return (f"<span class='sx' tabindex='0' role='button' "
            f"data-name='{n}' data-desc='{d}'>{s}</span>")

# Trading candlestick favicon (dark card + 3 candles, dashboard palette),
# inlined as a data URI so the HTML stays fully self-contained.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#161b22'/>"
    "<rect x='15.5' y='12' width='3' height='28' fill='#3fb950'/>"
    "<rect x='10' y='22' width='14' height='10' rx='2' fill='#3fb950'/>"
    "<rect x='30.5' y='20' width='3' height='32' fill='#f85149'/>"
    "<rect x='25' y='30' width='14' height='14' rx='2' fill='#f85149'/>"
    "<rect x='45.5' y='8' width='3' height='28' fill='#3fb950'/>"
    "<rect x='40' y='18' width='14' height='10' rx='2' fill='#3fb950'/>"
    "</svg>"
)
_FAVICON = "data:image/svg+xml," + urllib.parse.quote(_FAVICON_SVG)

# Info-circle icon used as the per-position "why" button.
_INFO_ICON = (
    "<svg width='13' height='13' viewBox='0 0 16 16' fill='none' "
    "xmlns='http://www.w3.org/2000/svg'>"
    "<circle cx='8' cy='8' r='6.6' stroke='currentColor' stroke-width='1.4'/>"
    "<path d='M8 7.1v3.6' stroke='currentColor' stroke-width='1.4' stroke-linecap='round'/>"
    "<circle cx='8' cy='4.9' r='1.1' fill='currentColor'/></svg>"
)

_CSS = """
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2128;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--pos:#3fb950;--neg:#f85149;--accent:#58a6ff;--accent-dim:rgba(88,166,255,.14);--topbar-h:52px}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:0;min-height:100vh}
main{padding:18px 20px 40px;max-width:1100px;margin:0 auto}
.page-meta{color:var(--muted);font-size:12px;margin:2px 0 14px;display:flex;align-items:center;gap:7px;flex-wrap:wrap;line-height:1.4}
.page-meta strong{color:var(--text);font-weight:600}
.page-meta .strategy{font-weight:600;color:var(--text)}
h1{font-size:20px;font-weight:600}
h2{font-size:15px;margin:26px 0 12px;font-weight:600}
h3{font-size:13px;color:var(--accent);margin-bottom:6px}
a{color:inherit}
.muted{color:var(--muted)}
.big{font-size:22px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.card-title{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card-value{font-size:22px;font-weight:600;margin-top:4px}
.card-sub{color:var(--muted);font-size:12px;margin-top:2px}
.card-value .amt{font-size:14px;font-weight:500;color:var(--muted);margin-left:7px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.accts{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.acct{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;text-decoration:none;color:var(--text);display:block;transition:border-color .15s,transform .15s}
.acct:hover{border-color:var(--accent)}
.acct:active{transform:scale(.995)}
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:4px 0;margin-top:14px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.panel-sub{padding:13px 12px 7px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.panel-sub.sep{border-top:1px solid var(--border);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.04em;padding-top:11px;padding-bottom:11px}
td:first-child,th:first-child{padding-left:12px}
td:last-child,th:last-child{padding-right:12px}
tr:last-child td{border-bottom:none}.panel>table>thead>tr>th{border-bottom:1px solid var(--border)}
.curve{width:100%;height:auto;background:var(--card);border:1px solid var(--border);border-radius:10px;display:block}
.curve path{fill:none;stroke:var(--accent);stroke-width:2}
.curve .zero{stroke:var(--border);stroke-dasharray:4 4}
.footer{color:var(--muted);font-size:12px;margin-top:28px;padding-top:16px;border-top:1px solid var(--border);line-height:1.6}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--border);color:var(--muted)}
/* --- sticky top menu bar (title + nav, saves page space) --- */
.topbar{position:sticky;top:0;z-index:200;display:flex;align-items:center;gap:14px;height:var(--topbar-h);padding:0 20px;background:rgba(13,17,23,.86);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--border);max-width:1100px;margin:0 auto}
.brand{font-size:16px;font-weight:700;letter-spacing:-.01em;text-decoration:none;display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.brand .logo{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--accent),#1f6feb);display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#04141f}
.brand .paper{font-size:10px;font-weight:700;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:1px 7px;letter-spacing:.06em}
.menu{display:flex;gap:4px;margin-left:auto;overflow-x:auto;-webkit-overflow-scrolling:touch}
.tab{padding:7px 14px;border-radius:8px;color:var(--muted);text-decoration:none;font-size:13px;font-weight:500;white-space:nowrap;flex-shrink:0;transition:color .12s,background .12s}
.tab:hover{color:var(--text);background:var(--card2)}
.tab.active{background:var(--accent);color:#0d1117}
.tabs{display:flex;gap:6px;margin:18px 0 4px;flex-wrap:wrap}
/* --- symbol tooltip --- */
.sx{position:relative;border-bottom:1px dotted var(--muted);cursor:help;font-weight:600;padding:0 1px}
.sx:focus-visible{outline:2px solid var(--accent);border-radius:4px}
#tip{position:fixed;display:none;z-index:999;max-width:280px;background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:9px 11px;font-size:12px;line-height:1.4;box-shadow:0 10px 28px rgba(0,0,0,.6);pointer-events:none}
#tip b{display:block;font-size:12.5px;color:var(--text);margin-bottom:2px;white-space:normal}
#tip span{color:var(--muted)}
/* --- decision-log detail (expanded "Why" row) --- */
.dd-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px;font-size:14px}
.dd-head .sym{font-weight:700;font-size:15px;letter-spacing:.01em}
.dd-head .qty{font-size:12px}
.dd-badge{padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;border:1px solid var(--border);color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.dd-badge.long{color:var(--pos);border-color:var(--pos)}
.dd-badge.short{color:var(--neg);border-color:var(--neg)}
.dd-size{font-size:12px}
.dd-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px 18px;padding:12px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:0 0 12px}
.dd-k{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em}
.dd-v{font-size:13px;margin-top:2px}
.dd-v small{font-size:11px;font-weight:400}
.dd-narr{margin-top:4px}
.dd-sec{margin:12px 0}
.dd-sec.why{background:rgba(88,166,255,.07);border-left:3px solid var(--accent);border-radius:0 6px 6px 0;padding:9px 12px}
.dd-sec-label{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.dd-sec.why .dd-sec-label{color:var(--accent)}
.dd-text{font-size:13.5px;line-height:1.55}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.long{color:var(--pos)}.short{color:var(--neg)}
/* --- per-position "why" button + expanding detail row --- */
.dbtn{background:none;border:1px solid var(--border);border-radius:6px;color:var(--muted);cursor:pointer;padding:5px 7px;line-height:1;display:inline-flex;align-items:center;gap:4px;vertical-align:middle;transition:all .12s;min-width:26px;justify-content:center}
.dbtn:hover{color:var(--accent);border-color:var(--accent)}
.dbtn.on{color:var(--accent);border-color:var(--accent);background:var(--accent-dim)}
.drow td{padding:0;background:linear-gradient(180deg,rgba(88,166,255,.05),rgba(88,166,255,.02))}
.ddetail{padding:16px 18px 18px;font-size:13px;border-top:1px solid var(--border);border-left:3px solid var(--accent)}
.drow.hidden{display:none}
/* --- decision journal --- */
details.journal summary{cursor:pointer;user-select:none;display:inline-block;margin:26px 0 0;font-size:15px;font-weight:600}
details.journal summary:hover{color:var(--accent)}
details.journal .muted{font-weight:400;font-size:12px}
/* --- mobile --- */
@media (max-width:720px){
  main{padding:14px 12px 32px}
  .topbar{padding:0 12px;gap:10px}
  .brand .paper{display:none}
  .card-value{font-size:19px}
  .dbtn{padding:7px 9px;min-width:32px}
  th,td{padding:9px 8px;font-size:12.5px}
  #tip{max-width:78vw}
  .ddetail{padding:14px 14px}
  .dd-grid{grid-template-columns:1fr 1fr;gap:9px 14px}
  .dd-head{gap:8px}
}
"""


def _money(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def _pct(x) -> str:
    return "—" if x is None else f"{x:.1%}"


def _stats(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "gross": 0.0, "fees": 0.0, "net": 0.0}
    wins = [t for t in trades if (t["net_pnl"] or 0) > 0]
    losses = [t for t in trades if (t["net_pnl"] or 0) <= 0]
    return {"n": n, "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / n, "gross": sum(t["gross_pnl"] or 0 for t in trades),
            "fees": sum(t["fees"] or 0 for t in trades),
            "net": sum(t["net_pnl"] or 0 for t in trades)}


def _card(title: str, value: str, sub: str = "", sign=None) -> str:
    color = ""
    if sign is not None:
        color = "pos" if sign > 0 else ("neg" if sign < 0 else "")
    return (f"<div class='card'><div class='card-title'>{title}</div>"
            f"<div class='card-value {color}'>{value}</div>"
            f"<div class='card-sub'>{sub}</div></div>")


def _portfolio_card(equity, cash, sub: str = "") -> str:
    """Portfolio value card: equity + a Cash line.

    No % chip — the 'vs last close' daily change read as noise (often 0.00%
    overnight/after-hours). The meaningful lifetime return lives on the Net P/L card.
    """
    eq = float(equity) if equity is not None else None
    val = _money(eq) if eq is not None else "—"
    cash_txt = _money(cash) if cash is not None else "—"
    sub_txt = f"Cash: {cash_txt}" + (f" · {sub}" if sub else "")
    return (f"<div class='card'><div class='card-title'>Portfolio value</div>"
            f"<div class='card-value'>{val}</div>"
            f"<div class='card-sub'>{sub_txt}</div></div>")


def _netpl_card(net, capital) -> str:
    """Net P/L card: % return vs starting capital (primary), then the net $.

    The % is fee-inclusive (net). The gross/fees breakdown is intentionally NOT
    shown (it duplicated the $ figure and the per-trade fees already live in the
    decision-log detail).
    """
    pct = None
    if capital:
        try:
            pct = float(net or 0.0) / float(capital) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            pct = None
    cls = "pos" if (net or 0) > 0 else ("neg" if (net or 0) < 0 else "")
    if pct is None:
        val, amt = _money(net), ""
    else:
        sign = "+" if pct > 0 else ""
        val = f"{sign}{pct:.2f}%"
        amt = f"<span class='amt'>{_money(net)}</span>"
    return (f"<div class='card'><div class='card-title'>Net P/L</div>"
            f"<div class='card-value {cls}'>{val}{amt}</div></div>")


def _starting_capital(account: str, st: dict | None = None) -> float | None:
    """Baseline capital for an account: the set-once snapshot value, else config."""
    if st:
        v = (st.get(account) or {}).get("starting_equity")
        if v:
            return float(v)
    return config.STARTING_CAPITAL.get(account)


def _curve(points, w: int = 920, h: int = 240) -> str:
    if not points:
        return ("<div class='muted' style='padding:20px'>No closed trades yet — "
                "the equity curve appears here.</div>")
    ys = [p[1] for p in points]
    lo, hi = min(ys), max(ys)
    if lo == hi:
        hi = lo + 1
    span = (hi - lo) or 1.0
    lo -= span * 0.08
    hi += span * 0.08
    n = len(points)

    def sx(i: int) -> float:
        return w / 2 if n == 1 else 24 + i / (n - 1) * (w - 48)

    def sy(y: float) -> float:
        return h - 24 - (y - lo) / (hi - lo) * (h - 48)

    d = " ".join(("M" if i == 0 else "L") + f"{sx(i):.1f},{sy(p[1]):.1f}"
                 for i, p in enumerate(points))
    zero = ""
    if lo <= 0 <= hi:
        zy = sy(0.0)
        zero = f"<line x1='24' y1='{zy:.1f}' x2='{w-24}' y2='{zy:.1f}' class='zero'/>"
    return f"<svg viewBox='0 0 {w} {h}' class='curve'>{zero}<path d='{d}'/></svg>"


def _fmt_ts(s: str) -> str:
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return s or ""


def _event_badge(decision: str) -> str:
    color = _DECISION_COLORS.get(decision, "#8b949e")
    return (f"<span style='display:inline-block;padding:1px 8px;border-radius:999px;"
            f"font-size:11px;font-weight:600;color:{color};border:1px solid {color}'>"
            f"{html.escape(str(decision)).upper()}</span>")


def _events_rows(events) -> str:
    if not events:
        return ("<tr><td colspan='3' class='muted'>No activity yet — this bot logs "
                "its decisions here as it runs.</td></tr>")
    out = []
    for e in events:
        reason = html.escape(e["reason"] or "")
        detail = html.escape(e["detail"] or "")
        if detail:
            reason = f"{reason} <span class='muted'>· {detail}</span>"
        out.append(
            f"<tr><td class='muted'>{_fmt_ts(e['created_at'])}</td>"
            f"<td>{_event_badge(e['decision'])}</td>"
            f"<td>{reason}</td></tr>"
        )
    return "".join(out)


def _parse_json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


# --- trade math helpers ----------------------------------------------------- #

def _mult(r) -> int:
    return 100 if (r["asset_class"] == "option") else 1


def _rr_planned(r) -> float | None:
    if r["rr_planned"] is not None:
        return float(r["rr_planned"])
    entry, stop, tgt = r["entry_price"], r["stop_price"], r["target_price"]
    if entry and stop and tgt:
        if r["side"] == "long":
            return (tgt - entry) / (entry - stop) if entry != stop else None
        return (entry - tgt) / (stop - entry) if stop != entry else None
    return None


def _notional(r) -> float | None:
    if not r["entry_price"]:
        return None
    return float(r["qty"]) * _mult(r) * float(r["entry_price"])


def _risk_usd(r) -> float | None:
    if not r["entry_price"] or not r["stop_price"]:
        return None
    return float(r["qty"]) * _mult(r) * abs(float(r["entry_price"]) - float(r["stop_price"]))


def _fraction(r, eq: float | None, usd: float | None) -> float | None:
    return usd / eq if (eq and usd is not None) else None


def _dbtn(on_row_id: int) -> str:
    return (f"<button class='dbtn' type='button' title='Show decision log' "
            f"aria-label='Show decision log' aria-expanded='false'>{_INFO_ICON}</button>")


def _detail_row_html(r, eq: float | None, colspan: int) -> str:
    """Expanded decision-log content shown when a position's 'Why' is clicked.

    Structured for skimming: a head line (symbol + side + decision), a compact
    stats grid (risk/size/R:R/stop→target/opened), then narrative sections with
    the thesis highlighted. Quiet fallback when nothing is logged.
    """
    dj = _parse_json(r["decision_json"])
    ctx = (dj or {}).get("context") or {}
    symbol = html.escape(r["symbol"])
    side = r["side"]

    # --- head: what happened, at a glance ---
    head = (f"<span class='sym'>{symbol}</span>"
            f"<span class='qty muted'>x{r['qty']:g}</span>"
            f"<span class='dd-badge {side}'>{html.escape(side)}</span>")
    if (dj or {}).get("decision"):
        head += _event_badge((dj or {}).get("decision"))
    size_mult = (dj or {}).get("size_multiplier")
    if size_mult is not None and 0 < float(size_mult) < 1.0:
        head += f"<span class='muted dd-size'>sized {size_mult:g}</span>"

    # --- stats grid: the numbers ---
    stats: list[tuple[str, str]] = []
    risk = _risk_usd(r)
    if risk is not None:
        txt = _money(risk)
        fr = _fraction(r, eq, risk)
        if fr is not None:
            txt += f" <small>({fr:.1%} eq)</small>"
        stats.append(("Risk", txt))
    notional = _notional(r)
    if notional is not None:
        txt = _money(notional)
        fr = _fraction(r, eq, notional)
        if fr is not None:
            txt += f" <small>({fr:.1%} eq)</small>"
        stats.append(("Size", txt))
    rr = _rr_planned(r)
    if rr is not None:
        stats.append(("Planned R:R", f"{rr:g}"))
    crr = ctx.get("risk_reward")
    if crr is not None:
        txt = str(crr)
        if ctx.get("net_rr_after_fees") is not None:
            txt += f" <small>(net {ctx['net_rr_after_fees']})</small>"
        stats.append(("R:R", txt))
    stop, tgt = r["stop_price"], r["target_price"]
    if stop and tgt:
        stats.append(("Stop → Target", f"{_money(stop)} → {_money(tgt)}"))
    when = r["entry_time"] or r["created_at"]
    if when:
        stats.append(("Opened", _fmt_ts(when)))
    grid = "".join(
        f"<div class='s'><div class='dd-k'>{k}</div><div class='dd-v'>{v}</div></div>"
        for k, v in stats)

    # --- narrative sections (thesis highlighted) ---
    rationale = (dj or {}).get("rationale") or r["note"]
    secs: list[tuple[str, str, bool]] = []
    if rationale:
        secs.append(("Why", html.escape(str(rationale)), True))
    if ctx.get("technical"):
        secs.append(("Setup", html.escape(str(ctx["technical"])), False))
    if ctx.get("market_regime"):
        secs.append(("Regime", html.escape(str(ctx["market_regime"])), False))
    close_rat = (dj or {}).get("close_rationale")
    if close_rat:
        secs.append(("Exit reason", html.escape(str(close_rat)), False))
    if ctx.get("recent_performance"):
        secs.append(("Recent", html.escape(str(ctx["recent_performance"])), False))
    secs_html = "".join(
        f"<div class='dd-sec{' why' if why else ''}'>"
        f"<div class='dd-sec-label'>{label}</div>"
        f"<div class='dd-text'>{txt}</div></div>"
        for label, txt, why in secs)

    if not (stats or secs):
        body = "<div class='muted' style='padding:10px 0'>No decision summary recorded for this trade.</div>"
    else:
        body = f"<div class='dd-head'>{head}</div>"
        if stats:
            body += f"<div class='dd-grid'>{grid}</div>"
        if secs:
            body += f"<div class='dd-narr'>{secs_html}</div>"
    return f"<tr class='drow hidden'><td colspan='{colspan}'><div class='ddetail'>{body}</div></td></tr>"


# --- open positions table --------------------------------------------------- #

_OPEN_TH = ("<tr><th>Why</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Size</th>"
            "<th>Entry</th><th>Stop</th><th>Target</th><th>R:R</th></tr>")


def _open_rows(rows, eq: dict, with_account: bool = False) -> str:
    ncol = 10 if with_account else 9
    if not rows:
        return f"<tr><td colspan='{ncol}' class='muted'>No open positions.</td></tr>"
    out = []
    for r in rows:
        side_cls = "long" if r["side"] == "long" else "short"
        eq_acct = eq.get(r["account"])
        acct = ""
        if with_account:
            acct = (f"<td><a class='tab' style='padding:1px 8px' href='/{r['account']}'>"
                    f"{html.escape(r['account'])}</a></td>")
        notional = _notional(r)
        size_txt = _money(notional) if notional is not None else "—"
        nf = _fraction(r, eq_acct, notional)
        if nf is not None:
            size_txt += f" <span class='muted'>({nf:.1%})</span>"
        rr = _rr_planned(r)
        out.append(
            f"<tr><td>{_dbtn(r['id'])}</td>{acct}"
            f"<td>{_sym_tag(r['symbol'])}</td>"
            f"<td class='{side_cls}'>{html.escape(r['side'])}</td>"
            f"<td class='mono'>{r['qty']:g}</td>"
            f"<td>{size_txt}</td>"
            f"<td>{_money(r['entry_price'])}</td>"
            f"<td>{_money(r['stop_price'])}</td>"
            f"<td>{_money(r['target_price'])}</td>"
            f"<td>{rr if rr is None else f'{rr:g}'}</td></tr>"
        )
        colspan = 10 if with_account else 9
        out.append(_detail_row_html(r, eq_acct, colspan))
    return "".join(out)


# --- closed trades table ---------------------------------------------------- #

def _closed_rows(rows, with_account: bool = True) -> str:
    ncol = 10 if with_account else 8
    if not rows:
        return f"<tr><td colspan='{ncol}' class='muted'>None yet.</td></tr>"
    out = []
    for r in rows:
        pnl = r["net_pnl"] or 0
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        side_cls = "long" if r["side"] == "long" else "short"
        acct = ""
        if with_account:
            acct = (f"<td>{html.escape(r['account'])}</td>"
                    f"<td class='muted'>{_STRATEGY_LABEL.get(r['strategy'], r['strategy'])}</td>")
        out.append(
            f"<tr><td class='muted'>{_fmt_ts(r['exit_time'] or r['created_at'])}</td>"
            f"<td>{_dbtn(r['id'])}</td>{acct}"
            f"<td>{_sym_tag(r['symbol'])}</td>"
            f"<td class='{side_cls}'>{html.escape(r['side'])}</td>"
            f"<td class='mono'>{r['qty']:g}</td>"
            f"<td>{_money(r['entry_price'])}</td>"
            f"<td>{_money(r['exit_price'])}</td>"
            f"<td class='{cls}'>{_money(pnl)}</td></tr>"
        )
        out.append(_detail_row_html(r, None, ncol))
    return "".join(out)


def _read_build_file(name: str) -> str | None:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name)) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _build_info() -> str:
    """Version + commit + time-since-last-update for the dashboard footer.

    Commit/date are baked into the image at build time (commit.txt/date.txt);
    env vars take precedence for manual/scripted builds.
    """
    parts = [f"v{config.VERSION}"]
    commit = os.environ.get("GIT_COMMIT") or _read_build_file("commit.txt") or "unknown"
    parts.append(f"commit {commit}")
    git_date = os.environ.get("GIT_DATE") or _read_build_file("date.txt")
    if git_date:
        try:
            dt = datetime.fromisoformat(git_date.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
            days, seconds = delta.days, delta.seconds
            hours, mins = seconds // 3600, (seconds % 3600) // 60
            rel = f"{days}d {hours}h" if days else (f"{hours}h {mins}m" if hours else f"{mins}m")
            parts.append(f"updated {rel} ago")
        except (ValueError, TypeError):
            pass
    return " · ".join(parts)


def _page(title: str, active: str, body: str) -> str:
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Top menu bar: title/brand + nav, saves vertical space vs a standalone h1.
    items = [("overview", "Overview", "/"), ("daily", "Daily", "/daily"),
             ("weekly", "Weekly", "/weekly"), ("yolo", "YOLO", "/yolo")]
    links = []
    for key, label, href in items:
        cls = " active" if key == active else ""
        links.append(f"<a class='tab{cls}' href='{href}'>{label}</a>")
    menu = "".join(links)
    return f"""<!doctype html>
<html lang='en'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>
<meta name='theme-color' content='#0d1117'>
<meta name='apple-mobile-web-app-capable' content='yes'>
<meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>
<meta name='apple-mobile-web-app-title' content='Alpaca'>
<title>{title} — Alpaca Trading</title>
<link rel='icon' type='image/svg+xml' href='{_FAVICON}'>
<style>{_CSS}</style></head>
<body>
<header class='topbar'>
  <a class='brand' href='/'><span class='logo'>A</span>Alpaca<span class='paper'>PAPER</span></a>
  <nav class='menu' aria-label='Navigation'>{menu}</nav>
</header>
<main>
{body}
</main>
<div id='tip' role='tooltip'></div>
<footer class='footer'>Paper trading only. Not investment advice. Regulatory fees (SEC / TAF / CAT) are modelled per Alpaca's brokerage fee schedule; net P/L is gross minus those modelled fees.<br><span style='opacity:.65'>Generated {gen} · {_build_info()}</span></footer>
<script>
(function(){{
  var tip = document.getElementById('tip');
  function show(el){{
    var name = el.getAttribute('data-name') || el.textContent;
    var desc = el.getAttribute('data-desc') || '';
    tip.innerHTML = '<b>'+name+'</b><span>'+desc+'</span>';
    tip.style.display = 'block';
    var r = el.getBoundingClientRect();
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var x = Math.min(Math.max(r.left + r.width/2 - w/2, 8), window.innerWidth - w - 8);
    var y = r.top - h - 8;
    if (y < 8) y = r.bottom + 10;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }}
  function hide(){{ tip.style.display = 'none'; }}
  function attach(el){{
    el.addEventListener('mouseenter', function(){{ show(el); }});
    el.addEventListener('mouseleave', hide);
    el.addEventListener('focus', function(){{ show(el); }});
    el.addEventListener('blur', hide);
    el.addEventListener('click', function(evt){{
      evt.stopPropagation();
      if (tip.style.display === 'block' && tip.__el === el) {{ hide(); tip.__el = null; }}
      else {{ show(el); tip.__el = el; }}
    }});
  }}
  Array.prototype.forEach.call(document.querySelectorAll('.sx'), attach);
  document.addEventListener('click', function(evt){{
    if (!evt.target.closest('.sx')) {{ hide(); tip.__el = null; }}
  }});
  document.addEventListener('scroll', hide, {{passive:true}});
  document.addEventListener('keydown', function(e){{ if(e.key==='Escape') hide(); }});
  /* Per-position decision-log expander. */
  Array.prototype.forEach.call(document.querySelectorAll('button.dbtn'), function(b){{
    b.addEventListener('click', function(){{
      var tr = b.closest('tr');
      var dr = tr ? tr.nextElementSibling : null;
      if (dr && dr.classList.contains('drow')) {{
        var open = dr.classList.toggle('hidden') === false;
        b.classList.toggle('on', open);
        b.setAttribute('aria-expanded', open ? 'true' : 'false');
      }}
    }});
  }});
}})();
</script>
</body></html>"""


def _open_th(with_account: bool) -> str:
    if with_account:
        return ("<tr><th>Why</th><th>Account</th><th>Symbol</th><th>Side</th><th>Qty</th>"
                "<th>Size</th><th>Entry</th><th>Stop</th><th>Target</th><th>R:R</th></tr>")
    return _OPEN_TH


def _closed_th(with_account: bool) -> str:
    if with_account:
        return ("<tr><th>Closed</th><th>Why</th><th>Account</th><th>Strategy</th><th>Symbol</th>"
                "<th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Net P/L</th></tr>")
    return ("<tr><th>Closed</th><th>Why</th><th>Symbol</th><th>Side</th><th>Qty</th>"
            "<th>Entry</th><th>Exit</th><th>Net P/L</th></tr>")


def _positions_trades_block(open_rows, closed_rows, eq: dict, with_account: bool = False) -> str:
    """One box holding open positions (current state) then recent closed trades
    (history). Each keeps its own columns, so they sit as two labelled tables in
    a single panel — fewer page sections, no column mismatch."""
    n_open, n_closed = len(open_rows), len(closed_rows)
    open_lbl = (f"Open positions <span class='muted'>· {n_open}</span>"
                if n_open else "Open positions")
    closed_lbl = (f"Recent closed trades <span class='muted'>· {n_closed}</span>"
                  if n_closed else "Recent closed trades")
    return (
        f"<div class='panel'>"
        f"<div class='panel-sub'>{open_lbl}</div>"
        f"<table>{_open_th(with_account)}{_open_rows(open_rows, eq, with_account)}</table>"
        f"<div class='panel-sub sep'>{closed_lbl}</div>"
        f"<table>{_closed_th(with_account)}{_closed_rows(closed_rows, with_account)}</table>"
        f"</div>"
    )


def _overview_body() -> str:
    db.init_db()
    all_t = db.all_trades()
    closed = [t for t in all_t if t["status"] == "closed"]
    open_t = db.open_trades()
    closed_sorted = sorted(closed, key=lambda t: t["exit_time"] or "")
    eq = db.account_equity()
    st = db.account_state()

    acct_cards = []
    now = datetime.now(timezone.utc)
    for a in ACCOUNTS:
        s = _stats([t for t in closed if t["account"] == a])
        le = db.last_event(a, _STRATEGY[a])
        last_line = ""
        if le:
            last_line = (f"<div class='muted' style='font-size:11px;margin-top:6px'>"
                         f"{_event_badge(le['decision'])} "
                         f"{html.escape((le['reason'] or '')[:90])}</div>")
        next_line = (f"<div class='muted' style='font-size:11px;margin-top:4px'>"
                     f"next: {schedule.next_label(a, now)}</div>")
        eq_a = eq.get(a)
        eq_txt = _money(eq_a) if eq_a is not None else "—"
        net_cls = "pos" if s["net"] > 0 else ("neg" if s["net"] < 0 else "")
        acct_cards.append(
            f"<a class='acct' href='/{a}'>"
            f"<h3>{a.upper()}</h3>"
            f"<div class='card-title' style='margin-top:2px'>Portfolio value</div>"
            f"<div class='big'>{eq_txt}</div>"
            f"<div class='{net_cls}' style='margin-top:6px'>{_money(s['net'])} <span class='muted'>net</span></div>"
            f"<div class='muted' style='margin-top:2px'>{s['n']} trades · {s['win_rate']:.0%} win · fees {_money(s['fees'])}</div>"
            f"{next_line}{last_line}</a>"
        )

    s_all = _stats(closed)
    eq_vals = [e for e in eq.values() if e is not None]
    total_eq = sum(eq_vals) if eq_vals else None
    cash_vals = [s["cash"] for s in st.values() if s.get("cash") is not None]
    total_cash = sum(cash_vals) if cash_vals else None
    total_cap = sum(_starting_capital(a, st) or 0 for a in ACCOUNTS) or None
    curve_pts, cum = [], 0.0
    for t in closed_sorted:
        cum += (t["net_pnl"] or 0)
        curve_pts.append((t["exit_time"], cum))

    recent = list(reversed(closed_sorted))[:20]

    return f"""
<h2>All accounts</h2>
<div class='grid'>
{_portfolio_card(total_eq, total_cash, sub=f"{len(eq_vals)} account{'s' if len(eq_vals) != 1 else ''}")}
{_netpl_card(s_all["net"], total_cap)}
{_card("Closed trades", str(s_all["n"]))}
{_card("Win rate", f"{s_all['win_rate']:.0%}")}
{_card("Open positions", str(len(open_t)))}
</div>

<h2>Positions &amp; trades</h2>
{_positions_trades_block(open_t, recent, eq, with_account=True)}

<h2>Accounts</h2>
<div class='accts'>{''.join(acct_cards)}</div>

<h2>Equity curve (cumulative net P/L)</h2>
<div class='panel'>{_curve(curve_pts)}</div>
"""


def _account_body(account: str) -> str:
    db.init_db()
    trades = [t for t in db.all_trades() if t["account"] == account]
    closed = [t for t in trades if t["status"] == "closed"]
    open_t = [t for t in trades if t["status"] == "open"]
    closed_sorted = sorted(closed, key=lambda t: t["exit_time"] or "")
    events = db.recent_events(100, account=account)
    eq = db.account_equity()
    st = db.account_state()

    s = _stats(closed)
    curve_pts, cum = [], 0.0
    for t in closed_sorted:
        cum += (t["net_pnl"] or 0)
        curve_pts.append((t["exit_time"], cum))

    recent = list(reversed(closed_sorted))[:20]
    label = _STRATEGY_LABEL.get(_STRATEGY[account], account)
    sa = st.get(account, {})

    return f"""
<div class='page-meta'><span class='strategy'>{html.escape(label)}</span> <span class='muted'>·</span> next eval <strong>{schedule.next_label(account)}</strong></div>
<div class='grid'>
{_portfolio_card(sa.get("equity"), sa.get("cash"))}
{_netpl_card(s["net"], _starting_capital(account, st))}
{_card("Closed trades", str(s["n"]))}
{_card("Win rate", f"{s['win_rate']:.0%}")}
{_card("Open positions", str(len(open_t)))}
</div>

<h2>Positions &amp; trades</h2>
{_positions_trades_block(open_t, recent, eq, with_account=False)}

<h2>Equity curve</h2>
<div class='panel'>{_curve(curve_pts)}</div>

<details class='journal'><summary>Decision journal <span class='muted'>— every go / no-go / skip / error this bot logged (latest {len(events)})</span></summary>
<div class='panel'><table><tr><th>Time</th><th>Decision</th><th>Reason</th></tr>
{_events_rows(events)}</table></div>
</details>
"""


def build() -> str:
    return _page("Overview", "overview", _overview_body())


def build_account(account: str) -> str:
    return _page(account.upper(), account, _account_body(account))


def main() -> None:
    config.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = build()
    config.DASHBOARD_PATH.write_text(out)
    print(f"Dashboard written to {config.DASHBOARD_PATH} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
