"""Position sizing and account-level circuit breakers for paper research."""

from __future__ import annotations


def position_size_myr(
    equity_myr: float,
    entry_price_myr: float,
    stop_loss_pct: float,
    account_risk_pct: float = 0.5,
    asset_exposure_pct: float = 25.0,
    total_exposure_pct: float = 80.0,
    current_asset_value_myr: float = 0.0,
    current_total_exposure_myr: float = 0.0,
    round_trip_cost_pct: float = 0.0,
) -> dict[str, float]:
    """Return maximum new position in MYR, capped by planned loss and exposure.

    Fees/slippage are conservatively added to the stop distance. Values are
    percentages, e.g. 0.5 means 0.5% of equity.
    """
    if equity_myr <= 0 or entry_price_myr <= 0:
        raise ValueError("equity and entry price must be positive")
    if not 0 < stop_loss_pct < 100 or account_risk_pct <= 0:
        raise ValueError("invalid stop loss or account risk")
    if min(current_asset_value_myr, current_total_exposure_myr, round_trip_cost_pct) < 0:
        raise ValueError("exposure and costs cannot be negative")
    risk_budget = equity_myr * account_risk_pct / 100
    loss_fraction = (stop_loss_pct + round_trip_cost_pct) / 100
    risk_limited = risk_budget / loss_fraction
    asset_room = max(0.0, equity_myr * asset_exposure_pct / 100 - current_asset_value_myr)
    total_room = max(0.0, equity_myr * total_exposure_pct / 100 - current_total_exposure_myr)
    notional = max(0.0, min(risk_limited, asset_room, total_room))
    return {
        "risk_budget_myr": risk_budget,
        "risk_limited_position_myr": risk_limited,
        "asset_exposure_room_myr": asset_room,
        "total_exposure_room_myr": total_room,
        "position_myr": notional,
        "quantity": notional / entry_price_myr,
        "planned_loss_myr": notional * loss_fraction,
    }


def drawdown_status(equity_myr: float, peak_equity_myr: float, warning_pct: float = 10.0, halt_pct: float = 20.0) -> dict[str, object]:
    if peak_equity_myr <= 0 or equity_myr < 0:
        raise ValueError("equity must be non-negative and peak equity must be positive")
    drawdown_pct = max(0.0, (peak_equity_myr - equity_myr) / peak_equity_myr * 100)
    if drawdown_pct >= halt_pct:
        state = "HALT_NEW_ENTRIES"
    elif drawdown_pct >= warning_pct:
        state = "WARNING"
    else:
        state = "NORMAL"
    return {"drawdown_pct": drawdown_pct, "state": state}
