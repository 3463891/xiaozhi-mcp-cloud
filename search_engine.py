"""
Free, key-less web search / fetch / weather / stock backends.

All backends are public endpoints that do NOT require an API key.
If TAVILY_API_KEY or BOCHA_API_KEY is present in the environment, those
higher-quality providers are tried first and the free engines become fallback.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "8"))


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _client(extra_headers: dict[str, str] | None = None, **kw: Any) -> httpx.AsyncClient:
    headers = {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
    }
    if extra_headers:
        headers.update(extra_headers)
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT, follow_redirects=True, headers=headers, **kw
    )


def _soup(html_text: str):
    from bs4 import BeautifulSoup

    try:
        return BeautifulSoup(html_text, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html_text, "html.parser")


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------
async def _ddg(query: str, count: int) -> list[SearchResult]:
    """DuckDuckGo HTML endpoint (no key)."""
    async with _client() as c:
        r = await c.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "cn-zh"},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        body = r.text

    out: list[SearchResult] = []
    blocks = re.split(r'class="result results_links', body)[1:]
    for b in blocks:
        m = re.search(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S
        )
        if not m:
            continue
        url, title = m.group(1), _clean(m.group(2))
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        snippet = _clean(sn.group(1)) if sn else ""
        url = html.unescape(url)
        if "/l/?uddg=" in url:
            q = re.search(r"uddg=([^&]+)", url)
            if q:
                from urllib.parse import unquote

                url = unquote(q.group(1))
        if title:
            out.append(SearchResult(title, url, snippet, "duckduckgo"))
        if len(out) >= count:
            break
    return out


async def _bing(query: str, count: int) -> list[SearchResult]:
    """Bing HTML endpoint (no key)."""
    async with _client() as c:
        r = await c.get(
            "https://www.bing.com/search",
            params={"q": query, "count": count * 2, "form": "QBLH"},
        )
        r.raise_for_status()
        soup = _soup(r.text)

    out: list[SearchResult] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        url = html.unescape(a.get("href", ""))
        cap = li.select_one(".b_caption p") or li.select_one("p")
        snippet = cap.get_text(" ", strip=True) if cap else ""
        if title and url:
            out.append(SearchResult(title, url, snippet, "bing"))
        if len(out) >= count:
            break
    return out


async def _sogou(query: str, count: int) -> list[SearchResult]:
    """Sogou HTML endpoint (no key)."""
    async with _client() as c:
        r = await c.get("https://www.sogou.com/web", params={"query": query})
        r.raise_for_status()
        body = r.text

    out: list[SearchResult] = []
    for m in re.finditer(r'<div class="vrwrap".*?(?=<div class="vrwrap"|$)', body, re.S):
        blk = m.group(0)
        a = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not a:
            continue
        url, title = a.group(1), _clean(a.group(2))
        if url.startswith("/link"):
            url = "https://www.sogou.com" + url
        sn = re.search(r'class="text-layout"[^>]*>(.*?)</div>', blk, re.S)
        if not sn:
            sn = re.search(r'class="space-txt"[^>]*>(.*?)</div>', blk, re.S)
        snippet = _clean(sn.group(1)) if sn else ""
        if title:
            out.append(SearchResult(title, url, snippet, "sogou"))
        if len(out) >= count:
            break
    return out


async def _searx(query: str, count: int) -> list[SearchResult]:
    """Public SearXNG instance with JSON output (no key)."""
    async with _client() as c:
        r = await c.get(
            "https://searx.be/search",
            params={"q": query, "format": "json", "language": "zh-CN"},
        )
        r.raise_for_status()
        data = r.json()
    return [
        SearchResult(
            _clean(i.get("title", "")),
            i.get("url", ""),
            _clean(i.get("content", "")),
            "searx",
        )
        for i in data.get("results", [])[:count]
    ]


async def _tavily(query: str, count: int) -> list[SearchResult]:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("no tavily key")
    async with _client() as c:
        r = await c.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "max_results": count,
                "search_depth": "basic",
                "include_answer": True,
            },
        )
        r.raise_for_status()
        data = r.json()
    out: list[SearchResult] = []
    if data.get("answer"):
        out.append(SearchResult("AI 摘要", "", str(data["answer"])[:800], "tavily"))
    for i in data.get("results", []):
        out.append(
            SearchResult(i.get("title", ""), i.get("url", ""), i.get("content", ""), "tavily")
        )
    return out


async def _bocha(query: str, count: int) -> list[SearchResult]:
    key = os.environ.get("BOCHA_API_KEY")
    if not key:
        raise RuntimeError("no bocha key")
    async with _client() as c:
        r = await c.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "count": count, "summary": True},
        )
        r.raise_for_status()
        data = r.json()
    out: list[SearchResult] = []
    for p in (data.get("data") or {}).get("webPages", {}).get("value", []):
        out.append(
            SearchResult(p.get("name", ""), p.get("url", ""), p.get("summary", ""), "bocha")
        )
    return out


PROVIDERS: dict[str, Any] = {
    "tavily": _tavily,
    "bocha": _bocha,
    "bing": _bing,
    "duckduckgo": _ddg,
    "sogou": _sogou,
    "searx": _searx,
}


def _provider_order() -> list[str]:
    order = os.environ.get("SEARCH_PROVIDER_ORDER", "")
    if order:
        return [p.strip() for p in order.split(",") if p.strip() in PROVIDERS]
    keys = [k for k, fn in (("tavily", _tavily), ("bocha", _bocha)) if os.environ.get(f"{k.upper()}_API_KEY")]
    keys += ["bing", "duckduckgo", "sogou", "searx"]
    return keys


async def web_search(query: str, count: int = 5) -> list[SearchResult]:
    """Try providers in order, return the first non-empty result."""
    errors: list[str] = []
    for name in _provider_order():
        fn = PROVIDERS[name]
        try:
            res = await asyncio.wait_for(fn(query, count), timeout=DEFAULT_TIMEOUT + 4)
            if res:
                return res
            errors.append(f"{name}: empty")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")
    raise RuntimeError("all search providers failed -> " + ", ".join(errors))


# --------------------------------------------------------------------------
# Page fetching
# --------------------------------------------------------------------------
async def fetch_url(url: str, max_chars: int = 4000) -> str:
    async with _client() as c:
        r = await c.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        body = r.text

    if "json" in ctype:
        try:
            return json.dumps(r.json(), ensure_ascii=False)[:max_chars]
        except Exception:  # noqa: BLE001
            pass

    body = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", body)
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", body)
    if m:
        body = m.group(1)
    text = _clean(body)
    return text[:max_chars]


# --------------------------------------------------------------------------
# Weather (Open-Meteo, no key)
# --------------------------------------------------------------------------
WMO = {
    0: "晴", 1: "多云间晴", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨",
    65: "大雨", 66: "冻雨(小)", 67: "冻雨(大)", 71: "小雪", 73: "中雪",
    75: "大雪", 77: "米雪", 80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪", 95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}


async def get_weather(city: str) -> str:
    async with _client() as c:
        g = await c.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
        )
        g.raise_for_status()
        gd = g.json()
        if not gd.get("results"):
            raise RuntimeError(f"未找到城市: {city}")
        loc = gd["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        name = loc.get("name", city)
        country = loc.get("country", "")

        w = await c.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                "timezone": "Asia/Shanghai",
                "forecast_days": 3,
            },
        )
        w.raise_for_status()
        wd = w.json()

    cur = wd.get("current", {})
    daily = wd.get("daily", {})
    code = cur.get("weather_code", 0)
    lines = [
        f"{name}{(' / ' + country) if country else ''} 当前天气：{WMO.get(code, '未知')}",
        f"气温 {cur.get('temperature_2m')}°C，湿度 {cur.get('relative_humidity_2m')}%，"
        f"风速 {cur.get('wind_speed_10m')} km/h",
    ]
    if daily.get("time"):
        lines.append("未来三天：")
        for i, d in enumerate(daily["time"]):
            c2 = daily.get("weather_code", [0, 0, 0])[i]
            lines.append(
                f"  {d}: {WMO.get(c2, '未知')} "
                f"{daily.get('temperature_2m_min', [0, 0, 0])[i]}~"
                f"{daily.get('temperature_2m_max', [0, 0, 0])[i]}°C"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Stock quote (Tencent + Eastmoney public endpoints, no key)
# --------------------------------------------------------------------------
def _secid(code: str) -> str:
    code = code.strip().lower()
    code = re.sub(r"^(sh|sz|bj)", "", code)
    if code.startswith(("6", "9", "11", "5")):
        return "1." + code
    if code.startswith(("4", "8")):
        return "0." + code
    return "0." + code


async def get_stock_price(code: str) -> str:
    """A-share / HK / US quote via public endpoints."""
    raw = code.strip()
    secid = _secid(raw)
    async with _client() as c:
        r = await c.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": secid,
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170,f168,f171",
            },
        )
        r.raise_for_status()
        d = (r.json() or {}).get("data") or {}
    if not d:
        raise RuntimeError(f"未查到行情: {code}")

    def v(k: str, div: int = 100) -> float | None:
        x = d.get(k)
        if x in (None, "-"):
            return None
        return x / div if div != 1 else x

    name = d.get("f58") or raw
    price = v("f43")
    chg = v("f169")
    pct = v("f170", 1)
    if pct is not None:
        pct = pct / 100
    high, low, openp, prev = v("f44"), v("f45"), v("f46"), v("f60")
    vol = d.get("f47")
    amount = d.get("f48")
    arrow = "涨" if (chg or 0) > 0 else ("跌" if (chg or 0) < 0 else "平")
    return (
        f"{name}({d.get('f57') or raw}) {arrow}\n"
        f"现价 {price} 元，涨跌 {chg} ({pct}%)\n"
        f"今开 {openp}，最高 {high}，最低 {low}，昨收 {prev}\n"
        f"成交量 {vol} 手，成交额 {amount} 元"
    )


# --------------------------------------------------------------------------
# Calculator
# --------------------------------------------------------------------------
_ALLOWED = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
}


def calculate(expression: str) -> str:
    expr = expression.strip()
    if not re.fullmatch(r"[0-9\.\+\-\*/\(\)\s,%a-z_A-Z]+", expr):
        raise RuntimeError("表达式包含不允许的字符")
    import math

    env: dict[str, Any] = {"__builtins__": {}}
    env.update(_ALLOWED)
    env.update({k: getattr(math, k) for k in dir(math) if not k.startswith("_")})
    env.pop("__loader__", None)
    env.pop("__spec__", None)
    result = eval(expr, env, {})  # noqa: S307 - sandboxed above
    return str(result)
