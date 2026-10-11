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

Market discovery, ticker, and order-book calls work without credentials. Luno's candle endpoint requires authentication in the current API, so `signal` needs a Luno **read-only** API key. Create it in Luno under Profile → Security → API keys and choose **Read-only access**. Do not use a trading-access key. The project removes `ALLOW_WRITE_OPERATIONS` from the MCP child environment and allow-lists only `get_markets_info`, `get_candles`, `get_order_book`, and `get_ticker`.

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

`discover` shows which requested assets currently have an exact MYR market and its exchange-reported status. `snapshot` fetches ticker and top-of-book data once when invoked. With a read-only key set, `signal` fetches up to 1,000 candles and prints an explainable baseline MA/MACD candidate signal. No command places orders or records a simulated fill. The signal is deliberately a starting rule for research, not an instruction to buy or sell. Automated 15-minute collection, historical backtesting, paper-fill ledger, fees/slippage modeling, and the account-equity curve are subsequent implementation steps.

The MCP integration follows Luno's documented server and tool names: https://github.com/luno/luno-mcp. The server is a separate executable/container; the Python project launches it over MCP stdio and never invokes its account or write tools.
