# 小智 MCP 自托管平台（facaiai 平替·永久免费版）

> 功能与 facaiai.cn 完全等价的 MCP 工具桥，部署在你自己手里——**没有有效期，没有账号依赖**。
> 当前已挂载到你的小智智能体「小十三定制款」上，提供 3 个免费工具：

| 工具 | 说明 | 数据源（全部免 API key） |
|---|---|---|
| `cloud_search` | 联网搜索（标题/摘要/链接） | DuckDuckGo → 百度 → Bing → SearXNG 四源容灾 |
| `get_stock_quote` | A股/港股/美股实时行情（含 PE/PB/市值/换手） | 腾讯行情接口 |
| `get_exchange_rate` | 货币汇率 | Frankfurter API |

对小智说这些话即可触发：
- "帮我搜一下今天的 AI 新闻" → 云搜索
- "查一下浙能电力的股价" → 股票行情
- "现在美元兑人民币多少" → 汇率

## 架构原理

```
小智设备 ──> xiaozhi.me 智能体（小十三定制款）
                 │  MCP 接入点 wss://api.xiaozhi.me/mcp/?token=...（你的 token，有效期至 2027-10-07）
                 ▼
       本机常驻 bridge（xiaozhi_mcp_bridge.py，开机自启）
                 │  主动出站 WebSocket，无需公网 IP
                 ▼
       DuckDuckGo / 百度 / 腾讯行情 / Frankfurter
```

小智云端是 MCP 客户端，bridge 主动连上接入点后，云端会下发 `initialize → tools/list`，
对话中 LLM 决定调用工具时下发 `tools/call`，bridge 执行并返回结果——与 facaiai 实例的工作方式完全一致。

## 当前部署状态（2026-10-15 已上线运行）

- **进程**：Windows 计划任务 `XiaoZhi-MCP-Bridge`，登录自启，pythonw 无窗口后台运行
- **配置**：接入点在 `config.json`（从你 facaiai 实例绑定的地址直接复用，token 到 2027-10-07）
- **日志**：`bridge.log`（滚动，2MB × 2 份）
- **依赖**：venv `~/.workbuddy/binaries/python/envs/default`（websockets/httpx/bs4）

### 日常管理命令（管理员 PowerShell）

```powershell
# 看运行状态
Get-ScheduledTask -TaskName "XiaoZhi-MCP-Bridge"
# 看实时日志
Get-Content "$HOME\WorkBuddy\2026-10-15-10-11-20\xiaozhi-mcp-cloud\bridge.log" -Tail 30 -Encoding UTF8 -Wait
# 重启服务
Stop-ScheduledTask -TaskName "XiaoZhi-MCP-Bridge"; Start-ScheduledTask -TaskName "XiaoZhi-MCP-Bridge"
```

### 修改/新增工具

编辑 `xiaozhi_mcp_bridge.py`：
1. 写一个 `async def tool_xxx(args: dict) -> dict` 函数，返回 `ok(结果)` / `err(原因)`
2. 在 `TOOL_DESCRIPTIONS` 里加描述（name/description/inputSchema）
3. 在 `TOOL_HANDLERS` 里注册
4. 重启服务即可

## 迁移到云端（可选，"网上部署"形态）

**前置条件**：能访问 huggingface.co 的网络（开 VPN）+ 一个 HF 账号 token。

```powershell
pip install huggingface_hub   # 装进 venv
python deploy_hf.py --hf-token hf_xxxx
```

一键完成后在 HF Spaces 运行（永久免费 Docker 容器）。云端上线后停本地：

```powershell
schtasks /Change /TN "XiaoZhi-MCP-Bridge" /DISABLE
```

注意：**同一接入点建议只保留一个客户端在线**，本地/云端二选一，避免行为不可预期的互踢。

## facaiai 说明

- 你的 facaiai 账户有效期到 **2027/1/13**，实例"联网搜索"(ID:1229) 可以在它后台停掉（不再需要）
- 它在你智能体上挂的旧工具与本服务无冲突；停掉 facaiai 实例后，本服务是唯一工具来源
- 接入点 token 本身是小智的（facaiai 只是借用绑定），token 有效期到 **2027-10-07**，在 xiaozhi.me 控制台可重置/续期

## 文件清单

| 文件 | 用途 |
|---|---|
| `xiaozhi_mcp_bridge.py` | 主服务（单文件，本地/云端通用） |
| `config.json` | 接入点配置（敏感，勿外传） |
| `test_server.py` | 协议集成自测（模拟小智云端，5 项断言） |
| `Dockerfile` / `README_HF.md` / `deploy_hf.py` | 云端部署包 |
| `cdp_probe.py` / `facaiai_probe.py` | 取接入点用的 CDP 探针（一次性工具） |
| `bridge.log` | 运行日志 |
