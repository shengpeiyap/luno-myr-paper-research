"""Persistent paper execution using public Luno candles and order books only."""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .backtest import load_candles_csv, validate_candles
from .indicators import momentum_signal
from .risk import drawdown_status, position_size_myr

MYR_TZ = timezone(timedelta(hours=8), name="MYT")
ASSET_PAIRS = {"BTC": "XBTMYR", "ETH": "ETHMYR", "SOL": "SOLMYR", "XRP": "XRPMYR"}
TIMEFRAMES = {"4h": 14_400, "1d": 86_400}


class PaperSafetyError(RuntimeError):
    """Raised when paper execution cannot safely reconcile its inputs/state."""


def acquire_lock(state_path: Path) -> int:
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PaperSafetyError(f"Paper runner lock exists: {lock_path}; ensure no runner is active before removing it") from exc


def release_lock(state_path: Path, descriptor: int) -> None:
    os.close(descriptor)
    state_path.with_suffix(state_path.suffix + ".lock").unlink(missing_ok=True)


def _new_state(starting_equity_myr: float) -> dict[str, Any]:
    if not math.isfinite(starting_equity_myr) or starting_equity_myr <= 0:
        raise PaperSafetyError("Starting paper equity must be a positive MYR amount")
    return {
        "schema_version": 1, "mode": "paper", "base_currency": "MYR",
        "initial_equity_myr": starting_equity_myr, "cash_myr": starting_equity_myr,
        "peak_equity_myr": starting_equity_myr, "halted_at_drawdown_limit": False,
        "manual_review_required": False, "positions": {}, "trades": [],
        "entries_by_myr_day": {}, "last_processed_candle_ms": {},
    }


def load_state(state_path: Path, starting_equity_myr: float) -> dict[str, Any]:
    if not state_path.exists():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        return _new_state(starting_equity_myr)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperSafetyError(f"Cannot read paper ledger safely: {exc}") from exc
    required = {"schema_version", "mode", "base_currency", "cash_myr", "peak_equity_myr",
                "halted_at_drawdown_limit", "manual_review_required", "positions", "trades",
                "entries_by_myr_day", "last_processed_candle_ms"}
    if not isinstance(state, dict) or not required.issubset(state):
        raise PaperSafetyError("Paper ledger has an unknown or incomplete schema; refusing to trade")
    if state["schema_version"] != 1 or state["mode"] != "paper" or state["base_currency"] != "MYR":
        raise PaperSafetyError("Paper ledger mode/currency/version mismatch; refusing to trade")
    if (not isinstance(state["positions"], dict) or not isinstance(state["trades"], list)
            or not isinstance(state["entries_by_myr_day"], dict)
            or not isinstance(state["last_processed_candle_ms"], dict)
            or not isinstance(state["halted_at_drawdown_limit"], bool)
            or not isinstance(state["manual_review_required"], bool)):
        raise PaperSafetyError("Paper ledger positions/trades are malformed")
    try:
        if not math.isfinite(float(state["cash_myr"])) or float(state["cash_myr"]) < 0:
            raise ValueError
        if not math.isfinite(float(state["peak_equity_myr"])) or float(state["peak_equity_myr"]) <= 0:
            raise ValueError
        for asset, position in state["positions"].items():
            if asset not in ASSET_PAIRS or not isinstance(position, dict):
                raise ValueError
            quantity = float(position["quantity"])
            entry_price = float(position["entry_price_myr"])
            stop_price = float(position["stop_price_myr"])
            entry_cost = float(position["entry_cost_remaining_myr"])
            if (not all(math.isfinite(value) for value in (quantity, entry_price, stop_price, entry_cost))
                    or quantity <= 0 or entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price
                    or entry_cost <= 0 or position["timeframe"] not in TIMEFRAMES):
                raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise PaperSafetyError("Paper ledger contains invalid balances or positions") from exc
    return state


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp_path, state_path)


def _ladder_fill(levels: list[dict[str, Any]], quantity: float, *, buy: bool,
                 quote_budget_myr: float | None = None) -> tuple[float, float, float]:
    """Walk up to 100 visible levels; return filled quantity, VWAP and quote."""
    remaining = quantity
    filled = 0.0
    quote = 0.0
    for level in levels:
        try:
            price, available = float(level["price"]), float(level.get("volume", level.get("quantity")))
        except (TypeError, ValueError, KeyError) as exc:
            raise PaperSafetyError("Malformed order-book level") from exc
        if not math.isfinite(price) or not math.isfinite(available) or price <= 0 or available <= 0:
            continue
        take = min(remaining, available)
        if buy and quote_budget_myr is not None:
            take = min(take, max(0.0, quote_budget_myr - quote) / price)
        if take <= 0:
            break
        filled += take
        quote += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    return filled, quote / filled if filled > 0 else 0.0, quote


