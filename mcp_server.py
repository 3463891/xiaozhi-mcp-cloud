"""
Minimal, dependency-light MCP (Model Context Protocol) JSON-RPC server core.

Implements exactly what the XiaoZhi (xiaozhi.me) cloud MCP endpoint speaks:
newline-delimited JSON-RPC 2.0 over a stdio pipe that is bridged to a
WebSocket. Kept hand-written on purpose so it needs no SDK version pinning and
starts instantly on free cloud tiers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("mcp")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

TOOL_TIMEOUT = float(__import__("os").environ.get("TOOL_TIMEOUT", "25"))


# 小智服务端内置函数名 —— MCP 工具撞名会让机器人「每轮对话先报错一次、之后又正常」
# （get_weather 是 ToolType.SYSTEM_CTL，每轮自动取本地天气写上下文）
XIAOZHI_BUILTIN_TOOL_NAMES = frozenset({"get_weather", "get_time", "get_current_time"})
XIAOZHI_RISKY_TOOL_NAMES = frozenset({
    "get_lunar", "change_role", "play_music", "handle_speaker", "call_device",
    "handle_exit_intent", "get_news_from_chinanews", "get_news_from_newsnow",
    "search_from_ragflow", "hass_state", "hass_init", "hass_play_music",
})


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Awaitable[str] | str],
        service: str = "core",
    ) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler
        self.service = service

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


class MCPServer:
    """JSON-RPC 2.0 dispatcher for MCP over any newline-delimited transport."""

    def __init__(self, server_name: str = "xiaozhi-mcp-cloud", version: str = "1.0.0") -> None:
        self.server_name = server_name
        self.version = version
        self.tools: dict[str, Tool] = {}
        self.negotiated_version = PROTOCOL_VERSION
        self.ready = False

    # ---------------------------------------------------------------- registry
    def register(self, tool: Tool) -> None:
        if tool.name in XIAOZHI_BUILTIN_TOOL_NAMES:
            logger.error(
                "工具名 %r 与小智服务端内置函数重名，机器人将每轮对话先报错！请改名",
                tool.name,
            )
        elif tool.name in XIAOZHI_RISKY_TOOL_NAMES:
            logger.warning("工具名 %r 在小智生态里也是内置函数名（仅提示）", tool.name)
        self.tools[tool.name] = tool

    def register_service(self, service: str, tools: list[Tool]) -> None:
        for t in tools:
            if t.service == service:
                self.register(t)

    def set_services(self, services: list[str]) -> None:
        """Rebuild the tool registry so only the given services are exposed.

        Register fresh Tool copies rather than sharing the module-level
        ALL_TOOLS objects. bridge.py wraps handlers in place to count calls;
        with shared objects, every reconnect would add one more wrapper layer
        (duplicated '调用工具' log lines and inflated tool_calls counters).
        """
        self.tools = {}
        for t in ALL_TOOLS:
            if t.service in services:
                self.register(Tool(t.name, t.description, t.schema, t.handler, t.service))

    # ---------------------------------------------------------------- dispatch
    async def handle(self, raw: str) -> str | None:
        """Handle one inbound JSON-RPC message. Returns reply text or None."""
        raw = raw.strip()
        if not raw:
            return None
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("unparseable inbound frame: %.200s", raw)
            return self._err(None, -32700, "Parse error")

        if isinstance(msg, list):
            replies = [r for r in [await self._one(m) for m in msg] if r]
            return "\n".join(replies) if replies else None
        return await self._one(msg)

    async def _one(self, msg: dict[str, Any]) -> str | None:
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if method == "initialize":
            return await self._initialize(mid, params)
        if method == "notifications/initialized":
            self.ready = True
            logger.info("MCP session initialized")
            return None
        if method in ("notifications/cancelled", "notifications/progress"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": [t.descriptor() for t in self.tools.values()]})
        if method == "tools/call":
            return await self._call_tool(mid, params)
        if method == "resources/list":
            return self._ok(mid, {"resources": []})
        if method == "resources/templates/list":
            return self._ok(mid, {"resourceTemplates": []})
        if method == "prompts/list":
            return self._ok(mid, {"prompts": []})
        if method == "logging/setLevel":
            return self._ok(mid, {})
        if method == "completion/complete":
            return self._ok(mid, {"completion": {"values": []}})
        if mid is None:
            logger.debug("ignoring notification: %s", method)
            return None
        # MCP clients may treat a -32601 error frame as a hard failure
        # (XiaoZhi devices buzz/show ERROR). An empty success keeps the
        # session quiet while we still log the unsupported method loudly.
        logger.warning("unsupported method %s (id=%s), replying empty ok", method, mid)
        return self._ok(mid, {})

    async def _initialize(self, mid: Any, params: dict[str, Any]) -> str:
        client_version = (params or {}).get("protocolVersion") or PROTOCOL_VERSION
        self.negotiated_version = (
            client_version if client_version in SUPPORTED_VERSIONS else PROTOCOL_VERSION
        )
        logger.info("initialize <- client %s (requested %s)", params.get("clientInfo"), client_version)
        return self._ok(
            mid,
            {
                "protocolVersion": self.negotiated_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {},
                    "prompts": {},
                    "logging": {},
                },
                "serverInfo": {"name": self.server_name, "version": self.version},
            },
        )

    async def _call_tool(self, mid: Any, params: dict[str, Any]) -> str:
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = self.tools.get(name)
        started = time.time()
        if tool is None:
            logger.warning("tool not found: %s", name)
            return self._err(mid, -32602, f"Unknown tool: {name}")
        try:
            if asyncio.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(tool.handler(args), timeout=TOOL_TIMEOUT)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(tool.handler, args), timeout=TOOL_TIMEOUT
                )
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            logger.info("tool %s ok in %.2fs", name, time.time() - started)
            return self._ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except asyncio.TimeoutError:
            logger.error("tool %s timed out", name)
            # Soft failure: XiaoZhi devices buzz + show ERROR on isError results,
            # so report the problem as normal text the LLM can act on instead.
            return self._ok(
                mid,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"工具 {name} 执行超时，请稍后再试，或换个方式回答这个问题。",
                        }
                    ],
                    "isError": False,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("tool %s failed", name)
            return self._ok(
                mid,
                {
                    "content": [
                        {"type": "text", "text": f"工具 {name} 执行失败: {e}。请换个方式回答用户的问题。"}
                    ],
                    "isError": False,
                },
            )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _ok(mid: Any, result: dict[str, Any]) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}, ensure_ascii=False)

    @staticmethod
    def _err(mid: Any, code: int, message: str) -> str:
        return json.dumps(
            {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
import search_engine as se  # noqa: E402


def _fmt_results(results: list[se.SearchResult], query: str) -> str:
    if not results:
        return f"未找到与「{query}」相关的结果。"
    lines = [f"「{query}」的搜索结果（来源：{results[0].source}）："]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}")
        if r.snippet:
            lines.append(f"   摘要：{r.snippet[:300]}")
        if r.url:
            lines.append(f"   链接：{r.url}")
    return "\n".join(lines)


async def _t_web_search(args: dict[str, Any]) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise RuntimeError("缺少参数 query")
    count = int(args.get("count") or 5)
    count = max(1, min(count, 10))
    results = await se.web_search(query, count)
    return _fmt_results(results, query)


async def _t_web_fetch(args: dict[str, Any]) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        raise RuntimeError("缺少参数 url")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = int(args.get("max_chars") or 4000)
    max_chars = max(200, min(max_chars, 20000))
    try:
        text = await se.fetch_url(url, max_chars)
    except Exception as e:  # noqa: BLE001
        logger.warning("web_fetch %s failed: %s", url, e)
        reason = str(e).strip() or type(e).__name__
        return (
            f"网页 {url} 抓取失败（{reason}）。这个站点可能禁止程序访问（常见于百度百科、"
            f"知乎、微信公众号）。请改用 web_search 搜索相关关键词，或换一个网址再试。"
        )
    return f"网页 {url} 的主要内容：\n{text}" if text else f"未能从 {url} 提取到内容。"


async def _t_weather(args: dict[str, Any]) -> str:
    city = (args.get("city") or args.get("location") or "").strip()
    if not city:
        raise RuntimeError("缺少参数 city")
    return await se.get_weather(city)


async def _t_stock(args: dict[str, Any]) -> str:
    code = (args.get("code") or args.get("symbol") or "").strip()
    if not code:
        raise RuntimeError("缺少参数 code")
    return await se.get_stock_price(code)


async def _t_time(args: dict[str, Any]) -> str:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    week = "一二三四五六日"[now.weekday()]
    return (
        f"当前北京时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（星期{week}）"
    )


async def _t_calc(args: dict[str, Any]) -> str:
    expr = args.get("expression") or args.get("expr") or ""
    if not expr:
        raise RuntimeError("缺少参数 expression")
    return f"{expr} = {se.calculate(str(expr))}"


ALL_TOOLS: list[Tool] = [
    Tool(
        name="web_search",
        description=(
            "联网搜索互联网上的实时信息。当用户询问新闻、最新事件、实时数据、"
            "人物资料、产品价格、天气以外的任何需要联网才能回答的问题时使用此工具。"
            "参数为搜索关键词，返回标题、摘要和链接。"
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，尽量简洁明确"},
                "count": {"type": "integer", "description": "返回结果数量，1-10，默认 5"},
            },
            "required": ["query"],
        },
        handler=_t_web_search,
        service="cloud-search",
    ),
    Tool(
        name="web_fetch",
        description=(
            "抓取指定网页的正文内容。当你已经有一个具体网址，"
            "或者需要根据搜索结果中的链接查看详细内容时使用。"
        ),
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的完整网址"},
                "max_chars": {"type": "integer", "description": "返回的最大字符数，默认 4000"},
            },
            "required": ["url"],
        },
        handler=_t_web_fetch,
        service="cloud-search",
    ),
    Tool(
        name="weather_forecast",
        description="查询指定城市当前的天气情况以及未来三天的天气预报。",
        schema={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名称，如 北京、上海、杭州"}},
            "required": ["city"],
        },
        handler=_t_weather,
        service="life",
    ),
    Tool(
        name="get_stock_price",
        description=(
            "查询 A 股等股票的实时行情，包括现价、涨跌幅、开盘价、最高价、最低价、昨收、成交量。"
            "参数为股票代码（如 600023）或带前缀的代码（如 sh600023、sz000001）。"
        ),
        schema={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "股票代码，如 600023 或 sh600023"}},
            "required": ["code"],
        },
        handler=_t_stock,
        service="finance",
    ),
    Tool(
        name="calculator",
        description="计算数学表达式，支持加减乘除、括号、幂运算和常用数学函数。",
        schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式，如 (1+2)*3 或 sqrt(8)"}
            },
            "required": ["expression"],
        },
        handler=_t_calc,
        service="utils",
    ),
    Tool(
        name="beijing_time",
        description="获取当前北京时间和星期几。当用户问现在几点、今天几号时使用。",
        schema={"type": "object", "properties": {}},
        handler=_t_time,
        service="life",
    ),
]

# Service catalogue (mirrors the "服务市场" of facaiai.cn)
SERVICE_CATALOG: dict[str, dict[str, Any]] = {
    "cloud-search": {
        "name": "云搜索",
        "icon": "🔍",
        "tagline": "实时联网搜索 + 网页正文抓取",
        "desc": "让小智具备实时联网搜索能力：搜索互联网最新信息，并可打开任意网页读取正文内容。",
        "tools": ["web_search", "web_fetch"],
        "free": True,
    },
    "finance": {
        "name": "股票行情",
        "icon": "📈",
        "tagline": "A 股实时价格、涨跌幅、成交量",
        "desc": "查询 A 股实时行情数据，支持沪市、深市、北交所股票代码。",
        "tools": ["get_stock_price"],
        "free": True,
    },
    "life": {
        "name": "生活助手",
        "icon": "🌤️",
        "tagline": "天气查询 + 当前时间",
        "desc": "查询城市当前天气与三天预报，以及当前北京时间。",
        "tools": ["weather_forecast", "beijing_time"],
        "free": True,
    },
    "utils": {
        "name": "计算工具",
        "icon": "🧮",
        "tagline": "数学表达式计算",
        "desc": "执行数学表达式计算，支持括号、幂运算和常用数学函数。",
        "tools": ["calculator"],
        "free": True,
    },
}

DEFAULT_SERVICES = ["cloud-search", "finance", "life", "utils"]
