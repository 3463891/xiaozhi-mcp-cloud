"""
End-to-end test: spin up a fake XiaoZhi cloud WebSocket server, let the
bridge connect to it, then run a real MCP handshake (initialize -> tools/list
-> tools/call) exactly like xiaozhi.me does.
"""
import asyncio
import json
import sys

import websockets

from bridge import Instance


async def fake_cloud(ready: asyncio.Event, got: list) -> None:
    async def handler(ws) -> None:
        await ready.wait()
        try:
            # 1) initialize
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "xiaozhi-cloud", "version": "1.0"},
                },
            }))
            line = await asyncio.wait_for(ws.recv(), timeout=15)
            got.append(("initialize", json.loads(line)))

            # 2) initialized notification
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))

            # 3) tools/list
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
            line = await asyncio.wait_for(ws.recv(), timeout=15)
            got.append(("tools/list", json.loads(line)))

            # 4) ping
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
            line = await asyncio.wait_for(ws.recv(), timeout=15)
            got.append(("ping", json.loads(line)))

            # 5) tools/call calculator (no network needed)
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "calculator", "arguments": {"expression": "sqrt(8)*2"}},
            }))
            line = await asyncio.wait_for(ws.recv(), timeout=15)
            got.append(("tools/call:calculator", json.loads(line)))

            # 6) tools/call beijing_time
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "beijing_time", "arguments": {}},
            }))
            line = await asyncio.wait_for(ws.recv(), timeout=15)
            got.append(("tools/call:time", json.loads(line)))

            # 7) tools/call web_search (network)
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"query": "浙能电力 主营业务", "count": 3}},
            }))
            line = await asyncio.wait_for(ws.recv(), timeout=40)
            got.append(("tools/call:web_search", json.loads(line)))

        except Exception as e:  # noqa: BLE001
            got.append(("ERROR", str(e)))
    async with websockets.serve(handler, "127.0.0.1", 8765):
        await asyncio.Future()


async def main() -> int:
    ready = asyncio.Event()
    got: list = []
    cloud = asyncio.create_task(fake_cloud(ready, got))
    await asyncio.sleep(1.5)

    inst = Instance("test-1", "测试实例", "ws://127.0.0.1:8765/mcp/?token=abc123")
    inst.start()

    # wait until the bridge reports a live connection
    for _ in range(60):
        if inst.status == "running":
            break
        await asyncio.sleep(0.25)
    ready.set()

    for _ in range(200):
        if len(got) >= 7:
            break
        await asyncio.sleep(0.25)

    ok = True
    for name, payload in got:
        print(f"\n=== {name} ===")
        if name == "ERROR":
            ok = False
            print(payload)
            continue
        print(json.dumps(payload, ensure_ascii=False)[:600])
        if "error" in payload:
            ok = False
        if name.startswith("tools/call") and name != "tools/call:calculator":
            r = payload.get("result", {})
            if r.get("isError"):
                ok = False
        if name == "tools/list":
            tools = payload.get("result", {}).get("tools", [])
            print("  工具数量:", len(tools))
            print("  工具名:", [t["name"] for t in tools])
            if len(tools) < 6:
                ok = False

    print("\n实例状态:", inst.status, "| 工具调用次数:", inst.tool_calls)
    passed = ok and len(got) >= 6
    print("结果:", "PASS" if passed else "FAIL")
    inst.stop()
    cloud.cancel()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
