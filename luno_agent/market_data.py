"""Download completed Luno MYR candles and save aligned local OHLCV CSV files."""

from __future__ import annotations

import base64
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .backtest import validate_candles

PAIRS = {"BTC": "XBTMYR", "ETH": "ETHMYR", "SOL": "SOLMYR", "XRP": "XRPMYR"}
TIMEFRAMES = {"4h": 14_400, "1d": 86_400}
API_URL = "https://api.luno.com/api/exchange/1/candles"
MAX_CANDLES_PER_REQUEST = 1000


def _fetch_page(pair: str, duration: int, since_ms: int, timeout: float = 20.0) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"pair": pair, "duration": duration, "since": since_ms})
    request = urllib.request.Request(
        f"{API_URL}?{query}", headers={"Accept": "application/json", "User-Agent": "luno-myr-paper-research/0.1"}
    )
    key_id, secret = os.environ.get("LUNO_API_KEY_ID"), os.environ.get("LUNO_API_SECRET")
    if bool(key_id) != bool(secret):
        raise RuntimeError("Set both LUNO_API_KEY_ID and LUNO_API_SECRET, or leave both unset")
    if key_id and secret:
        token = base64.b64encode(f"{key_id}:{secret}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                "Luno rejected the candle request (401). Its API reference marks candles as no-permission, "
                "but this endpoint may require a read-only key for your request; if you have one, set "
                "LUNO_API_KEY_ID and LUNO_API_SECRET in this PowerShell session and retry."
            ) from exc
        raise RuntimeError(f"Luno candle request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not retrieve Luno candles: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise RuntimeError("Luno returned an unexpected candle response")
    return payload["candles"]


def fetch_pair_candles(pair: str, duration: int, since_ms: int, end_ms: int) -> list[dict[str, float | int]]:
    """Page through at most 1000 candles per call and retain closed bars only."""
    rows: dict[int, dict[str, float | int]] = {}
    cursor = since_ms
    while cursor < end_ms:
        page = _fetch_page(pair, duration, cursor)
        if not page:
            break
        for item in page:
            try:
                timestamp = int(item["timestamp"])
                row = {"timestamp": timestamp, **{key: float(item[key]) for key in ("open", "high", "low", "close", "volume")}}
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Luno returned a malformed candle for {pair}") from exc
            if timestamp >= since_ms and timestamp + duration * 1000 <= end_ms:
                rows[timestamp] = row
        newest = max(int(item["timestamp"]) for item in page if "timestamp" in item)
        next_cursor = newest + duration * 1000
        if next_cursor <= cursor:
            raise RuntimeError(f"Luno candle pagination did not advance for {pair}")
        cursor = next_cursor
        if len(page) < MAX_CANDLES_PER_REQUEST or newest + duration * 1000 >= end_ms:
            break
        time.sleep(0.15)
    return [rows[timestamp] for timestamp in sorted(rows)]


def download_luno_csvs(data_dir: str | Path, timeframe: str, days: int) -> dict[str, Any]:
    """Fetch all supported MYR markets, align their common timestamps, then write CSVs."""
    if timeframe not in TIMEFRAMES:
        raise ValueError("timeframe must be 4h or 1d")
    if days <= 0:
        raise ValueError("days must be a positive whole number")
    duration = TIMEFRAMES[timeframe]
    now_ms = int(time.time() * 1000)
    end_ms = now_ms // (duration * 1000) * duration * 1000
    since_ms = end_ms - days * 86_400_000
    fetched = {
        asset: fetch_pair_candles(pair, duration, since_ms, end_ms)
        for asset, pair in PAIRS.items()
    }
    common = set(int(row["timestamp"]) for row in fetched["BTC"])
    for rows in fetched.values():
        common.intersection_update(int(row["timestamp"]) for row in rows)
    timestamps = sorted(common)
    if len(timestamps) < 60:
        counts = ", ".join(f"{asset}={len(rows)}" for asset, rows in fetched.items())
        raise RuntimeError(f"Fewer than 60 aligned candles available ({counts}); no CSVs were written")
    aligned = {
        asset: validate_candles([row for row in rows if int(row["timestamp"]) in common], duration)
        for asset, rows in fetched.items()
    }

    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for asset, rows in aligned.items():
            target = directory / f"{asset}-{timeframe}.csv"
            temporary = target.with_suffix(target.suffix + ".tmp")
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=("timestamp", "open", "high", "low", "close", "volume"))
                writer.writeheader()
                writer.writerows(rows)
            staged.append((temporary, target))
        # Replace only after every market has downloaded and validated.
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)

    return {
        "source": "Luno public exchange candle endpoint", "quote_currency": "MYR",
        "timeframe": timeframe, "requested_days": days, "aligned_candles_per_asset": len(timestamps),
        "first_timestamp_ms": timestamps[0], "last_timestamp_ms": timestamps[-1],
        "last_candle_closed": True,
        "files": {asset: str((directory / f"{asset}-{timeframe}.csv").resolve()) for asset in PAIRS},
        "read_only_market_data_only": True,
    }
