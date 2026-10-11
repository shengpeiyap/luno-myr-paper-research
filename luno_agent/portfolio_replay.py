"""Offline, multi-asset portfolio replay using only local OHLCV CSV files."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .backtest import _execution_price, load_candles_csv
from .indicators import momentum_signal
from .risk import drawdown_status, position_size_myr

MYR_TZ = timezone(timedelta(hours=8), name="MYT")
ASSETS = ("BTC", "ETH", "SOL", "XRP")
TIMEFRAMES = {"4h": 14_400, "1d": 86_400}


def run_portfolio_replay(
    data_dir: str | Path, *, timeframe: str, initial_equity_myr: float,
    stop_loss_pct: float, account_risk_pct: float, asset_exposure_pct: float,
    total_exposure_pct: float, max_entries_per_day: int,
    drawdown_warning_pct: float, drawdown_halt_pct: float,
    fee_pct_per_side: float, spread_pct_round_trip: float,
    slippage_pct_per_side: float,
) -> dict[str, Any]:
    """Replay four synchronized markets in one MYR account, without network calls."""
    if timeframe not in TIMEFRAMES:
        raise ValueError("timeframe must be 4h or 1d")
    if not math.isfinite(initial_equity_myr) or initial_equity_myr <= 0 or max_entries_per_day <= 0:
        raise ValueError("initial equity and daily entry limit must be positive")
    if not 0 < stop_loss_pct < 100:
        raise ValueError("stop loss must be between 0 and 100 percent")
    costs = (fee_pct_per_side, spread_pct_round_trip, slippage_pct_per_side)
    if any(not math.isfinite(value) or value < 0 for value in costs):
        raise ValueError("fees, spread, and slippage must be finite and non-negative")

    data_path = Path(data_dir)
    candles = {
        asset: load_candles_csv(data_path / f"{asset}-{timeframe}.csv", TIMEFRAMES[timeframe])
        for asset in ASSETS
    }
    timeline = [int(row["timestamp"]) for row in candles["BTC"]]
    if any([int(row["timestamp"]) for row in rows] != timeline for rows in candles.values()):
        raise ValueError("All four CSV files must have identical candle timestamps and coverage")

    cash = initial_equity_myr
    positions: dict[str, dict[str, float | int]] = {}
    pending: dict[str, str] = {}
    closes: dict[str, list[float]] = {asset: [] for asset in ASSETS}
    entries_by_day: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    total_fees = 0.0
    peak_equity = initial_equity_myr
    max_drawdown = 0.0
    halted = False
    halt_time: str | None = None
    round_trip_cost_pct = 2 * fee_pct_per_side + spread_pct_round_trip + 2 * slippage_pct_per_side

    def mark_equity(index: int) -> tuple[float, float]:
        equity = cash
        exposure = 0.0
        for asset, position in positions.items():
            mark = float(candles[asset][index]["close"])
            value = float(position["quantity"]) * _execution_price(
                mark, "sell", spread_pct_round_trip, slippage_pct_per_side
            ) * (1 - fee_pct_per_side / 100)
            equity += value
            exposure += float(position["quantity"]) * mark
        return equity, exposure

    def close_position(asset: str, raw_price: float, timestamp_ms: int, reason: str) -> None:
        nonlocal cash, total_fees
        position = positions.pop(asset, None)
        if position is None:
            return
        quantity = float(position["quantity"])
        fill = _execution_price(raw_price, "sell", spread_pct_round_trip, slippage_pct_per_side)
        fee = quantity * fill * fee_pct_per_side / 100
        proceeds = quantity * fill - fee
        entry_cost = float(position["entry_cost_myr"])
        cash += proceeds
        total_fees += fee
        trades.append({
            "asset": asset, "timeframe": timeframe,
            "entry_time_utc": datetime.fromtimestamp(int(position["entry_time_ms"]) / 1000, timezone.utc).isoformat(),
            "exit_time_utc": datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(),
            "entry_price_myr": round(float(position["entry_price_myr"]), 8),
            "exit_price_myr": round(fill, 8), "quantity": round(quantity, 12),
            "pnl_myr": round(proceeds - entry_cost, 4),
            "fees_myr": round(float(position["entry_fee_myr"]) + fee, 4),
            "exit_reason": reason,
        })

    for index, timestamp_ms in enumerate(timeline):
        day = datetime.fromtimestamp(timestamp_ms / 1000, MYR_TZ).date().isoformat()
        for asset in ASSETS:
            bar = candles[asset][index]
            action = pending.pop(asset, None)
            if action == "exit" and asset in positions:
                close_position(asset, float(bar["open"]), timestamp_ms, "SIGNAL_NEXT_OPEN")
            elif action == "entry" and asset not in positions and not halted:
                equity, exposure = mark_equity(index)
                position_values = {
                    name: float(pos["quantity"]) * float(candles[name][index]["open"])
                    for name, pos in positions.items()
                }
                open_asset_value = position_values.get(asset, 0.0)
                size = position_size_myr(
                    equity, float(bar["open"]), stop_loss_pct, account_risk_pct,
                    asset_exposure_pct, total_exposure_pct, open_asset_value, exposure,
                    round_trip_cost_pct=round_trip_cost_pct,
                )
                buy = _execution_price(float(bar["open"]), "buy", spread_pct_round_trip, slippage_pct_per_side)
                budget = min(float(size["position_myr"]), cash / (1 + fee_pct_per_side / 100))
                quantity = budget / buy if buy > 0 else 0.0
                if quantity > 0 and entries_by_day.get(day, 0) < max_entries_per_day:
                    quote = quantity * buy
                    fee = quote * fee_pct_per_side / 100
                    cash -= quote + fee
                    total_fees += fee
                    positions[asset] = {
                        "quantity": quantity, "entry_price_myr": buy,
                        "entry_cost_myr": quote + fee, "entry_fee_myr": fee,
                        "stop_price_myr": buy * (1 - stop_loss_pct / 100),
                        "entry_time_ms": timestamp_ms,
                    }
                    entries_by_day[day] = entries_by_day.get(day, 0) + 1

            position = positions.get(asset)
            if position:
                if float(bar["low"]) <= float(position["stop_price_myr"]):
                    close_position(asset, min(float(bar["open"]), float(position["stop_price_myr"])),
                                   timestamp_ms, "STOP_LOSS")
            closes[asset].append(float(bar["close"]))

        equity, exposure = mark_equity(index)
        peak_equity = max(peak_equity, equity)
        dd = drawdown_status(equity, peak_equity, drawdown_warning_pct, drawdown_halt_pct)
        max_drawdown = max(max_drawdown, float(dd["drawdown_pct"]))
        if dd["state"] == "HALT_NEW_ENTRIES" and not halted:
            halted = True
            halt_time = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()
        equity_curve.append({
            "timestamp_ms": timestamp_ms, "equity_myr": round(equity, 4),
            "cash_myr": round(cash, 4), "crypto_exposure_myr": round(exposure, 4),
            "drawdown_pct": round(float(dd["drawdown_pct"]), 4),
        })

        if index >= 59:
            for asset in ASSETS:
                signal = momentum_signal(closes[asset])["signal"]
                if asset in positions and signal == "EXIT_OR_AVOID_CANDIDATE":
                    pending[asset] = "exit"
                elif not halted and asset not in positions and signal == "BUY_CANDIDATE":
                    pending[asset] = "entry"

    final_time = timeline[-1]
    for asset in list(positions):
        close_position(asset, float(candles[asset][-1]["close"]), final_time, "END_OF_DATA")
    ending_equity = cash
    equity_curve[-1]["equity_myr"] = round(ending_equity, 4)
    equity_curve[-1]["cash_myr"] = round(cash, 4)
    equity_curve[-1]["crypto_exposure_myr"] = 0.0
    wins = sum(trade["pnl_myr"] > 0 for trade in trades)
    return {
        "research_only": True, "execution": "offline_ohlcv_next_open_estimated_costs",
        "network_access": False, "timeframe": timeframe, "assets": list(ASSETS),
        "candles_per_asset": len(timeline), "first_timestamp_ms": timeline[0],
        "last_timestamp_ms": timeline[-1], "initial_equity_myr": round(initial_equity_myr, 2),
        "ending_equity_myr": round(ending_equity, 2),
        "return_pct": round((ending_equity / initial_equity_myr - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown, 4), "halted_at_drawdown_limit": halted,
        "halt_time_utc": halt_time, "trade_count": len(trades),
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "fees_myr": round(total_fees, 4),
        "cost_assumptions_pct": {"fee_per_side": fee_pct_per_side,
                                 "spread_round_trip": spread_pct_round_trip,
                                 "slippage_per_side": slippage_pct_per_side},
        "trades": trades, "equity_curve": equity_curve,
    }
