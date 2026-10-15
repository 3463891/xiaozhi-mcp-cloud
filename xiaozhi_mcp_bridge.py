# -*- coding: utf-8 -*-
"""
小智 MCP 桥接服务（免费自托管版）
==================================
主动连接 xiaozhi.me 智能体的 MCP 接入点 (wss://api.xiaozhi.me/mcp/?token=...)，
向小智云端挂载以下工具：
  1. cloud_search        联网搜索（DuckDuckGo -> SearXNG -> Bing 多源容灾，免 API key）
  2. get_stock_quote     股票实时行情（腾讯 qt.gtimg.cn，免 key，A股/港股/美股）
  3. get_exchange_rate   货币汇率（Frankfurter API，免 key）

协议：MCP over WebSocket（小智云端作为 MCP 客户端，本服务作为服务端响应
initialize / tools/list / tools/call，协议版本 2024-11-05，JSON-RPC 2.0）。

配置：环境变量 MCP_ENDPOINT，或同目录 config.json {"endpoint": "wss://..."}
运行：python xiaozhi_mcp_bridge.py
"""
import asyncio
import json
import logging
import os
import random
import re
import sys
import urllib.parse
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import websockets
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# 配置与日志
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
PROTOCOL_VERSION = "2024-11-05"
HEALTH_PORT = int(os.environ.get("PORT", "17860"))  # 云端(HF)由环境变量注入 7860

logger = logging.getLogger("bridge")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)
try:
    _fh = RotatingFileHandler(HERE / "bridge.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
except Exception:
    pass


def load_endpoint() -> str:
    ep = os.environ.get("MCP_ENDPOINT", "").strip()
    if ep:
        return ep
    cfg = HERE / "config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")).get("endpoint", "").strip()
        except Exception as e:
            logger.error("config.json 解析失败: %s", e)
    logger.error("未配置接入点：请设置环境变量 MCP_ENDPOINT 或在 config.json 中填写 endpoint")
    sys.exit(1)


# ----------------------------------------------------------------------------
# HTTP 客户端
# ----------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
http_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global http_client
    if http_client is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            follow_redirects=True,
        )
    return http_client


def ok(data) -> dict:
    return {"content": [{"type": "text", "text": f"[Result] {data}"}]}


def err(msg) -> dict:
    return {"content": [{"type": "text", "text": f"[Error] {msg}"}], "isError": True}


# ----------------------------------------------------------------------------
# 工具 1：cloud_search 联网搜索（多源容灾）
# ----------------------------------------------------------------------------
SEARXNG_INSTANCES = [
    "https://searx.be",
    "https://search.inetol.net",
    "https://searx.tiekoetter.com",
    "https://priv.au",
]


def _ddg_unwrap(href: str) -> str:
    """解包 DuckDuckGo 的跳转链接 //duckduckgo.com/l/?uddg=<urlencoded>"""
    if "uddg=" in href:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            return q.get("uddg", [href])[0]
        except Exception:
            return href
    return href


