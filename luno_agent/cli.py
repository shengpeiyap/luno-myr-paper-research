"""Command line for read-only Luno discovery and paper signal inspection."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .backtest import load_candles_csv, run_backtest
from .indicators import momentum_signal
from .paper import PaperSafetyError, acquire_lock, load_state, release_lock, run_paper_cycle, save_state
from .portfolio_replay import run_portfolio_replay

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Luno MYR market research (no live orders).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover", help="List configured assets with an exact Luno MYR market.")
    snapshot_parser = subparsers.add_parser("snapshot", help="Show ticker and top-of-book for available MYR markets.")
    snapshot_parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS), default=sorted(ASSETS))
    signal_parser = subparsers.add_parser("signal", help="Inspect a baseline 4h/1d indicator signal.")
    signal_parser.add_argument("--timeframe", choices=sorted(TIMEFRAMES), default="4h")
    signal_parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS), default=sorted(ASSETS))
    signal_parser.add_argument("--lookback", type=int, default=400, help="Number of candles to request (max 1000).")
    backtest_parser = subparsers.add_parser("backtest", help="Compare stop candidates using a local OHLCV CSV.")
    backtest_parser.add_argument("--csv", required=True, help="CSV with timestamp,open,high,low,close,volume columns.")
    backtest_parser.add_argument("--asset", required=True, choices=sorted(ASSETS))
    backtest_parser.add_argument("--timeframe", required=True, choices=sorted(TIMEFRAMES))
    backtest_parser.add_argument("--equity-myr", required=True, type=float, help="Starting MYR paper equity.")
    backtest_parser.add_argument("--fee-pct-per-side", required=True, type=float, help="Actual or scenario fee per fill, as percent.")
    backtest_parser.add_argument("--spread-pct-round-trip", required=True, type=float, help="Assumed full bid/ask spread, as percent.")
    backtest_parser.add_argument("--slippage-pct-per-side", required=True, type=float, help="Assumed slippage per fill, as percent.")
    replay_parser = subparsers.add_parser("replay", help="Replay all four assets in one offline MYR portfolio from local CSVs.")
    replay_parser.add_argument("--data-dir", default="data", help="Directory containing ASSET-TIMEFRAME.csv for all four assets.")
    replay_parser.add_argument("--timeframe", choices=sorted(TIMEFRAMES), default="4h")
    replay_parser.add_argument("--equity-myr", required=True, type=float, help="Starting virtual MYR balance.")
    replay_parser.add_argument("--stop-loss-pct", type=float, default=None, help="Fixed stop percentage; defaults to the configured paper stop.")
    replay_parser.add_argument("--fee-pct-per-side", required=True, type=float, help="Estimated fee per fill, as percent.")
    replay_parser.add_argument("--spread-pct-round-trip", required=True, type=float, help="Assumed full spread, as percent.")
    replay_parser.add_argument("--slippage-pct-per-side", required=True, type=float, help="Assumed slippage per fill, as percent.")
    replay_parser.add_argument("--report", default=".paper/replay-latest.json", help="Output JSON report path (includes trades and equity curve).")
    paper_parser = subparsers.add_parser("paper", help="Run persistent paper-only execution from candle CSVs and live public books.")
    paper_parser.add_argument("--data-dir", default="data", help="Directory containing ASSET-TIMEFRAME.csv files.")
    paper_parser.add_argument("--state-file", default=".paper/account.json", help="Local paper ledger path (never committed).")
    paper_parser.add_argument("--timeframe", choices=sorted(TIMEFRAMES), default="4h")
    paper_parser.add_argument("--data-source", choices=("luno", "csv"), default="luno",
                              help="Fetch fresh candles from Luno or read ASSET-TIMEFRAME.csv files.")
    paper_parser.add_argument("--equity-myr", required=True, type=float, help="Initial MYR paper balance; used only for a new ledger.")
    paper_parser.add_argument("--fee-pct-per-side", required=True, type=float, help="Scenario/account fee, as percent per fill.")
    paper_parser.add_argument("--interval-seconds", type=int, default=900, help="Polling interval; default 15 minutes.")
    paper_parser.add_argument("--once", action="store_true", help="Process one cycle and exit instead of looping.")
    return parser


def run_backtest_cli(args: argparse.Namespace) -> int:
    if args.equity_myr <= 0:
        raise ValueError("--equity-myr must be positive")
    candles = load_candles_csv(args.csv, TIMEFRAMES[args.timeframe])
    config_path = Path(__file__).resolve().parent.parent / "config" / "strategy.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    risk = config["risk"]
    activity = config["activity"]
    results = [run_backtest(
        candles, asset=args.asset, timeframe=args.timeframe,
        initial_equity_myr=args.equity_myr, stop_loss_pct=stop_pct,
        account_risk_pct=risk["max_account_risk_per_trade_pct"],
        asset_exposure_pct=risk["max_asset_exposure_pct"],
        total_exposure_pct=risk["max_total_crypto_exposure_pct"],
        max_entries_per_day=activity["max_new_entries_per_myr_calendar_day"],
        drawdown_warning_pct=risk["account_drawdown_warning_pct"],
        drawdown_halt_pct=risk["account_drawdown_halt_pct"],
        fee_pct_per_side=args.fee_pct_per_side,
        spread_pct_round_trip=args.spread_pct_round_trip,
        slippage_pct_per_side=args.slippage_pct_per_side,
    ) for stop_pct in risk["stop_loss_candidates_pct"]]
    print(json.dumps({
        "research_only": True, "fill_model": "next_candle_open_with_spread_and_slippage",
        "asset_scope": "single_asset_per_run; results are not a combined portfolio",
        "candles": len(candles), "first_timestamp_ms": candles[0]["timestamp"],
        "last_timestamp_ms": candles[-1]["timestamp"], "results": results,
    }, indent=2))
    return 0


def run_replay_cli(args: argparse.Namespace) -> int:
    if args.equity_myr <= 0:
        raise ValueError("--equity-myr must be positive")
    config_path = Path(__file__).resolve().parent.parent / "config" / "strategy.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    risk = config["risk"]
    activity = config["activity"]
    stop_loss_pct = args.stop_loss_pct if args.stop_loss_pct is not None else risk["paper_default_stop_loss_pct"]
    result = run_portfolio_replay(
        args.data_dir, timeframe=args.timeframe, initial_equity_myr=args.equity_myr,
        stop_loss_pct=stop_loss_pct, account_risk_pct=risk["max_account_risk_per_trade_pct"],
        asset_exposure_pct=risk["max_asset_exposure_pct"],
        total_exposure_pct=risk["max_total_crypto_exposure_pct"],
        max_entries_per_day=activity["max_new_entries_per_myr_calendar_day"],
        drawdown_warning_pct=risk["account_drawdown_warning_pct"],
        drawdown_halt_pct=risk["account_drawdown_halt_pct"],
        fee_pct_per_side=args.fee_pct_per_side,
        spread_pct_round_trip=args.spread_pct_round_trip,
        slippage_pct_per_side=args.slippage_pct_per_side,
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in result.items() if key not in {"trades", "equity_curve"}}
    summary["trades"] = len(result["trades"])
    summary["equity_curve_points"] = len(result["equity_curve"])
    summary["report"] = str(report_path.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


async def main_async(args: argparse.Namespace) -> int:

    # Import MCP only for live market commands. Offline CSV backtests and
    # --help remain usable even when the MCP dependency/runtime is unavailable.
    from .api import LunoAPIError, LunoPublicClient

    if args.command == "paper":
        return await run_paper_cli(args)

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
        print(f"Error: {exc}", file=sys.stderr)
        return 2


async def run_paper_cli(args: argparse.Namespace) -> int:
    if args.equity_myr <= 0 or args.interval_seconds < 60:
        print("Error: --equity-myr must be positive and --interval-seconds at least 60", file=sys.stderr)
        return 2
    if not 0 <= args.fee_pct_per_side < 100:
        print("Error: --fee-pct-per-side must be between 0 and 100", file=sys.stderr)
        return 2
    state_path = Path(args.state_file).resolve()
    lock_descriptor = None
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_descriptor = acquire_lock(state_path)
        state = load_state(state_path, args.equity_myr)
        save_state(state_path, state)
        config_path = Path(__file__).resolve().parent.parent / "config" / "strategy.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        async with _paper_client() as client:
            while True:
                try:
                    candidate_state = copy.deepcopy(state)
                    result = await run_paper_cycle(
                        client, candidate_state, data_dir=Path(args.data_dir), timeframe=args.timeframe,
                        data_source=args.data_source,
                        fee_pct_per_side=args.fee_pct_per_side, risk=config["risk"],
                        activity=config["activity"], execution=config["execution"],
                    )
                    save_state(state_path, candidate_state)
                    state = candidate_state
                except Exception as exc:
                    result = {"mode": "paper", "error": str(exc), "new_entries_allowed": False}
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if args.once:
                    return 0 if "error" not in result else 2
                await asyncio.sleep(args.interval_seconds)
    except (OSError, KeyError, TypeError, ValueError, PaperSafetyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if lock_descriptor is not None:
            release_lock(state_path, lock_descriptor)


def _paper_client():
    from .api import LunoPublicClient
    return LunoPublicClient()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "backtest":
        try:
            if args.equity_myr <= 0:
                parser.error("--equity-myr must be positive")
            return run_backtest_cli(args)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    if args.command == "replay":
        try:
            return run_replay_cli(args)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    if args.command == "paper" and (args.interval_seconds < 60 or args.equity_myr <= 0):
        parser.error("--interval-seconds must be at least 60 and --equity-myr must be positive")
    if args.command == "signal" and not (os.environ.get("LUNO_API_KEY_ID") and os.environ.get("LUNO_API_SECRET")):
        print("Error: Luno requires API authentication for candles. Set a Luno read-only API key in this PowerShell session.", file=sys.stderr)
        return 2
    if args.command == "signal" and not 35 <= args.lookback <= 1000:
        parser.error("--lookback must be between 35 and 1000")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
