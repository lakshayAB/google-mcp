"""Start google-ads-mcp over stdio, list its tools and call them."""

import asyncio
import json
import logging
import time

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, PaginatedRequestParams, TextContent, Tool

from app.config import Settings

logger = logging.getLogger(__name__)

# The first `pipx run` / `uvx` downloads and builds the server, which can take a while.
STARTUP_TIMEOUT_SECONDS = 300
TOOL_CALL_TIMEOUT_SECONDS = 120
# Committed writes can take much longer (a video upload sends the whole file to YouTube).
WRITE_CALL_TIMEOUT_SECONDS = 1800
# Large GAQL results are cut here so one tool call can't flood the model's context.
# The model is told when this happens so it can narrow the query.
MAX_TOOL_RESULT_CHARS = 60_000


class MCPStartupError(RuntimeError):
    """Raised when the google-ads-mcp server cannot be started or initialized."""


class GoogleAdsMCPClient:
    """The google-ads-mcp subprocess and its MCP session, shared by all requests.

    The stdio transport's task group must be entered and exited in the same task, so a
    dedicated task owns the connection and request handlers only use the session.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._session: ClientSession | None = None
        self._runner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._startup_error: BaseException | None = None
        # One request at a time over the shared stdio session.
        self._call_lock = asyncio.Lock()
        self.tools: list[Tool] = []

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="google-ads-mcp")
        await self._ready.wait()
        if self._startup_error is not None:
            await self._runner
            raise MCPStartupError(
                f"Could not start google-ads-mcp with `{self._command_line()}`: "
                f"{type(self._startup_error).__name__}: {self._startup_error}. Check the server's "
                "stderr output above, that pipx/uvx can reach GitHub, and that your Google "
                "login is valid (run `python scripts/test_mcp.py`)."
            ) from self._startup_error
        logger.info("Connected to google-ads-mcp with tools: %s", [t.name for t in self.tools])

    async def stop(self) -> None:
        self._stop.set()
        if self._runner is not None:
            await self._runner
            self._runner = None

    def _command_line(self) -> str:
        return " ".join([self._settings.mcp_command, *self._settings.mcp_args])

    async def _run(self) -> None:
        params = StdioServerParameters(
            command=self._settings.mcp_command,
            args=self._settings.mcp_args,
            env=self._settings.mcp_env(),
        )
        logger.info("Starting google-ads-mcp: %s", self._command_line())
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=TOOL_CALL_TIMEOUT_SECONDS) as session:
                    with anyio.fail_after(STARTUP_TIMEOUT_SECONDS):
                        await session.initialize()
                    self.tools = await _list_tools(session)
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as exc:
            if not self._ready.is_set():
                self._startup_error = exc
            else:
                logger.exception("google-ads-mcp connection failed")
        finally:
            self._session = None
            self._ready.set()
            logger.info("google-ads-mcp stopped")

    def tool_schemas(self) -> list[dict]:
        """MCP tool definitions as plain name / description / JSON-schema dicts."""
        return [
            {
                "name": tool.name,
                "description": tool.description or tool.title or tool.name,
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
            }
            for tool in self.tools
        ]

    def is_write_tool(self, name: str) -> bool:
        """True unless the server marks the tool read-only (unknown tools count as writes)."""
        tool = next((t for t in self.tools if t.name == name), None)
        if tool is None:
            return True
        return not (tool.annotations is not None and tool.annotations.read_only_hint is True)

    def has_param(self, name: str, param: str) -> bool:
        tool = next((t for t in self.tools if t.name == name), None)
        properties = (tool.input_schema or {}).get("properties", {}) if tool else {}
        return param in properties

    async def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> tuple[str, bool]:
        """Run a tool and return (result text, is_error)."""
        if self._session is None:
            return "The Google Ads MCP server is not connected. Restart the app.", True

        args_json = json.dumps(arguments, ensure_ascii=False)
        started = time.perf_counter()
        try:
            async with self._call_lock:
                result = await self._session.call_tool(name, arguments, read_timeout_seconds=timeout)
        except Exception as exc:
            logger.exception("Tool call %s %s failed after %.2fs", name, args_json, time.perf_counter() - started)
            return f"Tool call failed: {type(exc).__name__}: {exc}", True
        logger.info("Tool call: %s %s (%.2fs)", name, args_json, time.perf_counter() - started)

        if not isinstance(result, CallToolResult):
            return f"Unexpected MCP response type: {type(result).__name__}", True

        text = _result_text(result)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            logger.info("Tool %s returned %d chars; truncating", name, len(text))
            text = (
                text[:MAX_TOOL_RESULT_CHARS]
                + f"\n\n[Result truncated: showed {MAX_TOOL_RESULT_CHARS} of {len(text)} "
                "characters. Narrow the query (fewer fields, a filter, ORDER BY + LIMIT) "
                "to see the rest.]"
            )
        if result.is_error:
            logger.warning("Tool %s returned an error: %s", name, text[:500])
        return text, bool(result.is_error)


async def _list_tools(session: ClientSession) -> list[Tool]:
    tools: list[Tool] = []
    cursor: str | None = None
    while True:
        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await session.list_tools(params=params)
        tools.extend(result.tools)
        cursor = result.next_cursor
        if not cursor:
            return tools


def _result_text(result: CallToolResult) -> str:
    parts = [block.text for block in result.content if isinstance(block, TextContent)]
    if parts:
        return "\n".join(parts)
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False, default=str)
    if result.content:
        return json.dumps([block.model_dump(mode="json") for block in result.content])
    return "(empty result)"
