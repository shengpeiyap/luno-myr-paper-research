"""Generate a self-contained HTML research dashboard for portfolio replay JSON."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any


def _x(timestamp: int, start: int, end: int, left: float = 64, width: float = 910) -> float:
    return left if end == start else left + (timestamp - start) / (end - start) * width


def _chart(values: list[tuple[int, float]], *, title: str, color: str, suffix: str,
           trades: list[dict[str, Any]] | None = None) -> str:
    if not values:
        return f"<section class='card'><h2>{html.escape(title)}</h2><p>No data</p></section>"
    width, height = 1000, 300
    left, right, top, bottom = 64, 20, 20, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    start, end = values[0][0], values[-1][0]
    numbers = [value for _, value in values]
    low, high = min(numbers), max(numbers)
    if high == low:
        padding = max(1.0, abs(high) * 0.03)
        low, high = low - padding, high + padding
    else:
        padding = (high - low) * 0.08
        low, high = low - padding, high + padding
    def y(value: float) -> float:
        return top + (high - value) / (high - low) * plot_h
    points = " ".join(f"{_x(t, start, end):.1f},{y(v):.1f}" for t, v in values)
    grid = []
    for step in range(5):
        value = high - (high - low) * step / 4
        yy = y(value)
        grid.append(f"<line x1='{left}' y1='{yy:.1f}' x2='{width-right}' y2='{yy:.1f}' class='grid'/><text x='{left-8}' y='{yy+4:.1f}' text-anchor='end'>{value:,.2f}{html.escape(suffix)}</text>")
    markers = []
    for trade in trades or []:
        for key, label, marker_color in (("entry_time_utc", "BUY", "#16845b"), ("exit_time_utc", "SELL", "#bd3e47")):
            try:
                stamp = int(datetime.fromisoformat(str(trade[key]).replace("Z", "+00:00")).timestamp() * 1000)
            except (KeyError, TypeError, ValueError):
                continue
            if start <= stamp <= end:
                xx = _x(stamp, start, end)
                point_index = round((stamp - start) / (end - start) * (len(values) - 1)) if end != start else 0
                yy = y(numbers[min(len(numbers) - 1, max(0, point_index))])
                marker = "▲" if label == "BUY" else "▼"
                markers.append(f"<text x='{xx:.1f}' y='{yy:.1f}' text-anchor='middle' fill='{marker_color}' font-size='15' title='{label} {html.escape(str(trade.get('asset','')))}'>{marker}</text>")
    first_label = datetime.fromtimestamp(start / 1000, timezone.utc).strftime("%Y-%m-%d")
    last_label = datetime.fromtimestamp(end / 1000, timezone.utc).strftime("%Y-%m-%d")
    return (
        f"<section class='card'><h2>{html.escape(title)}</h2><svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)} chart'>"
        f"{''.join(grid)}<polyline points='{points}' fill='none' stroke='{color}' stroke-width='2.5' stroke-linejoin='round' stroke-linecap='round'/>{''.join(markers)}"
        f"<text x='{left}' y='{height-12}'>{first_label}</text><text x='{width-right}' y='{height-12}' text-anchor='end'>{last_label}</text></svg></section>"
    )


def render_replay_html(result: dict[str, Any]) -> str:
    """Render summary, equity/drawdown plots, trade markers, and trade ledger."""
    curve = result.get("equity_curve", [])
    trades = result.get("trades", [])
    equity = [(int(row["timestamp_ms"]), float(row["equity_myr"])) for row in curve]
    drawdown = [(int(row["timestamp_ms"]), float(row["drawdown_pct"])) for row in curve]
    metrics = [
        ("Start", f"MYR {float(result['initial_equity_myr']):,.2f}"),
        ("End", f"MYR {float(result['ending_equity_myr']):,.2f}"),
        ("Return", f"{float(result['return_pct']):.2f}%"),
        ("Max drawdown", f"{float(result['max_drawdown_pct']):.2f}%"),
        ("Trades", str(result["trade_count"])),
        ("Win rate", f"{float(result['win_rate_pct']):.1f}%"),
        ("Fees", f"MYR {float(result['fees_myr']):,.2f}"),
        ("Drawdown halt", "Yes" if result.get("halted_at_drawdown_limit") else "No"),
    ]
    cards = "".join(f"<div class='metric'><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>" for label, value in metrics)
    rows = []
    for trade in trades:
        rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
                trade.get("asset", ""), trade.get("entry_time_utc", ""), trade.get("exit_time_utc", ""),
                f"{float(trade.get('entry_price_myr', 0)):,.4f}", f"{float(trade.get('exit_price_myr', 0)):,.4f}",
                f"{float(trade.get('pnl_myr', 0)):,.2f}", trade.get("exit_reason", ""),
            )) + "</tr>"
        )
    trade_table = "".join(rows) or "<tr><td colspan='7'>No completed trades</td></tr>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MYR Portfolio Replay</title><style>
:root{{--bg:#f4f6f8;--card:#fff;--ink:#1e293b;--muted:#64748b;--line:#e2e8f0}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1180px;margin:32px auto;padding:0 20px}}h1{{margin:0 0 4px}}h2{{font-size:18px;margin:0 0 16px}}.sub{{color:var(--muted);margin-bottom:20px}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:18px 0}}.metric,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 1px 2px #0f172a0a}}
.metric span{{display:block;color:var(--muted);font-size:13px}}.metric strong{{display:block;margin-top:6px;font-size:19px}}.card{{margin:14px 0;overflow:auto}}svg{{width:100%;height:auto;min-width:620px}}svg text{{font-size:11px;fill:var(--muted)}}.grid{{stroke:var(--line);stroke-width:1}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}th{{color:var(--muted)}}.note{{color:var(--muted);font-size:13px}}
</style></head><body><main><h1>MYR Portfolio Replay</h1>
<div class="sub">{html.escape(', '.join(result.get('assets', [])))} · {html.escape(str(result.get('timeframe','')))} · Offline OHLCV simulation · Research only</div>
<div class="metrics">{cards}</div>
{_chart(equity, title='Equity curve (MYR)', color='#315fd3', suffix='', trades=trades)}
{_chart(drawdown, title='Drawdown (%)', color='#bd3e47', suffix='%', trades=None)}
<section class="card"><h2>Trade ledger</h2><div style="overflow:auto"><table><thead><tr><th>Asset</th><th>Entry UTC</th><th>Exit UTC</th><th>Entry MYR</th><th>Exit MYR</th><th>P&amp;L MYR</th><th>Exit reason</th></tr></thead><tbody>{trade_table}</tbody></table></div></section>
<p class="note">This report reflects the selected history and cost assumptions only. It is not a forecast or a live-trading recommendation.</p></main></body></html>"""
