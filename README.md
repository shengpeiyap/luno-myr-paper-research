# Luno MYR Strategy Starter

This project starts as a **paper-trading and research** system for Luno MYR crypto markets. Live order placement is intentionally disabled. Market data is accessed through Luno's official MCP server; this project only invokes its read-only market-data tools.

## Initial policy

- Markets: discover available MYR pairs from Luno at runtime; enable BTC, ETH, SOL, or XRP only when the exact market is listed and active.
- Signal timeframe: selectable between 4 hours and 1 day. Evaluate these modes separately; do not blend their signals in the first backtest.
- Order book: use a 15-minute snapshot as an execution/liquidity filter, not as the directional signal. Reject stale data, wide spreads, or insufficient depth.
- Stop loss: compare 5%, 6.5%, and 8% fixed entry-price stops in backtests. The initial paper-trading default is 6.5%; this is a starting hypothesis, not an optimized setting.
- Account risk: initial assumed maximum planned loss is 0.5% of current MYR account equity per trade. This is configurable and is measured at the stop price, including estimated fees and slippage.
- Exposure: initial assumed cap is 25% of equity per asset and 80% total crypto exposure. These are conservative engineering defaults to review with backtest results.
- Activity: at most 3 new entries per MYR calendar day. A stop-out followed by a re-entry counts as a new entry; partial fills and adds are associated with the original trade and cannot exceed its risk budget.
- Drawdown: warn at 10% from the running equity peak; halt new entries at 20%. Resume only after manual review/reset.
- No leverage, withdrawals, or transfer permissions are needed by the strategy.

The 0.5% per-trade account risk, 25% per-asset exposure, 80% total exposure, and 10% warning threshold are explicit starter assumptions because those values were not specified. Edit `config/strategy.json` before running research.

## Build sequence

1. Read-only discovery of Luno markets, candles, fees, balances, and order book. Keep secrets outside source control.
2. Historical backtest for each timeframe and stop candidate, including maker/taker fees, spread, slippage, partial fills, and missing-data behavior.
3. Paper execution and audit log. Verify sizing, daily limits, drawdown circuit breakers, and reconciliation.
4. Only after review, consider enabling live trading with a dedicated minimum-permission API key and explicit order limits.

## Configuration

`config/strategy.json` is the policy source of truth. The runtime must fail closed when a market is unavailable, data is stale, balances cannot be reconciled, or risk calculations are invalid. Never infer a tradable pair from a ticker alias: use the exact market ID returned by Luno.

## MCP setup and current commands

The official Luno MCP server and Python client are now installed for this project. The server binary is kept under `.tools` and Python dependencies are isolated in `.venv`. In a fresh PowerShell window, run:

```powershell
Set-Location "C:\Users\User\OneDrive\Documents\AI Agent Investor"
$env:LUNO_MCP_COMMAND = (Resolve-Path .tools\luno-mcp.exe).Path
$env:LUNO_MCP_ARGS = ""
\.venv\Scripts\python.exe -m luno_agent.cli discover
```

Market discovery, ticker, and order-book calls work without credentials. Luno's current API reference labels candles as requiring no permission, but the installed MCP server returned `ErrUnauthorized` when we tried without credentials. In this environment, `signal` therefore needs a Luno **read-only** API key. Create it in Luno under Profile → Security → API keys and choose **Read-only access**. Do not use a trading-access key. The project removes `ALLOW_WRITE_OPERATIONS` from the MCP child environment and allow-lists only `get_markets_info`, `get_candles`, `get_order_book`, and `get_ticker`.

Without an API key, check supported MYR markets and fetch a live snapshot:

```powershell
.\.venv\Scripts\python.exe -m luno_agent.cli discover
.\.venv\Scripts\python.exe -m luno_agent.cli snapshot
```

Before running the two `signal` commands, enter the read-only key in the current PowerShell session. `Read-Host -AsSecureString` avoids echoing the secret or putting it in command history:

