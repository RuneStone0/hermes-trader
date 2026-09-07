"""Generate the dark-theme HTML dashboard(s) from trades.db.

Two page types:
  - overview ("/"): all-accounts summary, clickable per-account cards, equity
    curve, and recent closed trades.
  - account ("/daily", "/weekly", "/yolo"): one account's stats, its full
    decision log, equity curve, and open/closed trades.

Net P/L = gross P/L minus modelled regulatory fees. Self-contained HTML
(inline CSS + inline SVG, no CDN).
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
                   "yolo": "YOLO"}
_DECISION_COLORS = {"go": "#3fb950", "exit": "#58a6ff", "no_go": "#d29922",
                    "skip": "#8b949e", "error": "#f85149"}

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

_CSS = """
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--pos:#3fb950;--neg:#f85149;--accent:#58a6ff;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:24px;max-width:1100px;margin:0 auto}
h1{font-size:20px;font-weight:600}
h2{font-size:15px;margin:28px 0 12px;font-weight:600}
h3{font-size:13px;color:var(--accent);margin-bottom:6px}
.muted{color:var(--muted)}
.big{font-size:22px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.card-title{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card-value{font-size:22px;font-weight:600;margin-top:4px}
.card-sub{color:var(--muted);font-size:12px;margin-top:2px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.accts{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.acct{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;text-decoration:none;color:var(--text);display:block;transition:border-color .15s}
.acct:hover{border-color:var(--accent)}
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.04em}
tr:last-child td{border-bottom:none}
.curve{width:100%;height:auto;background:var(--card);border:1px solid var(--border);border-radius:10px}
.curve path{fill:none;stroke:var(--accent);stroke-width:2}
.curve .zero{stroke:var(--border);stroke-dasharray:4 4}
.footer{color:var(--muted);font-size:12px;margin-top:24px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--border);color:var(--muted)}
.tabs{display:flex;gap:6px;margin:18px 0 4px;flex-wrap:wrap}
.tab{padding:6px 14px;border-radius:8px;border:1px solid var(--border);color:var(--muted);text-decoration:none;font-size:13px;font-weight:500}
.tab:hover{color:var(--text);border-color:var(--accent)}
.tab.active{background:var(--accent);color:#0d1117;border-color:var(--accent)}
.trade{border-bottom:1px solid var(--border);padding:10px 4px}
.trade summary{cursor:pointer;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.trade-body{padding:10px 0 4px 18px;font-size:13px}
.dl{margin:4px 0}
.lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-right:6px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.long{color:var(--pos)}.short{color:var(--neg)}
"""


def _money(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


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


def _closed_rows(rows, with_account: bool = True) -> str:
    cols = 8 if with_account else 6
    if not rows:
        return f"<tr><td colspan='{cols}' class='muted'>None yet.</td></tr>"
    out = []
    for r in rows:
        pnl = r["net_pnl"] or 0
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        acct = (f"<td>{html.escape(r['account'])}</td>"
                f"<td class='muted'>{_STRATEGY_LABEL.get(r['strategy'], r['strategy'])}</td>") \
            if with_account else ""
        out.append(
            f"<tr>{acct}<td>{html.escape(r['symbol'])}</td><td>{r['side']}</td><td>{r['qty']:g}</td>"
            f"<td>{_money(r['entry_price'])}</td><td>{_money(r['exit_price'])}</td>"
            f"<td class='{cls}'>{_money(pnl)}</td></tr>"
        )
    return "".join(out)


def _open_rows(rows, with_account: bool = True) -> str:
    cols = 8 if with_account else 6
    if not rows:
        return f"<tr><td colspan='{cols}' class='muted'>No open positions.</td></tr>"
    out = []
    for r in rows:
        acct = (f"<td>{html.escape(r['account'])}</td>"
                f"<td class='muted'>{_STRATEGY_LABEL.get(r['strategy'], r['strategy'])}</td>") \
            if with_account else ""
        out.append(
            f"<tr>{acct}<td>{html.escape(r['symbol'])}</td><td>{r['side']}</td><td>{r['qty']:g}</td>"
            f"<td>{_money(r['entry_price'])}</td><td>{_money(r['stop_price'])}</td>"
            f"<td>{_money(r['target_price'])}</td></tr>"
        )
    return "".join(out)


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
            f"{decision.upper()}</span>")


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


def _decision_body(dj) -> str:
    if not dj:
        return "<div class='muted'>No decision summary recorded for this trade.</div>"
    ctx = dj.get("context") or {}
    rows = []
    badge = _event_badge(dj.get("decision", ""))
    if dj.get("size_multiplier") is not None:
        badge += f" <span class='muted'>· size {dj['size_multiplier']}</span>"
    rows.append(("Decision", badge))
    if dj.get("rationale"):
        rows.append(("Rationale", html.escape(str(dj["rationale"]))))
    if ctx.get("technical"):
        rows.append(("Setup", html.escape(str(ctx["technical"]))))
    if ctx.get("market_regime"):
        rows.append(("Regime", html.escape(str(ctx["market_regime"]))))
    if ctx.get("risk_reward") is not None:
        rr = str(ctx["risk_reward"])
        if ctx.get("net_rr_after_fees") is not None:
            rr += f" <span class='muted'>(net {ctx['net_rr_after_fees']} after fees)</span>"
        rows.append(("Risk:reward", rr))
    if ctx.get("recent_performance"):
        rows.append(("Recent", html.escape(str(ctx["recent_performance"]))))
    return "".join(f"<div class='dl'><span class='lbl'>{k}</span> {v}</div>" for k, v in rows)


def _trade_details(rows) -> str:
    if not rows:
        return "<div class='muted' style='padding:10px'>No closed trades yet.</div>"
    out = []
    for r in rows:
        pnl = r["net_pnl"] or 0
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        side_cls = "long" if r["side"] == "long" else "short"
        dj = _parse_json(r["decision_json"])
        out.append(
            f"<details class='trade'><summary>"
            f"<span class='muted mono'>{_fmt_ts(r['exit_time'] or r['created_at'])}</span>"
            f"<strong>{html.escape(r['symbol'])}</strong>"
            f"<span class='{side_cls}'>{r['side']}</span> x{r['qty']:g}"
            f"{_money(r['entry_price'])} → {_money(r['exit_price'])}"
            f"<span class='{cls}'>{_money(pnl)}</span>"
            f"</summary><div class='trade-body'>{_decision_body(dj)}</div></details>"
        )
    return "".join(out)


def _open_details(rows) -> str:
    if not rows:
        return "<div class='muted' style='padding:10px'>No open positions.</div>"
    out = []
    for r in rows:
        side_cls = "long" if r["side"] == "long" else "short"
        dj = _parse_json(r["decision_json"])
        out.append(
            f"<details class='trade'><summary>"
            f"<strong>{html.escape(r['symbol'])}</strong>"
            f"<span class='{side_cls}'>{r['side']}</span> x{r['qty']:g}"
            f"<span class='muted'>entry {_money(r['entry_price'])} · stop {_money(r['stop_price'])} · target {_money(r['target_price'])}</span>"
            f"</summary><div class='trade-body'>{_decision_body(dj)}</div></details>"
        )
    return "".join(out)


def _nav(active: str) -> str:
    items = [("overview", "Overview", "/"),
             ("daily", "Daily", "/daily"),
             ("weekly", "Weekly", "/weekly"),
             ("yolo", "YOLO", "/yolo")]
    links = []
    for key, label, href in items:
        cls = "active" if key == active else ""
        links.append(f"<a class='tab {cls}' href='{href}'>{label}</a>")
    return f"<nav class='tabs'>{''.join(links)}</nav>"
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
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{title} — Alpaca Trading</title>
<link rel='icon' type='image/svg+xml' href='{_FAVICON}'>
<style>{_CSS}</style></head><body>
<h1>Alpaca Trading Dashboard <span class='badge'>PAPER</span></h1>
<div class='muted' style='margin-top:4px'>Net P/L includes modelled regulatory fees (SEC / TAF / CAT) · generated {gen}</div>
{_nav(active)}
{body}
<div class='footer'>Paper trading only. Not investment advice. Regulatory fees modelled per Alpaca's brokerage fee schedule.<br><span style='opacity:.65'>{_build_info()}</span></div>
</body></html>"""


def _overview_body() -> str:
    db.init_db()
    all_t = db.all_trades()
    closed = [t for t in all_t if t["status"] == "closed"]
    open_t = db.open_trades()
    closed_sorted = sorted(closed, key=lambda t: t["exit_time"] or "")

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
        acct_cards.append(
            f"<a class='acct' href='/{a}'>"
            f"<h3>{a.upper()}</h3>"
            f"<div class='big {('pos' if s['net'] > 0 else ('neg' if s['net'] < 0 else ''))}'>{_money(s['net'])}</div>"
            f"<div class='muted'>gross {_money(s['gross'])} · fees {_money(s['fees'])}</div>"
            f"<div class='muted'>{s['n']} trades · {s['win_rate']:.0%} win</div>"
            f"{next_line}{last_line}</a>"
        )

    s_all = _stats(closed)
    curve_pts, cum = [], 0.0
    for t in closed_sorted:
        cum += (t["net_pnl"] or 0)
        curve_pts.append((t["exit_time"], cum))

    recent = list(reversed(closed_sorted))[:20]

    return f"""
<h2>All accounts</h2>
<div class='grid'>
{_card("Net P/L", _money(s_all["net"]), f"gross {_money(s_all['gross'])} · fees {_money(s_all['fees'])}", sign=s_all["net"])}
{_card("Closed trades", str(s_all["n"]))}
{_card("Win rate", f"{s_all['win_rate']:.0%}")}
{_card("Open positions", str(len(open_t)))}
</div>

<h2>Accounts</h2>
<div class='accts'>{''.join(acct_cards)}</div>

<h2>Equity curve (cumulative net P/L)</h2>
<div class='panel'>{_curve(curve_pts)}</div>

<h2>Recent closed trades</h2>
<div class='panel'><table><tr><th>Account</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Net P/L</th></tr>
{_closed_rows(recent)}</table></div>
"""


def _account_body(account: str) -> str:
    db.init_db()
    trades = [t for t in db.all_trades() if t["account"] == account]
    closed = [t for t in trades if t["status"] == "closed"]
    open_t = [t for t in trades if t["status"] == "open"]
    closed_sorted = sorted(closed, key=lambda t: t["exit_time"] or "")
    events = db.recent_events(100, account=account)

    s = _stats(closed)
    curve_pts, cum = [], 0.0
    for t in closed_sorted:
        cum += (t["net_pnl"] or 0)
        curve_pts.append((t["exit_time"], cum))

    recent = list(reversed(closed_sorted))[:20]
    label = _STRATEGY_LABEL.get(_STRATEGY[account], account)

    return f"""
<h2>{account.upper()} <span class='muted' style='font-size:13px'>— {label}</span></h2>
<div class='muted' style='margin:4px 0 0'>Next evaluation: {schedule.next_label(account)}</div>
<div class='grid'>
{_card("Net P/L", _money(s["net"]), f"gross {_money(s['gross'])} · fees {_money(s['fees'])}", sign=s["net"])}
{_card("Closed trades", str(s["n"]))}
{_card("Win rate", f"{s['win_rate']:.0%}")}
{_card("Open positions", str(len(open_t)))}
</div>

<h2>Decision log</h2>
<div class='panel'><table><tr><th>Time</th><th>Decision</th><th>Reason</th></tr>
{_events_rows(events)}</table></div>

<h2>Equity curve</h2>
<div class='panel'>{_curve(curve_pts)}</div>

<h2>Open positions</h2>
<div class='panel'>{_open_details(open_t)}</div>

<h2>Recent closed trades</h2>
<div class='panel'>{_trade_details(recent)}</div>
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
