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
from urllib.parse import quote

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

# wttr.in echoes English back in its lang_zh field, so translate ourselves.
WTTR_ZH = {
    "sunny": "晴", "clear": "晴",
    "partly cloudy": "晴间多云", "cloudy": "多云", "overcast": "阴",
    "mist": "薄雾", "fog": "雾", "freezing fog": "冻雾",
    "haze": "霾", "smoky haze": "烟霾", "smoke": "烟",
    "patchy rain nearby": "局部有雨", "patchy rain possible": "局部有雨",
    "light drizzle": "小毛毛雨", "patchy light drizzle": "局部小毛毛雨",
    "freezing drizzle": "冻毛毛雨", "light freezing rain": "小冻雨",
    "heavy freezing rain": "大冻雨", "light rain": "小雨",
    "moderate rain": "中雨", "heavy rain": "大雨",
    "moderate rain at times": "间歇中雨", "heavy rain at times": "间歇大雨",
    "light rain shower": "小阵雨", "moderate or heavy rain shower": "中到大阵雨",
    "torrential rain shower": "暴雨", "thundery outbreaks possible": "可能有雷雨",
    "thundery outbreaks in nearby": "附近有雷雨", "patchy light rain with thunder": "局部雷阵雨",
    "moderate or heavy rain with thunder": "中到大雷雨",
    "light snow": "小雪", "moderate snow": "中雪", "heavy snow": "大雪",
    "patchy snow possible": "局部有雪", "blizzard": "暴风雪",
    "light snow showers": "小阵雪", "moderate or heavy snow showers": "中到大阵雪",
    "sleet": "雨夹雪", "light sleet": "小雨夹雪", "moderate or heavy sleet": "中到大雨夹雪",
    "patchy sleet possible": "局部雨夹雪", "light freezing rain showers": "小冻雨",
    "ice pellets": "冰粒",
}


def _zh_weather(desc: str) -> str:
    """Map a wttr.in English description onto Chinese, with a few normalisations."""
    d = (desc or "").strip().lower()
    if not d:
        return "未知"
    if d in WTTR_ZH:
        return WTTR_ZH[d]
    for en, zh in WTTR_ZH.items():
        if en in d:
            return zh
    return desc


async def _weather_openmeteo(c: httpx.AsyncClient, city: str) -> str | None:
    g = await c.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "zh", "format": "json"},
    )
    g.raise_for_status()
    gd = g.json()
    if not gd.get("results"):
        return None
    loc = gd["results"][0]

    w = await c.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "timezone": "Asia/Shanghai",
            "forecast_days": 3,
        },
    )
    w.raise_for_status()
    wd = w.json()

    name = loc.get("name", city)
    country = loc.get("country", "")
    cur = wd.get("current", {})
    daily = wd.get("daily", {})
    lines = [
        f"{name}{(' / ' + country) if country else ''} 当前天气：{WMO.get(cur.get('weather_code', 0), '未知')}",
        f"气温 {cur.get('temperature_2m')}°C，湿度 {cur.get('relative_humidity_2m')}%，"
        f"风速 {cur.get('wind_speed_10m')} km/h",
    ]
    if daily.get("time"):
        lines.append("未来三天：")
        for i, d in enumerate(daily["time"]):
            code = (daily.get("weather_code") or [0])[i]
            lo = (daily.get("temperature_2m_min") or ["?"])[i]
            hi = (daily.get("temperature_2m_max") or ["?"])[i]
            lines.append(f"  {d}: {WMO.get(code, '未知')} {lo}~{hi}°C")
    return "\n".join(lines)


async def _weather_wttr(c: httpx.AsyncClient, city: str) -> str | None:
    """Fallback provider. wttr.in needs no key and is rarely rate-limited."""
    r = await c.get(
        f"https://wttr.in/{quote(city)}",
        params={"format": "j1", "lang": "zh"},
    )
    r.raise_for_status()
    try:
        d = r.json()
    except Exception:  # noqa: BLE001
        return None
    cc = (d.get("current_condition") or [{}])[0]
    if not cc:
        return None

    def zh(obj: dict[str, Any]) -> str:
        v = obj.get("weatherDesc")
        if v and isinstance(v, list) and v[0].get("value"):
            return _zh_weather(v[0]["value"])
        return "未知"

    area = (d.get("nearest_area") or [{}])[0]

    def area_val(key: str) -> str:
        v = area.get(key)
        if v and isinstance(v, list) and v[0].get("value"):
            return v[0]["value"]
        return ""

    # wttr.in returns romanised area names; the user's own input reads better
    name = city or area_val("areaName")
    country = area_val("country")
    lines = [
        f"{name}{(' / ' + country) if country else ''} 当前天气：{zh(cc)}",
        f"气温 {cc.get('temp_C')}°C，湿度 {cc.get('humidity')}%，"
        f"风速 {cc.get('windspeedKmph')} km/h",
    ]
    days = (d.get("weather") or [])[:3]
    if days:
        lines.append("未来三天：")
        for day in days:
            hourly = day.get("hourly") or [{}]
            noon = hourly[4] if len(hourly) > 4 else hourly[0]
            lines.append(
                f"  {day.get('date')}: {zh(noon)} "
                f"{day.get('mintempC')}~{day.get('maxtempC')}°C"
            )
    return "\n".join(lines)


