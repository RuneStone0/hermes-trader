"""Generate the dark-theme HTML dashboard from trades.db.

Net P/L = gross P/L minus modelled regulatory fees, so figures read like a real
live Alpaca account. Self-contained HTML (inline CSS + inline SVG, no CDN).

Run:  python3 dashboard.py
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import config
import db


def _money(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def _stats(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "gross": 0.0, "fees": 0.0, "net": 0.0}
    wins = [t for t in trades if (t["net_pnl"] or 0) > 0]
    losses = [t for t in trades if (t["net_pnl"] or 0) <= 0]
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "gross": sum(t["gross_pnl"] or 0 for t in trades),
        "fees": sum(t["fees"] or 0 for t in trades),
        "net": sum(t["net_pnl"] or 0 for t in trades),
    }


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


def _closed_rows(rows) -> str:
    if not rows:
        return "<tr><td colspan='8' class='muted'>None yet.</td></tr>"
    out = []
    for r in rows:
        pnl = r["net_pnl"] or 0
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        out.append(
            f"<tr><td>{html.escape(r['account'])}</td><td>{html.escape(r['strategy'])}</td>"
            f"<td>{html.escape(r['symbol'])}</td><td>{r['side']}</td><td>{r['qty']:g}</td>"
            f"<td>{_money(r['entry_price'])}</td><td>{_money(r['exit_price'])}</td>"
            f"<td class='{cls}'>{_money(pnl)}</td></tr>"
        )
    return "".join(out)


def _open_rows(rows) -> str:
    if not rows:
        return "<tr><td colspan='8' class='muted'>No open positions.</td></tr>"
    out = []
    for r in rows:
        out.append(
            f"<tr><td>{html.escape(r['account'])}</td><td>{html.escape(r['strategy'])}</td>"
            f"<td>{html.escape(r['symbol'])}</td><td>{r['side']}</td><td>{r['qty']:g}</td>"
            f"<td>{_money(r['entry_price'])}</td><td>{_money(r['stop_price'])}</td>"
            f"<td>{_money(r['target_price'])}</td></tr>"
        )
    return "".join(out)


def build() -> str:
    db.init_db()
    all_t = db.all_trades()
    closed = [t for t in all_t if t["status"] == "closed"]
    open_t = db.open_trades()
    closed_sorted = sorted(closed, key=lambda t: t["exit_time"] or "")

    acct_cards = []
    for a in ("daily", "weekly", "yolo"):
        s = _stats([t for t in closed if t["account"] == a])
        acct_cards.append(
            f"<div class='acct'><h3>{a.upper()}</h3>"
            f"<div class='big {('pos' if s['net'] > 0 else ('neg' if s['net'] < 0 else ''))}'>"
            f"{_money(s['net'])}</div>"
            f"<div class='muted'>gross {_money(s['gross'])} · fees {_money(s['fees'])}</div>"
            f"<div class='muted'>{s['n']} trades · {s['win_rate']:.0%} win</div></div>"
        )

    s_all = _stats(closed)
    curve_pts, cum = [], 0.0
    for t in closed_sorted:
        cum += (t["net_pnl"] or 0)
        curve_pts.append((t["exit_time"], cum))

    recent = list(reversed(closed_sorted))[:20]
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Alpaca Trading Dashboard</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--pos:#3fb950;--neg:#f85149;--accent:#58a6ff;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding:24px;max-width:1100px;margin:0 auto}}
h1{{font-size:20px;font-weight:600}}
h2{{font-size:15px;margin:28px 0 12px;font-weight:600}}
h3{{font-size:13px;color:var(--accent);margin-bottom:6px}}
.muted{{color:var(--muted)}}
.big{{font-size:22px;font-weight:600}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}}
.card-title{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.card-value{{font-size:22px;font-weight:600;margin-top:4px}}
.card-sub{{color:var(--muted);font-size:12px;margin-top:2px}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}
.accts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.acct{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}}
.panel{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}}
th{{color:var(--muted);font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.04em}}
tr:last-child td{{border-bottom:none}}
.curve{{width:100%;height:auto;background:var(--card);border:1px solid var(--border);border-radius:10px}}
.curve path{{fill:none;stroke:var(--accent);stroke-width:2}}
.curve .zero{{stroke:var(--border);stroke-dasharray:4 4}}
.footer{{color:var(--muted);font-size:12px;margin-top:24px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--border);color:var(--muted)}}
</style></head><body>
<h1>Alpaca Trading Dashboard <span class='badge'>PAPER</span></h1>
<div class='muted' style='margin-top:4px'>Three accounts · net P/L includes modelled regulatory fees (SEC / TAF / CAT) · generated {gen}</div>

<h2>All accounts</h2>
<div class='grid'>
{_card("Net P/L", _money(s_all["net"]), f"gross {_money(s_all['gross'])} · fees {_money(s_all['fees'])}", sign=s_all["net"])}
{_card("Closed trades", str(s_all["n"]))}
{_card("Win rate", f"{s_all['win_rate']:.0%}")}
{_card("Open positions", str(len(open_t)))}
</div>

<h2>Per account</h2>
<div class='accts'>{''.join(acct_cards)}</div>

<h2>Equity curve (cumulative net P/L)</h2>
<div class='panel'>{_curve(curve_pts)}</div>

<h2>Open positions</h2>
<div class='panel'><table><tr><th>Account</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th></tr>
{_open_rows(open_t)}</table></div>

<h2>Recent closed trades</h2>
<div class='panel'><table><tr><th>Account</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Net P/L</th></tr>
{_closed_rows(recent)}</table></div>

<div class='footer'>Paper trading only. Not investment advice. Regulatory fees modelled per Alpaca's brokerage fee schedule.</div>
</body></html>"""


def main() -> None:
    config.DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = build()
    config.DASHBOARD_PATH.write_text(out)
    print(f"Dashboard written to {config.DASHBOARD_PATH} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
