"""Command line for read-only Luno discovery and paper signal inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

from .api import LunoAPIError, LunoPublicClient
from .indicators import momentum_signal

TIMEFRAMES = {"4h": 14_400, "1d": 86_400}
ASSETS = {"BTC", "ETH", "SOL", "XRP"}


async def discover(client: LunoPublicClient) -> list[dict]:
    rows = []
    for market in await client.markets():
        base = market.get("base_currency")
        if base == "XBT":
            base = "BTC"
        if base in ASSETS and market.get("counter_currency") == "MYR":
            rows.append({
                "asset": base,
                "market_id": market.get("market_id"),
                "trading_status": market.get("trading_status"),
                "min_volume": market.get("min_volume"),
                "price_scale": market.get("price_scale"),
            })
    return rows


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Read-only Luno MYR market research (no live orders).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover", help="List configured assets with an exact Luno MYR market.")
    snapshot_parser = subparsers.add_parser("snapshot", help="Show ticker and top-of-book for available MYR markets.")
    snapshot_parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS), default=sorted(ASSETS))
    signal_parser = subparsers.add_parser("signal", help="Inspect a baseline 4h/1d indicator signal.")
    signal_parser.add_argument("--timeframe", choices=sorted(TIMEFRAMES), default="4h")
    signal_parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS), default=sorted(ASSETS))
    signal_parser.add_argument("--lookback", type=int, default=400, help="Number of candles to request (max 1000).")
    args = parser.parse_args()

    if args.command == "signal" and not (os.environ.get("LUNO_API_KEY_ID") and os.environ.get("LUNO_API_SECRET")):
        print("Error: Luno requires API authentication for candles. Set a Luno read-only API key in this PowerShell session.", file=sys.stderr)
        return 2
    if args.command == "signal" and not 35 <= args.lookback <= 1000:
        parser.error("--lookback must be between 35 and 1000")

    try:
        async with LunoPublicClient() as client:
            try:
                markets = await discover(client)
                if args.command == "discover":
                    print(json.dumps({"as_of_utc": datetime.now(timezone.utc).isoformat(), "markets": markets}, indent=2))
                    return 0

                selected = [row for row in markets if row["asset"] in args.assets and row["trading_status"] == "ACTIVE"]
                if args.command == "snapshot":
                    results = []
                    for market in selected:
                        pair = market["market_id"]
                        book = await client.order_book_top(pair)
                        ticker = await client.ticker(pair)
                        bids, asks = book.get("bids", []), book.get("asks", [])
                        best_bid = float(bids[0]["price"]) if bids else None
                        best_ask = float(asks[0]["price"]) if asks else None
                        mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
                        spread_pct = ((best_ask - best_bid) / mid * 100) if mid else None
                        results.append({"asset": market["asset"], "pair": pair, "ticker": ticker,
                                        "book_timestamp_ms": book.get("timestamp"), "best_bid_myr": best_bid,
                                        "best_ask_myr": best_ask, "spread_pct": spread_pct,
                                        "book_age_seconds": max(0, time.time() - book.get("timestamp", 0) / 1000)})
                    print(json.dumps({"as_of_utc": datetime.now(timezone.utc).isoformat(), "snapshots": results}, indent=2))
                    return 0

                duration = TIMEFRAMES[args.timeframe]
                since_ms = int(time.time() * 1000) - args.lookback * duration * 1000
                results = []
                for market in selected:
                    candles = await client.candles(market["market_id"], duration, since_ms)
                    closes = [float(row["close"]) for row in candles]
                    results.append({"asset": market["asset"], "pair": market["market_id"], "timeframe": args.timeframe,
                                    "candles": len(candles), "last_candle_utc": datetime.fromtimestamp(candles[-1]["timestamp"] / 1000, timezone.utc).isoformat() if candles else None,
                                    "indicators": momentum_signal(closes)})
                print(json.dumps({"as_of_utc": datetime.now(timezone.utc).isoformat(), "research_only": True, "signals": results}, indent=2))
            except (LunoAPIError, KeyError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
        return 0
    except (LunoAPIError, KeyError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
