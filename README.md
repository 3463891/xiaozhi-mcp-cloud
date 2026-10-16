# 小智 MCP 云平台（自建版）

一套可免费部署在云端的「小智 MCP 网关」，功能对标 <https://www.facaiai.cn/dashboard>：

- 网页控制台：注册 / 登录 / 服务市场 / 实例管理 / 实时日志 / **工具测试**
- 后台常驻进程：主动 WebSocket 外连你的小智智能体 MCP 接入点，注册工具
- 断线自动指数退避重连，7×24 在线
- 内置工具：**云搜索**、网页抓取、股票行情、天气、时间、计算器
- 「工具测试」页可直接在云端调用任意工具并查看返回，用于验证服务是否可用（无需通过语音）

## 原理

小智官方控制台会为每个智能体生成一个 MCP 接入点：

```
wss://api.xiaozhi.me/mcp/?token=xxxxxxxx
```

这个接入点需要**工具提供方主动连上去**（官方示例 `mcp_pipe.py` 就是这么做的）。
本项目把「桥接器 + MCP Server + Web 控制台」合成一个进程，部署到云上即可一直在线。

```
小智云端 (wss://api.xiaozhi.me)  <──WebSocket──  本服务  ──>  搜索引擎 / 行情 / 天气
```

## 内置服务

| 服务 | 工具 | 说明 |
|---|---|---|
| 云搜索 | `web_search` `web_fetch` | 实时联网搜索 + 网页正文抓取 |
| 股票行情 | `get_stock_price` | A 股实时价、涨跌幅、成交量 |
| 生活助手 | `weather_forecast` `beijing_time` | 天气与三天预报、当前北京时间 |
| 计算工具 | `calculator` | 数学表达式求值 |

搜索后端按优先级自动回退：**Tavily**（若配置了 Key）→ Bing → DuckDuckGo → 搜狗 → SearXNG。
免密钥即可开箱使用。

行情后端同样是**三源回退链**：东方财富 → 腾讯 → 新浪。
这一点在境外节点上很关键 —— 实测新加坡节点访问 `push2.eastmoney.com` 会失败，会自动落到腾讯行情。

## 一、获取小智 MCP 接入点

1. 打开 <https://xiaozhi.me/console/login> 登录
2. 右上角创建 / 选择你的智能体
3. 点击【配置】→【扩展能力】→【MCP 接入点】→ 复制地址（形如 `wss://api.xiaozhi.me/mcp/?token=...`）

## 二、部署（三种免费方式，任选其一）

### 方式 A：Render（免费，无需信用卡）

1. 把本仓库 push 到你的 GitHub
2. 打开 <https://dashboard.render.com/blueprints>，用 GitHub 登录
3. 选择本仓库，Render 会自动读取 `render.yaml` 并构建
4. 部署完成后得到 `https://xxx.onrender.com`

> 免费档 15 分钟无访问会休眠。本项目内置自 ping 保活：把 `SELF_URL` 改成你的实际地址即可永不休眠。

### 方式 B：ClawCloud Run（免费、常驻不休眠、新加坡/日本节点）

1. 打开 <https://console.run.claw.cloud/signin>，用 GitHub 登录
2. 进入 App Launchpad → Create App
3. 镜像源填 `ghcr.io/<你的用户名>/xiaozhi-mcp-cloud:main`（仓库已配置 GitHub Actions 自动构建）
4. 端口填 `7860`，开启公网访问 → 得到 HTTPS 域名

### 方式 C：任意支持 Docker 的平台

```bash
docker build -t xiaozhi-mcp-cloud .
docker run -d -p 7860:7860 \
  -e ADMIN_EMAIL=admin@example.com \
  -e ADMIN_PASSWORD=change-me \
  xiaozhi-mcp-cloud
```

## 三、使用

1. 浏览器打开部署好的地址，注册管理员账号并登录
2. 「服务市场」→ 选择「云搜索」→【安装并绑定】
3. 粘贴第一步复制的小智 MCP 接入点 → 安装
4. 实例状态变为「运行中」后，回到 xiaozhi.me 控制台刷新，即可看到工具已接入
5. 用小智说话测试：「帮我搜一下今天的科技新闻」
6. 想先确认服务是否正常，可在控制台「工具测试」页选工具、填参数、点「运行」，
   直接看到云端返回结果 —— 不需要经过小智语音

## 环境变量

| 变量 | 说明 |
|---|---|
| `PORT` | 监听端口，默认 7860 |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | 管理员账号，设置后重启不丢 |
| `SECRET_KEY` | 会话签名密钥，建议设置 |
| `ALLOW_PUBLIC_REGISTER` | 是否开放自助注册，默认 true，公网建议 false |
| `MCP_ENDPOINT` | 可选，启动时自动创建并连接的默认实例 |
| `MCP_SERVICES` | 默认实例启用的服务，逗号分隔 |
| `TAVILY_API_KEY` / `BOCHA_API_KEY` | 可选，搜索质量增强 |
| `SELF_URL` | 自身公网地址，用于防休眠保活 |
| `KEEPALIVE_INTERVAL` | 保活间隔秒数，默认 180 |

## 本地运行

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:7860
```

本地端到端测试（模拟小智云端跑完整 MCP 握手）：

```bash
python test_e2e.py
```