def _book_view(book: dict[str, Any], now_ms: int, *, max_age_seconds: int,
               max_spread_pct: float) -> dict[str, Any]:
    bids, asks = book.get("bids"), book.get("asks")
    timestamp = book.get("timestamp")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise PaperSafetyError("Order book is empty")
    try:
        bid, ask = float(bids[0]["price"]), float(asks[0]["price"])
        age = (now_ms - int(timestamp)) / 1000
    except (TypeError, ValueError, KeyError) as exc:
        raise PaperSafetyError("Order book is missing a valid price or timestamp") from exc
    if age < -5 or age > max_age_seconds:
        raise PaperSafetyError(f"Order book is stale or has a future timestamp ({age:.1f}s)")
    if bid <= 0 or ask <= bid:
        raise PaperSafetyError("Order book prices are invalid or crossed")
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    return {"bid": bid, "ask": ask, "age_seconds": age, "spread_pct": spread_pct,
            "spread_acceptable": spread_pct <= max_spread_pct, "bids": bids, "asks": asks}


def _mark_equity(state: dict[str, Any], books: dict[str, dict[str, Any]], fee_pct: float) -> tuple[float, float]:
    equity = float(state["cash_myr"])
    exposure = 0.0
    for asset, position in state["positions"].items():
        if asset not in books:
            raise PaperSafetyError(f"No fresh order book for open {asset} position; pausing all paper actions")
        value = float(position["quantity"]) * books[asset]["bid"]
        exposure += value
        # Mark positions at bid and reserve a taker fee for liquidation.
        equity += value * (1 - fee_pct / 100)
    return equity, exposure


def _record_exit(state: dict[str, Any], asset: str, position: dict[str, Any], book: dict[str, Any],
                 fee_pct: float, reason: str, now_ms: int) -> dict[str, Any] | None:
    old_quantity = float(position["quantity"])
    filled, average, quote = _ladder_fill(book["bids"], old_quantity, buy=False)
    if filled <= 1e-12:
        return {"asset": asset, "action": "EXIT_WAITING_FOR_DEPTH", "reason": reason}
    fee = quote * fee_pct / 100
    fraction = min(1.0, filled / old_quantity)
    allocated_cost = float(position["entry_cost_remaining_myr"]) * fraction
    pnl = quote - fee - allocated_cost
    state["cash_myr"] += quote - fee
    position["quantity"] = max(0.0, old_quantity - filled)
    position["entry_cost_remaining_myr"] = max(0.0, float(position["entry_cost_remaining_myr"]) - allocated_cost)
    state["trades"].append({
        "asset": asset, "timeframe": position["timeframe"], "side": "SELL",
        "timestamp_utc": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
        "quantity": filled, "price_myr": average, "fee_myr": fee,
        "realized_pnl_myr": pnl, "reason": reason,
        "complete_exit": position["quantity"] <= 1e-12,
    })
    if position["quantity"] <= 1e-12:
        del state["positions"][asset]
    elif reason == "SIGNAL_EXIT":
        position["exit_pending"] = True
    return {"asset": asset, "action": "PAPER_SELL", "quantity": filled,
            "average_price_myr": average, "fee_myr": fee, "realized_pnl_myr": pnl,
            "reason": reason, "remaining_quantity": position.get("quantity", 0.0)}


