"""Dependency-free indicators for research; inputs must be oldest-to-newest."""

from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    output: list[float | None] = [None] * len(values)
    if len(values) < period:
        return output
    current = sum(values[:period]) / period
    output[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (1 - alpha)
        output[index] = current
    return output


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, float | None, float | None]:
    fast_ema = ema_series(values, fast)
    slow_ema = ema_series(values, slow)
    macd_line: list[float | None] = [
        (a - b) if a is not None and b is not None else None
        for a, b in zip(fast_ema, slow_ema)
    ]
    valid_start = next((i for i, value in enumerate(macd_line) if value is not None), len(values))
    valid_macd = [value for value in macd_line[valid_start:] if value is not None]
    signal_values = ema_series(valid_macd, signal)
    signal_line = signal_values[-1] if signal_values else None
    line = macd_line[-1] if macd_line else None
    histogram = line - signal_line if line is not None and signal_line is not None else None
    return line, signal_line, histogram


def momentum_signal(closes: list[float], ma_period: int = 50) -> dict[str, object]:
    """Explainable baseline signal: MA trend filter plus MACD line/signal relation.

    This is a research signal only, not a validated or profitable strategy.
    """
    if len(closes) < max(ma_period, 35):
        return {"signal": "INSUFFICIENT_DATA", "ma": None, "macd": None, "macd_signal": None, "histogram": None}
    average = sma(closes, ma_period)
    line, signal_line, histogram = macd(closes)
    if average is None or line is None or signal_line is None:
        label = "INSUFFICIENT_DATA"
    elif closes[-1] > average and line > signal_line:
        label = "BUY_CANDIDATE"
    elif closes[-1] < average and line < signal_line:
        label = "EXIT_OR_AVOID_CANDIDATE"
    else:
        label = "HOLD_OR_WAIT"
    return {"signal": label, "ma": average, "macd": line, "macd_signal": signal_line, "histogram": histogram}