async def _search_ddg(query: str, count: int) -> list:
    r = await client().get("https://html.duckduckgo.com/html/", params={"q": query})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for item in soup.select(".result, .web-result"):
        a = item.select_one("a.result__a")
        if not a or not a.get_text(strip=True):
            continue
        snippet_el = item.select_one(".result__snippet")
        results.append({
            "title": a.get_text(strip=True),
            "url": _ddg_unwrap(a.get("href", "")),
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
        if len(results) >= count:
            break
    return results


async def _search_searxng(query: str, count: int) -> list:
    last_err = None
    for base in SEARXNG_INSTANCES:
        try:
            r = await client().get(f"{base}/search", params={"q": query, "format": "json"}, timeout=8.0)
            if r.status_code != 200:
                continue
            data = r.json()
            return [{"title": it.get("title", ""), "url": it.get("url", ""),
                     "snippet": it.get("content", "")}
                    for it in data.get("results", [])[:count]]
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError("所有 SearXNG 实例均不可用")


async def _search_bing(query: str, count: int) -> list:
    r = await client().get("https://www.bing.com/search",
                           params={"q": query, "mkt": "zh-CN", "count": count})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for item in soup.select("li.b_algo"):
        a = item.select_one("h2 a")
        if not a or not a.get_text(strip=True):
            continue
        snip = ""
        p = item.select_one(".b_caption p, .b_paractrl, p")
        if p:
            snip = p.get_text(strip=True)
        results.append({"title": a.get_text(strip=True), "url": a.get("href", ""), "snippet": snip})
        if len(results) >= count:
            break
    return results


async def _search_baidu(query: str, count: int) -> list:
    """百度搜索（国内直连稳定，免 key）"""
    r = await client().get("https://www.baidu.com/s", params={"wd": query, "rn": str(max(count, 10))})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for item in soup.select("div.result, div.result-op"):
        a = item.select_one("h3 a")
        if not a or not a.get_text(strip=True):
            continue
        title = a.get_text(strip=True)
        snip = ""
        for sel in (".c-abstract", "span[class*='content-right']", "div[class*='abstract']"):
            el = item.select_one(sel)
            if el and el.get_text(strip=True):
                snip = el.get_text(strip=True)
                break
        if not snip:  # 兜底：容器全文去掉标题后截取
            full = re.sub(r"\s+", " ", item.get_text(" ", strip=True)).replace(title, "", 1).strip()
            snip = full[:160]
        results.append({"title": title, "url": a.get("href", ""), "snippet": snip})
        if len(results) >= count:
            break
    return results


async def tool_cloud_search(args: dict) -> dict:
    query = str(args.get("query", "")).strip()
    count = int(args.get("count", 5) or 5)
    count = max(1, min(count, 10))
    if not query:
        return err("query 参数不能为空")

    errors = []
    for name, fn in (("duckduckgo", _search_ddg), ("baidu", _search_baidu),
                     ("bing", _search_bing), ("searxng", _search_searxng)):
        try:
            results = await fn(query, count)
            if results:
                lines = [f"「{query}」搜索结果（来源: {name}，共 {len(results)} 条）："]
                for i, it in enumerate(results, 1):
                    lines.append(f"{i}. {it['title']}\n   链接: {it['url']}\n   摘要: {it['snippet']}")
                return ok("\n".join(lines))
            errors.append(f"{name}: 0 结果")
        except Exception as e:
            errors.append(f"{name}: {e}")
    return err("所有搜索源均失败 -> " + " | ".join(errors))


# ----------------------------------------------------------------------------
# 工具 2：get_stock_quote 股票行情（腾讯接口，免 key）
# ----------------------------------------------------------------------------
def _normalize_symbol(sym: str) -> str:
    sym = sym.strip()
    low = sym.lower()
    if re.fullmatch(r"(sh|sz|bj)\d{6}", low):   # 已带市场前缀
        return low
    if re.fullmatch(r"hk\d{4,5}", low):          # 已带 hk
        return low
    if low.startswith("us") and re.fullmatch(r"us[A-Za-z]{1,6}(\.[A-Za-z]{2,3})?", low):
        return sym                                # 已带 us，保持原大小写
    if re.fullmatch(r"\d{6}", sym):               # A股 6 位数字自动判市场
        return ("sh" if sym.startswith(("6", "9")) else "sz") + sym
    if re.fullmatch(r"\d{4,5}", sym):             # 港股
        return "hk" + sym.zfill(5)
    if re.fullmatch(r"[A-Za-z]{1,6}(\.[A-Za-z]{2,3})?", sym):  # 美股
        return "us" + sym.upper()
    return sym


US_RETRY_SUFFIX = ".OQ"  # 腾讯美股：纳斯达克 .OQ，第一轮不带后缀未命中时补后缀重试


def _parse_tencent_quote(raw: str, sym: str) -> dict:
    fields = raw.split("~")
    g = lambda i: fields[i].strip() if len(fields) > i else ""
    fnum = lambda i: _to_float(g(i))
    return {
        "symbol": sym,
        "name": g(1),
        "price": fnum(3),
        "prev_close": fnum(4),
        "open": fnum(5),
        "change": fnum(31),
        "change_pct": fnum(32),
        "high": fnum(33),
        "low": fnum(34),
        "turnover_rate_pct": fnum(38),
        "pe_ttm": fnum(39),
        "float_mv_yi": fnum(44),
        "total_mv_yi": fnum(45),
        "pb": fnum(46),
    }


def _to_float(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


async def _fetch_tencent_quotes(syms: list) -> dict:
    """从腾讯行情接口拉取并解析，返回 {symbol: quote_dict}"""
    r = await client().get("https://qt.gtimg.cn/q=" + ",".join(syms))
    text = r.content.decode("gbk", errors="replace")
    found = {}
    for sym in syms:
        m = re.search(r'v_' + re.escape(sym) + r'="([^"]*)"', text)
        if m and m.group(1):
            found[sym] = _parse_tencent_quote(m.group(1), sym)
    return found


async def tool_stock_quote(args: dict) -> dict:
    symbols_arg = str(args.get("symbols", "")).strip()
    if not symbols_arg:
        return err("symbols 参数不能为空，例如 '600023' 或 '600023,hk00700,usAAPL'")
    symbols = [_normalize_symbol(s) for s in re.split(r"[,，\s]+", symbols_arg) if s][:8]
    if not symbols:
        return err("没有有效的股票代码")
    try:
        quotes = await _fetch_tencent_quotes(symbols)
        # 美股第一轮未命中的，补 .OQ（纳斯达克）重试
        us_missing = [s for s in symbols
                      if s not in quotes and s.startswith("us") and "." not in s]
        if us_missing:
            quotes.update(await _fetch_tencent_quotes([s + ".OQ" for s in us_missing]))
        lines = []
        for sym in symbols:
            q = quotes.get(sym) or quotes.get(sym + ".OQ")
            if not q:
                lines.append(f"{sym}: 未查到（代码可能有误，A股如 600023，港股如 hk00700，美股如 usAAPL）")
                continue
            pct = q["change_pct"]
            pct_s = f"{pct:+.2f}%" if pct is not None else "N/A"
            extra = []
            if q["pe_ttm"] is not None: extra.append(f"PE(TTM) {q['pe_ttm']}")
            if q["pb"] is not None: extra.append(f"PB {q['pb']}")
            if q["total_mv_yi"] is not None: extra.append(f"总市值 {q['total_mv_yi']}亿")
            if q["turnover_rate_pct"] is not None: extra.append(f"换手 {q['turnover_rate_pct']}%")
            lines.append(
                f"{q['name']}({q['symbol']}) 现价 {q['price']}  涨跌 {q['change']} ({pct_s})"
                f" | 今开 {q['open']} 最高 {q['high']} 最低 {q['low']} 昨收 {q['prev_close']}"
                + (f" | {' '.join(extra)}" if extra else "")
            )
        return ok("\n".join(lines))
    except Exception as e:
        return err(f"行情查询失败: {e}")


# ----------------------------------------------------------------------------
# 工具 3：get_exchange_rate 汇率（Frankfurter，免 key）
# ----------------------------------------------------------------------------
async def tool_exchange_rate(args: dict) -> dict:
    base = str(args.get("base", "USD")).strip().upper() or "USD"
    symbols = str(args.get("symbols", "CNY")).strip().upper() or "CNY"
    symbols = ",".join([s for s in re.split(r"[,，\s]+", symbols) if s][:8])
    try:
        r = await client().get(f"https://api.frankfurter.dev/v1/latest",
                               params={"base": base, "symbols": symbols})
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates", {})
        if not rates:
            return err(f"未查询到 {base} -> {symbols} 的汇率")
        date = data.get("date", "")
        lines = [f"1 {base} 兑换（{date}）："]
        for k, v in rates.items():
            lines.append(f"  1 {base} = {v} {k}")
        return ok("\n".join(lines))
    except Exception as e:
        return err(f"汇率查询失败: {e}")


# ----------------------------------------------------------------------------
# 工具注册表（docstring JSON 格式与小智生态兼容）
# ----------------------------------------------------------------------------
TOOL_DESCRIPTIONS = [
    {
        "name": "cloud_search",
        "description": "联网搜索互联网上的最新信息、新闻和资料，返回标题、摘要和链接。当需要查询实时资讯、你不确定或不知道的知识时应使用此工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或问题"},
                "count": {"type": "integer", "description": "返回结果条数，默认 5，最大 10"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_stock_quote",
        "description": "查询股票实时行情，支持 A 股（如 600023）、港股（如 hk00700）、美股（如 usAAPL）。返回现价、涨跌幅、开高低收、PE、PB、总市值、换手率。可一次查多只，逗号分隔。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbols": {"type": "string", "description": "股票代码，逗号分隔，如 '600023,hk00700,usAAPL'"},
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "get_exchange_rate",
        "description": "查询货币之间实时汇率，如 1 美元等于多少人民币。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "基准货币代码，如 USD"},
                "symbols": {"type": "string", "description": "目标货币，如 CNY，可逗号分隔多个"},
            },
            "required": ["base", "symbols"],
        },
    },
]

TOOL_HANDLERS = {
    "cloud_search": tool_cloud_search,
    "get_stock_quote": tool_stock_quote,
    "get_exchange_rate": tool_exchange_rate,
}


# ----------------------------------------------------------------------------
# MCP 协议处理（小智云端 -> 本服务）
# ----------------------------------------------------------------------------
async def handle_message(ws, raw) -> None:
    try:
        payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception as e:
        logger.warning("无法解析的消息: %s", e)
        return

    method = payload.get("method", "")
    has_id = "id" in payload
    logger.info("收到请求: %s (id=%s)", method, payload.get("id"))

    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xiaozhi-cloud-mcp", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOL_DESCRIPTIONS}
        elif method == "tools/call":
            params = payload.get("params", {}) or {}
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            handler = TOOL_HANDLERS.get(name)
            if handler is None:
                result = err(f"工具 '{name}' 不存在")
            else:
                logger.info("执行工具: %s args=%s", name, json.dumps(args, ensure_ascii=False)[:300])
                result = await handler(args)
                logger.info("工具完成: %s", name)
        elif method == "ping":
            result = {"status": "ok"}
        elif method == "notifications/initialized":
            logger.info("客户端确认初始化完成")
            return  # 通知无需响应
        else:
            result = {"status": "ignored", "method": method}

        if has_id:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result},
                                     ensure_ascii=False))
            logger.info("已响应: %s", method)
    except Exception as e:
        logger.error("处理 %s 出错: %s", method, e)
        if has_id:
            try:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": payload["id"],
                                          "error": {"code": -32603, "message": str(e)}}))
            except Exception:
                pass


