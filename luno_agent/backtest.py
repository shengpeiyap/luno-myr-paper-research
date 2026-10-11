"""Conservative, single-market OHLCV backtest for the paper strategy."""

from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .indicators import momentum_signal
from .risk import drawdown_status, position_size_myr

MYR_TZ = timezone(timedelta(hours=8), name="MYT")
REQUIRED_CSV_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


def _timestamp_ms(value: str) -> int:
    try:
        numeric = float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("ISO timestamps must include a timezone")
        return int(parsed.timestamp() * 1000)
    if not math.isfinite(numeric):
        raise ValueError("timestamp must be finite")
    # Unix seconds are below 1e12; Luno's API uses milliseconds.
    return int(numeric * 1000 if abs(numeric) < 1_000_000_000_000 else numeric)


def load_candles_csv(path: str | Path, timeframe_seconds: int) -> list[dict[str, float | int]]:
    """Load a strict timestamp/open/high/low/close/volume CSV and reject gaps."""
    candles: list[dict[str, float | int]] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = {column.strip().lower() for column in (reader.fieldnames or [])}
        missing = REQUIRED_CSV_COLUMNS - columns
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        for line_number, raw in enumerate(reader, start=2):
            row = {key.strip().lower(): value for key, value in raw.items() if key}
            try:
                candle = {
                    "timestamp": _timestamp_ms(row["timestamp"].strip()),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            except (AttributeError, TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Invalid candle on CSV line {line_number}: {exc}") from exc
            prices = [candle[key] for key in ("open", "high", "low", "close", "volume")]
            if any(not math.isfinite(float(value)) for value in prices):
                raise ValueError(f"Non-finite candle value on CSV line {line_number}")
            if min(candle[key] for key in ("open", "high", "low", "close")) <= 0 or candle["volume"] < 0:
                raise ValueError(f"Invalid price or volume on CSV line {line_number}")
            if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
                raise ValueError(f"High is inconsistent on CSV line {line_number}")
            if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
                raise ValueError(f"Low is inconsistent on CSV line {line_number}")
            candles.append(candle)

    if len(candles) < 60:
        raise ValueError("At least 60 candles are required for indicator warm-up and evaluation")
    timestamps = [int(candle["timestamp"]) for candle in candles]
    if timestamps != sorted(set(timestamps)):
        raise ValueError("Candles must be strictly increasing with no duplicate timestamps")
    expected_ms = timeframe_seconds * 1000
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous != expected_ms:
            raise ValueError("Candle timestamps contain a gap or do not match the selected timeframe")
    return candles


def _execution_price(raw_price: float, side: str, spread_pct: float, slippage_pct: float) -> float:
    half_spread = spread_pct / 200
    impact = (half_spread + slippage_pct / 100) / 100
    return raw_price * (1 + impact if side == "buy" else 1 - impact)


def _equity(cash: float, quantity: float, mark: float, fee_pct: float,
            spread_pct: float, slippage_pct: float) -> float:
    if quantity <= 0:
        return cash
    liquidation = _execution_price(mark, "sell", spread_pct, slippage_pct)
    return cash + quantity * liquidation * (1 - fee_pct / 100)


def run_backtest(
    candles: list[dict[str, Any]], *, asset: str, timeframe: str, initial_equity_myr: float,
    stop_loss_pct: float, account_risk_pct: float, asset_exposure_pct: float,
    total_exposure_pct: float, max_entries_per_day: int, drawdown_warning_pct: float,
    drawdown_halt_pct: float, fee_pct_per_side: float, spread_pct_round_trip: float,
    slippage_pct_per_side: float,
) -> dict[str, Any]:
    """Simulate one asset; signals use closed bars and fills use the next open."""
    if not candles or initial_equity_myr <= 0:
        raise ValueError("candles and positive initial MYR equity are required")
    if not 0 < stop_loss_pct < 100 or max_entries_per_day <= 0:
        raise ValueError("stop loss and daily entry limit are invalid")
    if min(fee_pct_per_side, spread_pct_round_trip, slippage_pct_per_side) < 0:
        raise ValueError("fees, spread, and slippage cannot be negative")
    if any(not math.isfinite(value) for value in (fee_pct_per_side, spread_pct_round_trip, slippage_pct_per_side)):
        raise ValueError("fees, spread, and slippage must be finite")

    cash = initial_equity_myr
    quantity = 0.0
    entry_price = 0.0
    entry_fee = 0.0
    entry_cash_debit = 0.0
    entry_time_ms = 0
    stop_price = 0.0
    peak = initial_equity_myr
    max_drawdown = 0.0
    halted = False
    pending: str | None = None
    entries_by_day: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    total_fees = 0.0
    round_trip_cost_pct = (2 * fee_pct_per_side) + spread_pct_round_trip + (2 * slippage_pct_per_side)

    def close_position(raw_price: float, timestamp_ms: int, reason: str) -> None:
        nonlocal cash, quantity, total_fees
        if quantity <= 0:
            return
        fill = _execution_price(raw_price, "sell", spread_pct_round_trip, slippage_pct_per_side)
        exit_fee = quantity * fill * fee_pct_per_side / 100
        proceeds = quantity * fill - exit_fee
        pnl = proceeds - entry_cash_debit
        total_fees += exit_fee
        trades.append({
            "asset": asset, "timeframe": timeframe, "stop_loss_pct": stop_loss_pct,
            "entry_time_utc": datetime.fromtimestamp(entry_time_ms / 1000, timezone.utc).isoformat(),
            "exit_time_utc": datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(),
            "entry_price_myr": round(entry_price, 8), "exit_price_myr": round(fill, 8),
            "quantity": round(quantity, 12), "pnl_myr": round(pnl, 4),
            "fees_myr": round(entry_fee + exit_fee, 4), "exit_reason": reason,
        })
        cash += proceeds
        quantity = 0.0

    closes: list[float] = []
    for index, candle in enumerate(candles):
        timestamp_ms = int(candle["timestamp"])
        bar_open = float(candle["open"])
        bar_high = float(candle["high"])
        bar_low = float(candle["low"])
        bar_close = float(candle["close"])
        day = datetime.fromtimestamp(timestamp_ms / 1000, MYR_TZ).date().isoformat()

        if pending == "exit" and quantity > 0:
            close_position(bar_open, timestamp_ms, "SIGNAL_NEXT_OPEN")
        elif pending == "entry" and quantity == 0 and not halted and entries_by_day.get(day, 0) < max_entries_per_day:
            equity_at_open = _equity(cash, quantity, bar_open, fee_pct_per_side,
                                     spread_pct_round_trip, slippage_pct_per_side)
            sizing = position_size_myr(
                equity_at_open, bar_open, stop_loss_pct, account_risk_pct,
                asset_exposure_pct, total_exposure_pct,
                round_trip_cost_pct=round_trip_cost_pct,
            )
            buy_price = _execution_price(bar_open, "buy", spread_pct_round_trip, slippage_pct_per_side)
            buy_fee_rate = fee_pct_per_side / 100
            max_affordable = cash / (buy_price * (1 + buy_fee_rate))
            quantity = min(float(sizing["quantity"]), max_affordable)
            if quantity > 0:
                entry_price = buy_price
                entry_fee = quantity * buy_price * buy_fee_rate
                entry_cash_debit = quantity * buy_price + entry_fee
                cash -= entry_cash_debit
                total_fees += entry_fee
                entry_time_ms = timestamp_ms
                stop_price = entry_price * (1 - stop_loss_pct / 100)
                entries_by_day[day] = entries_by_day.get(day, 0) + 1

        pending = None

        # If a bar opens through the stop, use its open; otherwise use the stop
        # level. This avoids granting a better fill than the OHLC data supports.
        if quantity > 0 and bar_low <= stop_price:
            close_position(min(bar_open, stop_price), timestamp_ms, "STOP_LOSS")

        closes.append(bar_close)
        marked_equity = _equity(cash, quantity, bar_close, fee_pct_per_side,
                                spread_pct_round_trip, slippage_pct_per_side)
        peak = max(peak, marked_equity)
        drawdown = max(0.0, (peak - marked_equity) / peak * 100)
        max_drawdown = max(max_drawdown, drawdown)
        if drawdown_status(marked_equity, peak, drawdown_warning_pct, drawdown_halt_pct)["state"] == "HALT_NEW_ENTRIES":
            halted = True

        if index >= 59 and not halted:
            signal = momentum_signal(closes)
            if quantity > 0 and signal["signal"] == "EXIT_OR_AVOID_CANDIDATE":
                pending = "exit"
            elif quantity == 0 and signal["signal"] == "BUY_CANDIDATE":
                pending = "entry"

    if quantity > 0:
        last = candles[-1]
        close_position(float(last["close"]), int(last["timestamp"]), "END_OF_DATA")
    ending_equity = cash
    wins = sum(float(trade["pnl_myr"]) > 0 for trade in trades)
    return {
        "asset": asset, "timeframe": timeframe, "stop_loss_pct": stop_loss_pct,
        "initial_equity_myr": round(initial_equity_myr, 2),
        "ending_equity_myr": round(ending_equity, 2),
        "return_pct": round((ending_equity / initial_equity_myr - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "trade_count": len(trades), "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "fees_myr": round(total_fees, 4), "halted_at_drawdown_limit": halted,
        "cost_assumptions_pct": {
            "fee_per_side": fee_pct_per_side, "spread_round_trip": spread_pct_round_trip,
            "slippage_per_side": slippage_pct_per_side,
        },
        "trades": trades,
    }