```powershell
$env:LUNO_API_KEY_ID = Read-Host "Luno read-only API key ID"
$secret = Read-Host "Luno API key secret" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $env:LUNO_API_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
.\.venv\Scripts\python.exe -m luno_agent.cli signal --timeframe 4h
.\.venv\Scripts\python.exe -m luno_agent.cli signal --timeframe 1d
Remove-Item Env:LUNO_API_KEY_ID,Env:LUNO_API_SECRET
```

`discover` shows which requested assets currently have an exact MYR market and its exchange-reported status. `snapshot` fetches ticker and top-of-book data once when invoked. With a read-only key set, `signal` fetches up to 1,000 candles and prints an explainable baseline MA/MACD candidate signal. No command places orders or records a simulated fill. The signal is deliberately a starting rule for research, not an instruction to buy or sell.

## Historical backtest (single asset)

`backtest` compares the configured 5%, 6.5%, and 8% fixed stop candidates for one asset and one timeframe at a time. It accepts a CSV with these columns: `timestamp,open,high,low,close,volume`. Timestamps can be Unix seconds, Unix milliseconds, or timezone-aware ISO 8601 strings. At least 60 consecutive candles are required; gaps, duplicates, unsorted rows, and timeframe mismatches are rejected. A run is isolated to one asset and is not a combined portfolio result.

Example (replace the cost assumptions with values appropriate for your account and data):

```powershell
.\.venv\Scripts\python.exe -m luno_agent.cli backtest `
  --csv .\data\BTC-4h.csv --asset BTC --timeframe 4h --equity-myr 10000 `
  --fee-pct-per-side 0.60 --spread-pct-round-trip 0.10 --slippage-pct-per-side 0.05
```

All three cost inputs are required so a run cannot silently treat costs as zero. The example values are illustrative only. [Luno Malaysia taker fees](https://guide.luno.com/hc/en-gb/articles/14581451031709-Luno-fees-and-limits-in-Malaysia) depend on rolling 30-day volume; verify your current account tier before using a fee assumption. The engine uses the same fixed fee on each fill, applies the supplied full spread and slippage, enters on the next candle open after a closed-bar signal, and uses conservative stop-gap fills. It enforces the configured per-trade risk, exposure caps, MYR-calendar-day entry limit, and 20% drawdown halt.

OHLCV candles do not contain historical 15-minute order books. This initial backtester therefore takes a fixed spread assumption and does not model historical depth or partial fills. It also does not fetch or invent candle history: in the current environment, the official MCP `get_candles` call returned `ErrUnauthorized` without account credentials. Obtain candle CSV data through an authorized read-only source before interpreting any result. No performance result should be treated as a forecast.

## Automatic paper execution

The `paper` command runs a persistent **simulation only**. It uses the existing MA/MACD candidate rule, reads completed 4-hour or daily candles, and checks current public order-book data every 15 minutes. The default candle source is Luno and therefore needs candle access to work; if that remains unavailable, use fresh `ASSET-TIMEFRAME.csv` files with `--data-source csv`. CSV files must be updated externally. The runner refuses stale candles, stale/crossed books, spreads above the configured cap, inactive markets, malformed state, or an already-running second instance.

Start with a single cycle to inspect behavior:

```powershell
.\.venv\Scripts\python.exe -m luno_agent.cli paper `
  --timeframe 4h --equity-myr 10000 --fee-pct-per-side 0.60 --once
```

Omit `--once` to poll every 15 minutes. Once the API is available, use only a read-only key for market data. The state file defaults to `.paper/account.json` and is ignored by Git. Buys/sells are simulated from the visible order-book depth; partial fills are recorded, stops are monitored each cycle, and signal exits wait for a new closed candle. The ledger halts new entries at the configured 20% drawdown and requires manual review before resuming. The program does not call any Luno order, balance, or account tools and contains no live execution path.

This is not ready for unattended use: candle access has not been verified for this account, the model has not been validated on historical account-specific fees and real paper logs, and the portfolio is not yet reconciled against actual account balances. Treat paper results as engineering validation only.

The MCP integration follows Luno's documented server and tool names: https://github.com/luno/luno-mcp. The server is a separate executable/container; the Python project launches it over MCP stdio and never invokes its account or write tools.
