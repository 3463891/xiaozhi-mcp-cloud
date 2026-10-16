"""
WebSocket bridge: connects outward to a XiaoZhi MCP endpoint and speaks
JSON-RPC over it, with exponential-backoff reconnect and per-instance state.

Mirrors the official `mcp_pipe.py` behaviour (connect -> serve -> retry) but
runs in-process and exposes status/logs so the dashboard can drive it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any, Callable, Deque

import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from mcp_server import MCPServer

logger = logging.getLogger("bridge")

INITIAL_BACKOFF = float(os.environ.get("RECONNECT_MIN", "2"))
MAX_BACKOFF = float(os.environ.get("RECONNECT_MAX", "300"))
PING_INTERVAL = float(os.environ.get("WS_PING_INTERVAL", "20"))
PING_TIMEOUT = float(os.environ.get("WS_PING_TIMEOUT", "25"))
MAX_LOG_LINES = 300


class Instance:
    """One MCP bridge instance bound to one XiaoZhi agent endpoint."""

    def __init__(
        self,
        instance_id: str,
        name: str,
        endpoint: str,
        services: list[str] | None = None,
        autostart: bool = True,
    ) -> None:
        self.id = instance_id
        self.name = name
        self.endpoint = endpoint
        self.services = services or list(
            __import__("mcp_server").DEFAULT_SERVICES
        )
        self.autostart = autostart
        self.status = "stopped"  # stopped | connecting | running | error
        self.last_error = ""
        self.started_at: float | None = None
        self.connected_at: float | None = None
        self.reconnects = 0
        self.tool_calls = 0
        self.logs: Deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ logs
    def log(self, level: str, message: str) -> None:
        entry = {"t": time.strftime("%H:%M:%S"), "level": level, "msg": message}
        self.logs.append(entry)
        logger.log(
            {"info": logging.INFO, "error": logging.ERROR, "warn": logging.WARNING}.get(
                level, logging.INFO
            ),
            "[%s] %s",
            self.name,
            message,
        )

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self.status = "connecting"
        self.log("info", "实例启动中，准备连接小智 MCP 接入点")
        self._task = asyncio.create_task(self._run(), name=f"inst-{self.id}")

    def stop(self) -> None:
        self.status = "stopped"
        self.log("info", "实例已停止")
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.started_at = None
        self.connected_at = None

    def _build_server(self) -> MCPServer:
        srv = MCPServer()
        srv.set_services(self.services)
        # wrap handlers to count calls
        for t in srv.tools.values():
            orig = t.handler
            if asyncio.iscoroutinefunction(orig):

                async def wrapped(args, _o=orig, _i=self):
                    _i.tool_calls += 1
                    _i.log("info", f"调用工具 {_o.__name__[3:] if _o.__name__.startswith('_t_') else _o.__name__}")
                    return await _o(args)

            else:

                def wrapped(args, _o=orig, _i=self):  # type: ignore[misc]
                    _i.tool_calls += 1
                    _i.log("info", "调用工具")
                    return _o(args)

            t.handler = wrapped
        return srv

    async def _run(self) -> None:
        attempt = 0
        backoff = INITIAL_BACKOFF
        self.started_at = time.time()
        while not self._stop.is_set():
            try:
                if attempt > 0:
                    self.status = "connecting"
                    self.log("warn", f"{backoff:.0f}s 后进行第 {attempt} 次重连")
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    if self._stop.is_set():
                        break

                await self._session()
                backoff = INITIAL_BACKOFF
                attempt = 0
                if self._stop.is_set():
                    break
                attempt = 1
                backoff = INITIAL_BACKOFF
            except asyncio.CancelledError:
                raise
            except (InvalidURI,) as e:
                self.status = "error"
                self.last_error = str(e)
                self.log("error", f"MCP 接入点地址无效：{e}")
                break
            except Exception as e:  # noqa: BLE001
                attempt += 1
                self.reconnects += 1
                self.status = "connecting"
                self.last_error = f"{type(e).__name__}: {e}"
                self.log("warn", f"连接断开（{type(e).__name__}），准备重连")
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _session(self) -> None:
        uri = self.endpoint.strip()
        if not uri:
            raise InvalidURI("empty endpoint")
        server = self._build_server()
        self.log("info", f"正在连接 {self._mask(uri)}")
        async with websockets.connect(
            uri,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            close_timeout=10,
            max_size=None,
        ) as ws:
            self.status = "running"
            self.connected_at = time.time()
            self.last_error = ""
            self.log("info", "已连接小智 MCP 接入点，工具已注册：" + ", ".join(server.tools))
            async for message in ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", "replace")
                for line in message.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if os.environ.get("MCP_DEBUG_FRAMES", "1") == "1":
                        logger.info("MCP <= %s", line[:600])
                    reply = await server.handle(line)
                    if reply:
                        if os.environ.get("MCP_DEBUG_FRAMES", "1") == "1":
                            logger.info("MCP => %s", reply[:600])
                        await ws.send(reply)
        self.status = "connecting"

    @staticmethod
    def _mask(uri: str) -> str:
        """Show the endpoint with the token truncated, for logs and the API.

        Rebuilt by hand rather than via urlencode() so the mask reads as
        `?token=eyJh***` instead of `?token=eyJh%2A%2A%2A`.
        """
        try:
            from urllib.parse import parse_qsl, urlsplit

            p = urlsplit(uri)
            parts = []
            for k, v in parse_qsl(p.query):
                if k.lower() == "token" and v:
                    v = v[:4] + "***"
                parts.append(f"{k}={v}")
            query = "&".join(parts)
            return f"{p.scheme}://{p.netloc}{p.path}?{query}" if query else f"{p.scheme}://{p.netloc}{p.path}"
        except Exception:  # noqa: BLE001
            return uri

    # ------------------------------------------------------------------ i/o
    def to_dict(self) -> dict[str, Any]:
        uptime = None
        if self.connected_at:
            uptime = int(time.time() - self.connected_at)
        return {
            "id": self.id,
            "name": self.name,
            "endpoint": self._mask(self.endpoint),
            "services": self.services,
            "status": self.status,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "uptime": uptime,
            "reconnects": self.reconnects,
            "tool_calls": self.tool_calls,
            "tool_count": len(
                [t for t in __import__("mcp_server").ALL_TOOLS if t.service in self.services]
            ),
        }


class InstanceManager:
    def __init__(self) -> None:
        self.instances: dict[str, Instance] = {}

    def add(
        self,
        name: str,
        endpoint: str,
        services: list[str] | None = None,
        autostart: bool = True,
        instance_id: str | None = None,
    ) -> Instance:
        import uuid

        iid = instance_id or (name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:6])
        inst = Instance(iid, name, endpoint, services, autostart)
        self.instances[iid] = inst
        if autostart:
            inst.start()
        return inst

    def get(self, iid: str) -> Instance | None:
        return self.instances.get(iid)

    def remove(self, iid: str) -> bool:
        inst = self.instances.pop(iid, None)
        if inst:
            inst.stop()
            return True
        return False

    def start(self, iid: str) -> bool:
        inst = self.instances.get(iid)
        if not inst:
            return False
        inst.start()
        return True

    def stop(self, iid: str) -> bool:
        inst = self.instances.get(iid)
        if not inst:
            return False
        inst.stop()
        return True

    def all(self) -> list[dict[str, Any]]:
        return [i.to_dict() for i in self.instances.values()]

    def autostart_all(self) -> None:
        for i in self.instances.values():
            if i.autostart:
                i.start()


manager = InstanceManager()