async def get_weather(city: str) -> str:
    """Weather with a provider fallback chain (Open-Meteo -> wttr.in).

    Open-Meteo rate-limits per source IP (429); shared cloud egress IPs hit that
    fast, so a second independent provider is required for reliability.
    """
    city = (city or "").strip()
    if not city:
        raise RuntimeError("请提供城市名")
    errors: list[str] = []
    async with _client() as c:
        for name, fn in (("open-meteo", _weather_openmeteo), ("wttr.in", _weather_wttr)):
            try:
                out = await fn(c, city)
                if out:
                    return out
                errors.append(f"{name}:empty")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}:{type(e).__name__}")
    raise RuntimeError(f"未查到天气: {city}（数据源均失败：{', '.join(errors)}）")


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


def _prefix(code: str) -> str:
    """sh600023 / sz000001 / bj430047 for Tencent + Sina endpoints."""
    c = re.sub(r"^(sh|sz|bj)", "", code.strip().lower())
    if c.startswith(("6", "9", "11", "5")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


def _f(x: Any) -> float | None:
    try:
        out = float(x)
    except (TypeError, ValueError):
        return None
    return out


def _px(x: Any) -> str:
    return "—" if x is None else f"{x:.2f}"


def _money(x: Any) -> str:
    if x is None:
        return "—"
    if x >= 1e8:
        return f"{x / 1e8:.2f} 亿元"
    if x >= 1e4:
        return f"{x / 1e4:.2f} 万元"
    return f"{x:.0f} 元"


def _fmt_quote(name, code, price, chg, pct, openp, high, low, prev, vol, amount) -> str:
    arrow = "涨" if (chg or 0) > 0 else ("跌" if (chg or 0) < 0 else "平")
    pct_s = "—" if pct is None else f"{pct:+.2f}"
    vol_s = "—" if vol is None else f"{int(vol):,}"
    return (
        f"{name}({code}) {arrow}\n"
        f"现价 {_px(price)} 元，涨跌 {_px(chg)} ({pct_s}%)\n"
        f"今开 {_px(openp)}，最高 {_px(high)}，最低 {_px(low)}，昨收 {_px(prev)}\n"
        f"成交量 {vol_s} 手，成交额 {_money(amount)}"
    )


async def _quote_eastmoney(c: httpx.AsyncClient, raw: str) -> str | None:
    r = await c.get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params={
            "secid": _secid(raw),
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
        },
    )
    r.raise_for_status()
    d = (r.json() or {}).get("data") or {}
    if not d:
        return None

    def v(k: str, div: int = 100) -> float | None:
        x = d.get(k)
        if x in (None, "-"):
            return None
        return x / div if div != 1 else x

    pct = v("f170", 1)
    if pct is not None:
        pct = pct / 100
    return _fmt_quote(
        d.get("f58") or raw, d.get("f57") or raw,
        v("f43"), v("f169"), pct, v("f46"), v("f44"), v("f45"), v("f60"),
        d.get("f47"), d.get("f48"),
    )


async def _quote_tencent(c: httpx.AsyncClient, raw: str) -> str | None:
    r = await c.get(f"https://qt.gtimg.cn/q={_prefix(raw)}",
                    headers={"Referer": "https://gu.qq.com/"})
    r.raise_for_status()
    m = re.search(r'="([^"]+)"', r.content.decode("gbk", "replace"))
    if not m:
        return None
    f = m.group(1).split("~")
    if len(f) < 38 or not f[1]:
        return None
    amount_wan = _f(f[37])
    return _fmt_quote(
        f[1], f[2] or raw,
        _f(f[3]), _f(f[31]), _f(f[32]), _f(f[5]), _f(f[33]), _f(f[34]), _f(f[4]),
        _f(f[36]), (amount_wan * 10000) if amount_wan is not None else None,
    )


async def _quote_sina(c: httpx.AsyncClient, raw: str) -> str | None:
    sym = _prefix(raw)
    r = await c.get(f"https://hq.sinajs.cn/list={sym}",
                    headers={"Referer": "https://finance.sina.com.cn"})
    r.raise_for_status()
    m = re.search(r'="([^"]*)"', r.content.decode("gbk", "replace"))
    if not m or not m.group(1).strip():
        return None
    f = m.group(1).split(",")
    if len(f) < 32 or not f[0]:
        return None
    price, prev = _f(f[3]), _f(f[2])
    chg = (price - prev) if (price is not None and prev is not None) else None
    pct = (chg / prev * 100) if (chg is not None and prev) else None
    vol_shares = _f(f[8])
    return _fmt_quote(
        f[0], sym[2:], price, chg, pct, _f(f[1]), _f(f[4]), _f(f[5]), prev,
        (vol_shares / 100) if vol_shares is not None else None, _f(f[9]),
    )


async def get_stock_price(code: str) -> str:
    """A-share quote with a 3-provider fallback chain (Eastmoney -> Tencent -> Sina).

    A single provider can be geo-blocked or rate-limited, so never trust just one.
    """
    raw = code.strip()
    errors: list[str] = []
    async with _client() as c:
        for name, fn in (("eastmoney", _quote_eastmoney),
                         ("tencent", _quote_tencent),
                         ("sina", _quote_sina)):
            try:
                out = await fn(c, raw)
                if out:
                    return out
                errors.append(f"{name}:empty")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}:{type(e).__name__}")
    raise RuntimeError(f"未查到行情 {code}（数据源均失败：{', '.join(errors)}）")


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