# ----------------------------------------------------------------------------
# 连接循环（指数退避重连）
# ----------------------------------------------------------------------------
async def bridge_loop(endpoint: str) -> None:
    delay, max_delay = 1.0, 60.0
    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info("连接小智 MCP 接入点 (第 %s 次尝试)…", attempt)
            async with websockets.connect(endpoint, ping_interval=20, ping_timeout=10,
                                          max_size=None) as ws:
                logger.info("✅ WebSocket 已连接，等待小智云端下发请求…")
                delay = 1.0  # 成功后重置退避
                async for message in ws:
                    await handle_message(ws, message)
            logger.warning("连接正常关闭，5 秒后重连")
            delay = 5.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("连接异常: %s", e)
        wait = delay * random.uniform(0.8, 1.2)
        logger.info("将在 %.1f 秒后重连", wait)
        await asyncio.sleep(wait)
        delay = min(delay * 2, max_delay)


# ----------------------------------------------------------------------------
# 健康检查 HTTP（HF Spaces 端口保活 / 本地可忽略）
# ----------------------------------------------------------------------------
async def health_server() -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await asyncio.wait_for(reader.read(4096), timeout=5)
            body = b'{"status": "running"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                         b"Connection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handler, "0.0.0.0", HEALTH_PORT)
        logger.info("健康检查端口: %s", HEALTH_PORT)
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning("健康检查端口启动失败（本地模式可忽略）: %s", e)


async def main() -> None:
    endpoint = load_endpoint()
    if not endpoint.startswith(("ws://", "wss://")):
        logger.error("接入点地址应以 ws:// 或 wss:// 开头，当前: %s", endpoint[:40] + "…")
        sys.exit(1)
    logger.info("小智 MCP 桥接服务启动 | 工具: %s", ", ".join(TOOL_HANDLERS))
    await asyncio.gather(health_server(), bridge_loop(endpoint))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已手动退出")
