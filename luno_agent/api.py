"""Read-only adapter for the official Luno MCP server."""

from __future__ import annotations

import json
import os
import shlex
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import stdio_client


class LunoAPIError(RuntimeError):
    """Raised when the MCP server or a Luno market-data tool fails."""


MARKET_TOOLS = {"get_markets_info", "get_candles", "get_order_book", "get_ticker"}


def _decode_result(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        detail = "\n".join(getattr(block, "text", "") for block in result.content)
        raise LunoAPIError(detail or "Luno MCP tool returned an error")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise LunoAPIError("Luno MCP returned no structured JSON result")


class LunoPublicClient:
    """MCP client limited to market-data tools; no account or write tools."""

    def __init__(self) -> None:
        self.command = os.environ.get("LUNO_MCP_COMMAND", "luno-mcp")
        raw_args = os.environ.get("LUNO_MCP_ARGS", "")
        self.args = shlex.split(raw_args, posix=os.name != "nt")
        self._stack: AsyncExitStack | None = None
        self._session = None
        self._tool_names: set[str] = set()

    async def __aenter__(self) -> "LunoPublicClient":
        from mcp import StdioServerParameters

        env = os.environ.copy()
        for key in ("ALLOW_WRITE_OPERATIONS", "LUNO_API_KEY_ID", "LUNO_API_SECRET"):
            env.pop(key, None)
        key_id = os.environ.get("LUNO_API_KEY_ID")
        key_secret = os.environ.get("LUNO_API_SECRET")
        if key_id and key_secret:
            env["LUNO_API_KEY_ID"] = key_id
            env["LUNO_API_SECRET"] = key_secret
        parameters = StdioServerParameters(command=self.command, args=self.args, env=env)
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            read_stream, write_stream = await self._stack.enter_async_context(stdio_client(parameters))
            self._session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
            await self._session.initialize()
            tools = await self._session.list_tools()
            self._tool_names = {tool.name for tool in tools.tools}
            return self
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, traceback)
            self._stack = None

    async def _call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if name not in MARKET_TOOLS:
            raise LunoAPIError(f"Tool {name!r} is not allowed by this read-only adapter")
        if name not in self._tool_names:
            raise LunoAPIError(f"Official Luno MCP server does not expose {name!r}")
        if self._session is None:
            raise LunoAPIError("Luno MCP client is not connected")
        try:
            result = await self._session.call_tool(name, arguments or {})
        except Exception as exc:
            raise LunoAPIError(f"Luno MCP call failed ({name}): {exc}") from exc
        return _decode_result(result)

    async def markets(self) -> list[dict[str, Any]]:
        payload = await self._call("get_markets_info")
        markets = payload.get("markets")
        if not isinstance(markets, list):
            raise LunoAPIError("get_markets_info response is missing markets[]")
        return markets

    async def candles(self, pair: str, duration_seconds: int, since_ms: int) -> list[dict[str, Any]]:
        if not (os.environ.get("LUNO_API_KEY_ID") and os.environ.get("LUNO_API_SECRET")):
            raise LunoAPIError(
                "Luno requires API authentication for candles. Create a read-only API key, "
                "then set LUNO_API_KEY_ID and LUNO_API_SECRET in this PowerShell session."
            )
        payload = await self._call("get_candles", {"pair": pair, "duration": duration_seconds, "since": since_ms})
        candles = payload.get("candles")
        if not isinstance(candles, list):
            raise LunoAPIError("get_candles response is missing candles[]")
        return candles

    async def order_book_top(self, pair: str) -> dict[str, Any]:
        payload = await self._call("get_order_book", {"pair": pair})
        if "bids" not in payload or "asks" not in payload:
            raise LunoAPIError("get_order_book response is missing bids/asks")
        # The Luno MCP tool can return the complete book; trim to the top 100
        # levels to keep the existing analysis interface bounded.
        payload["bids"] = list(payload["bids"][:100])
        payload["asks"] = list(payload["asks"][:100])
        return payload

    async def ticker(self, pair: str) -> dict[str, Any]:
        return await self._call("get_ticker", {"pair": pair})