async def run_paper_cycle(client: Any, state: dict[str, Any], *, data_dir: Path, timeframe: str,
                         data_source: str, fee_pct_per_side: float, risk: dict[str, Any], activity: dict[str, Any],
                         execution: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    """Process current stop conditions and new closed-bar signals without exchange writes."""
    now_ms = now_ms or int(time.time() * 1000)
    if not math.isfinite(fee_pct_per_side) or not 0 <= fee_pct_per_side < 100:
        raise PaperSafetyError("Paper fee must be finite and between 0 and 100 percent per side")
    markets = await client.markets()
    available = {}
    for market in markets:
        base = "BTC" if market.get("base_currency") == "XBT" else market.get("base_currency")
        if base in ASSET_PAIRS and market.get("counter_currency") == "MYR" and market.get("trading_status") == "ACTIVE":
            available[base] = market

    events: list[dict[str, Any]] = []
    books: dict[str, dict[str, Any]] = {}
    for asset in sorted(set(available) | set(state["positions"])):
        if asset not in available:
            if asset in state["positions"]:
                raise PaperSafetyError(f"Open position {asset} has no active MYR market; stopping runner")
            continue
        raw = await client.order_book_top(available[asset]["market_id"])
        try:
            view = _book_view(raw, now_ms, max_age_seconds=int(execution["max_order_book_age_seconds"]),
                              max_spread_pct=float(execution["max_spread_pct"]))
            books[asset] = view
            if not view["spread_acceptable"]:
                events.append({"asset": asset, "action": "WIDE_SPREAD", "spread_pct": view["spread_pct"]})
        except PaperSafetyError as exc:
            if asset in state["positions"]:
                raise PaperSafetyError(f"{asset} open position: {exc}; pausing all paper actions") from exc
            events.append({"asset": asset, "action": "SKIP_MARKET", "reason": str(exc)})

    exited_this_cycle: set[str] = set()
    # Stops are monitored from the current bid every cycle, even between bars.
    for asset in list(state["positions"]):
        position = state["positions"][asset]
        book = books[asset]
        stop_hit = book["bid"] <= float(position["stop_price_myr"])
        if position.get("stop_triggered") or position.get("exit_pending") or stop_hit:
            reason = "STOP_LOSS" if position.get("stop_triggered") or stop_hit else "SIGNAL_EXIT"
            if reason == "STOP_LOSS":
                position["stop_triggered"] = True
            event = _record_exit(state, asset, position, book, fee_pct_per_side, reason, now_ms)
            if event:
                events.append(event)
                if event.get("action") == "PAPER_SELL":
                    exited_this_cycle.add(asset)

    fresh_signals: dict[str, tuple[list[dict[str, float | int]], dict[str, Any]]] = {}
    duration_ms = TIMEFRAMES[timeframe] * 1000
    for asset in sorted(available):
        try:
            duration = TIMEFRAMES[timeframe]
            if data_source == "luno":
                pair = available[asset].get("market_id")
                if not isinstance(pair, str) or not pair:
                    raise ValueError("exchange did not provide an exact market ID")
                raw_candles = await client.candles(pair, duration, now_ms - 1000 * duration * 1000)
                candles = validate_candles(raw_candles, duration)
            else:
                file_path = data_dir / f"{asset}-{timeframe}.csv"
                candles = load_candles_csv(file_path, duration)
            closed = [row for row in candles if int(row["timestamp"]) + duration_ms <= now_ms]
            if len(closed) < 60:
                raise ValueError("fewer than 60 closed candles")
            age_ms = now_ms - (int(closed[-1]["timestamp"]) + duration_ms)
            if age_ms > duration_ms * 1.25:
                raise ValueError(f"last closed candle is stale ({age_ms / 1000:.0f}s old)")
            signal = momentum_signal([float(row["close"]) for row in closed])
            fresh_signals[asset] = (closed, signal)
        except Exception as exc:
            events.append({"asset": asset, "action": "SKIP_SIGNAL", "reason": str(exc)})

    for asset, (candles, signal) in fresh_signals.items():
        if asset not in books:
            continue
        candle_ms = int(candles[-1]["timestamp"])
        previous_ms = int(state["last_processed_candle_ms"].get(asset, 0))
        if candle_ms <= previous_ms:
            continue
        state["last_processed_candle_ms"][asset] = candle_ms
        if asset in exited_this_cycle:
            events.append({"asset": asset, "action": "NO_ENTRY", "reason": "wait_for_next_closed_candle_after_exit"})
            continue
        position = state["positions"].get(asset)
        if position:
            if not position.get("stop_triggered") and signal["signal"] == "EXIT_OR_AVOID_CANDIDATE":
                event = _record_exit(state, asset, position, books[asset], fee_pct_per_side, "SIGNAL_EXIT", now_ms)
                if event:
                    events.append(event)
            continue
        if state["halted_at_drawdown_limit"] or state["manual_review_required"] or signal["signal"] != "BUY_CANDIDATE":
            events.append({"asset": asset, "action": "NO_ENTRY", "signal": signal["signal"],
                           "reason": "drawdown_halt" if state["halted_at_drawdown_limit"] or state["manual_review_required"] else "signal_filter"})
            continue
        if not books[asset]["spread_acceptable"]:
            events.append({"asset": asset, "action": "NO_ENTRY", "reason": "spread_above_cap",
                           "spread_pct": books[asset]["spread_pct"]})
            continue

        day = datetime.fromtimestamp(now_ms / 1000, MYR_TZ).date().isoformat()
        if int(state["entries_by_myr_day"].get(day, 0)) >= int(activity["max_new_entries_per_myr_calendar_day"]):
            events.append({"asset": asset, "action": "NO_ENTRY", "reason": "daily_entry_limit"})
            continue

        equity, total_exposure = _mark_equity(state, books, fee_pct_per_side)
        if drawdown_status(equity, float(state["peak_equity_myr"]),
                           float(risk["account_drawdown_warning_pct"]),
                           float(risk["account_drawdown_halt_pct"]))["state"] == "HALT_NEW_ENTRIES":
            state["halted_at_drawdown_limit"] = True
            state["manual_review_required"] = True
            state["halted_at_utc"] = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()
            events.append({"asset": asset, "action": "NO_ENTRY", "reason": "drawdown_halt"})
            continue

        current_asset = state["positions"].get(asset)
        current_value = float(current_asset["quantity"]) * books[asset]["bid"] if current_asset else 0.0
        spread = books[asset]["spread_pct"]
        size = position_size_myr(
            equity, books[asset]["ask"], float(risk["paper_default_stop_loss_pct"]),
            float(risk["max_account_risk_per_trade_pct"]), float(risk["max_asset_exposure_pct"]),
            float(risk["max_total_crypto_exposure_pct"]), current_value, total_exposure,
            round_trip_cost_pct=2 * fee_pct_per_side + spread,
        )
        quote_budget = min(float(size["position_myr"]), float(state["cash_myr"])) / (1 + fee_pct_per_side / 100)
        target_quantity = quote_budget / books[asset]["ask"] if books[asset]["ask"] else 0.0
        filled, average, quote = _ladder_fill(books[asset]["asks"], target_quantity, buy=True,
                                              quote_budget_myr=quote_budget)
        min_volume = float(available[asset].get("min_volume", 0) or 0)
        if filled < min_volume or quote <= 0:
            events.append({"asset": asset, "action": "NO_ENTRY", "reason": "insufficient_order_book_depth_or_below_minimum"})
            continue
        fee = quote * fee_pct_per_side / 100
        state["cash_myr"] -= quote + fee
        stop_price = average * (1 - float(risk["paper_default_stop_loss_pct"]) / 100)
        state["positions"][asset] = {
            "quantity": filled, "entry_price_myr": average, "entry_cost_remaining_myr": quote + fee,
            "entry_fee_myr": fee, "stop_price_myr": stop_price, "timeframe": timeframe,
            "opened_at_utc": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
            "entry_candle_ms": candle_ms, "stop_triggered": False,
        }
        state["entries_by_myr_day"][day] = int(state["entries_by_myr_day"].get(day, 0)) + 1
        state["trades"].append({"asset": asset, "timeframe": timeframe, "side": "BUY",
                                "timestamp_utc": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(),
                                "quantity": filled, "price_myr": average, "fee_myr": fee,
                                "stop_price_myr": stop_price, "reason": "MA_MACD_BUY_CANDIDATE"})
        events.append({"asset": asset, "action": "PAPER_BUY", "quantity": filled,
                       "average_price_myr": average, "fee_myr": fee, "stop_price_myr": stop_price,
                       "signal": signal["signal"], "spread_pct": spread})

    equity, exposure = _mark_equity(state, books, fee_pct_per_side)
    state["peak_equity_myr"] = max(float(state["peak_equity_myr"]), equity)
    dd = drawdown_status(equity, float(state["peak_equity_myr"]),
                         float(risk["account_drawdown_warning_pct"]),
                         float(risk["account_drawdown_halt_pct"]))
    if dd["state"] == "HALT_NEW_ENTRIES":
        state["halted_at_drawdown_limit"] = True
        state["manual_review_required"] = True
    state["updated_at_utc"] = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()
    state["trades"] = state["trades"][-5000:]
    return {"mode": "paper", "as_of_utc": state["updated_at_utc"], "events": events,
            "equity_myr": round(equity, 2), "cash_myr": round(float(state["cash_myr"]), 2),
            "crypto_exposure_myr": round(exposure, 2), "drawdown_pct": dd["drawdown_pct"],
            "drawdown_state": dd["state"], "open_positions": len(state["positions"]),
            "halted_at_drawdown_limit": state["halted_at_drawdown_limit"]}
