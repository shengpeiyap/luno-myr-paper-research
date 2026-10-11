"""Optional Telegram notifications for paper-only execution events."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def telegram_credentials_present() -> bool:
    return bool(os.environ.get("LUNO_PAPER_TELEGRAM_BOT_TOKEN") and os.environ.get("LUNO_PAPER_TELEGRAM_CHAT_ID"))


def send_telegram_alert(message: str) -> None:
    """Send one message. Never include the bot token in errors or logs."""
    token = os.environ.get("LUNO_PAPER_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("LUNO_PAPER_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram alerts require LUNO_PAPER_TELEGRAM_BOT_TOKEN and LUNO_PAPER_TELEGRAM_CHAT_ID")
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "luno-myr-paper-research/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram notification failed with HTTP {exc.code}; check bot configuration") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram notification failed: {type(exc).__name__}") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Telegram rejected the notification; check bot and chat configuration")


def format_paper_alert(result: dict[str, Any], *, newly_halted: bool) -> str | None:
    lines = [f"Luno paper · MYR · {result.get('as_of_utc', '')}"]
    for event in result.get("events", []):
        action = event.get("action")
        asset = event.get("asset", "")
        if action == "PAPER_BUY":
            lines.append(f"BUY {asset}: qty {float(event.get('quantity', 0)):.8g} @ MYR {float(event.get('average_price_myr', 0)):,.4f}; stop MYR {float(event.get('stop_price_myr', 0)):,.4f}")
        elif action == "PAPER_SELL":
            lines.append(f"SELL {asset}: qty {float(event.get('quantity', 0)):.8g} @ MYR {float(event.get('average_price_myr', 0)):,.4f}; P&L MYR {float(event.get('realized_pnl_myr', 0)):,.2f}; {event.get('reason', '')}")
    if newly_halted:
        lines.append(f"RISK HALT: new entries disabled; drawdown {float(result.get('drawdown_pct', 0)):.2f}%")
    if len(lines) == 1:
        return None
    lines.append(f"Equity MYR {float(result.get('equity_myr', 0)):,.2f} · cash MYR {float(result.get('cash_myr', 0)):,.2f}")
    return "\n".join(lines)
