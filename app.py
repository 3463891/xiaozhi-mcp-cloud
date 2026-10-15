"""
XiaoZhi MCP Cloud - self-hosted replacement for https://www.facaiai.cn/dashboard

Single process that provides:
  * a web dashboard (register / login / instances / service market / logs)
  * a background bridge per instance that connects outward to the XiaoZhi
    MCP endpoint (wss://api.xiaozhi.me/mcp/?token=...) and serves tools
  * a keep-alive pinger so free-tier hosts never put the container to sleep

Run locally:  python app.py
Deploy:       see README.md (HuggingFace Spaces / Render / Koyeb, all free)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

import mcp_server
from bridge import manager
from search_engine import DEFAULT_TIMEOUT

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("xiaozhi-mcp-cloud")

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    probe = DATA_DIR / ".probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)
except Exception:  # noqa: BLE001
    DATA_DIR = Path(os.environ.get("LOCAL_DATA_DIR", "./data"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.warning("/data not writable, falling back to %s", DATA_DIR)

USERS_FILE = DATA_DIR / "users.json"
INSTANCES_FILE = DATA_DIR / "instances.json"
SECRET_FILE = DATA_DIR / ".secret_key"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("failed reading %s: %s", path, e)
    return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.error("failed writing %s: %s", path, e)


def _secret_key() -> bytes:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode()
    key = SECRET_FILE.read_text(encoding="utf-8").strip() if SECRET_FILE.exists() else ""
    if not key:
        key = secrets.token_hex(32)
        try:
            SECRET_FILE.write_text(key, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return key.encode()


SECRET_KEY = _secret_key()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return salt, base64.b64encode(dk).decode()


def verify_password(password: str, salt: str, expected: str) -> bool:
    _, got = hash_password(password, salt)
    return hmac.compare_digest(got, expected)


def sign_session(email: str) -> str:
    payload = f"{email}:{int(time.time()) + 60 * 60 * 24 * 14}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def verify_session(token: str) -> str | None:
    try:
        payload, sig = token.rsplit(":", 1)
        expected = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        email, exp = payload.rsplit(":", 1)
        if int(exp) < time.time():
            return None
        return email
    except Exception:  # noqa: BLE001
        return None


def current_user(request: Request) -> str | None:
    token = request.cookies.get("xz_session")
    return verify_session(token) if token else None


def require_user(request: Request) -> str:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="未登录")
    return u


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="XiaoZhi MCP Cloud", version="1.0.0")


@app.middleware("http")
async def trust_proxy(request: Request, call_next):
    return await call_next(request)


# ------------------------------------------------------------------- auth API
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    running = sum(1 for i in manager.instances.values() if i.status == "running")
    return {"ok": True, "instances": len(manager.instances), "running": running}


@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    users = _read_json(USERS_FILE, {})
    return {"needs_setup": not users, "logged_in": False}


@app.post("/api/auth/register")
async def register(request: Request) -> Any:
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")

    users = _read_json(USERS_FILE, {})
    if users and email in users:
        raise HTTPException(400, "该邮箱已注册")
    if users and os.environ.get("ALLOW_PUBLIC_REGISTER", "true").lower() != "true":
        raise HTTPException(403, "当前站点未开放注册")

    salt, hashed = hash_password(password)
    users[email] = {"salt": salt, "hash": hashed, "created_at": time.time()}
    _write_json(USERS_FILE, users)
    logger.info("registered user %s", email)

    resp = JSONResponse({"ok": True})
    resp.set_cookie("xz_session", sign_session(email), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return resp


@app.post("/api/auth/login")
async def login(request: Request) -> Any:
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    users = _read_json(USERS_FILE, {})
    rec = users.get(email)
    if not rec or not verify_password(password, rec["salt"], rec["hash"]):
        raise HTTPException(401, "邮箱或密码错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("xz_session", sign_session(email), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return resp


@app.post("/api/auth/logout")
async def logout() -> Any:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("xz_session")
    return resp


@app.get("/api/me")
async def me(request: Request) -> Any:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "未登录")
    return {"email": u}


# --------------------------------------------------------------- service API
@app.get("/api/services")
async def services() -> Any:
    return {"services": mcp_server.SERVICE_CATALOG}


# -------------------------------------------------------------- instance API
def _persist() -> None:
    data = [
        {
            "id": i.id,
            "name": i.name,
            "endpoint": i.endpoint,
            "services": i.services,
            "autostart": i.autostart,
        }
        for i in manager.instances.values()
    ]
    _write_json(INSTANCES_FILE, data)


@app.get("/api/instances")
async def list_instances(request: Request) -> Any:
    require_user(request)
    return {"instances": manager.all()}


@app.post("/api/instances")
async def create_instance(request: Request) -> Any:
    require_user(request)
    body = await request.json()
    name = (body.get("name") or "我的小智").strip()
    endpoint = (body.get("endpoint") or "").strip()
    services = body.get("services") or mcp_server.DEFAULT_SERVICES
    if not endpoint:
        raise HTTPException(400, "请填写 MCP 接入点地址")
    if not endpoint.startswith(("ws://", "wss://")):
        raise HTTPException(400, "MCP 接入点必须以 ws:// 或 wss:// 开头")
    bad = [s for s in services if s not in mcp_server.SERVICE_CATALOG]
    if bad:
        raise HTTPException(400, f"未知服务: {', '.join(bad)}")

    inst = manager.add(name, endpoint, services, autostart=True)
    _persist()
    logger.info("created instance %s (%s)", name, inst.id)
    return {"ok": True, "instance": inst.to_dict()}


@app.post("/api/instances/{iid}/start")
async def start_instance(iid: str, request: Request) -> Any:
    require_user(request)
    if not manager.start(iid):
        raise HTTPException(404, "实例不存在")
    return {"ok": True}


@app.post("/api/instances/{iid}/stop")
async def stop_instance(iid: str, request: Request) -> Any:
    require_user(request)
    if not manager.stop(iid):
        raise HTTPException(404, "实例不存在")
    return {"ok": True}


@app.delete("/api/instances/{iid}")
async def delete_instance(iid: str, request: Request) -> Any:
    require_user(request)
    if not manager.remove(iid):
        raise HTTPException(404, "实例不存在")
    _persist()
    return {"ok": True}


@app.get("/api/instances/{iid}/logs")
async def instance_logs(iid: str, request: Request) -> Any:
    require_user(request)
    inst = manager.get(iid)
    if not inst:
        raise HTTPException(404, "实例不存在")
    return {"logs": list(inst.logs)}


# ---------------------------------------------------------------------- page
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小智 MCP 云平台</title>
<style>
:root{
  --bg:#f6f7fb; --card:#ffffff; --line:#e5e7eb; --text:#1f2328; --muted:#6b7280;
  --brand:#4f46e5; --brand-soft:#eef2ff; --ok:#16a34a; --warn:#d97706; --err:#dc2626; --idle:#9ca3af;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
a{color:inherit;text-decoration:none}
header{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
.nav{max-width:1100px;margin:0 auto;padding:0 20px;height:58px;display:flex;align-items:center;gap:26px}
.logo{font-weight:700;font-size:17px;display:flex;align-items:center;gap:8px}
.logo .dot{width:9px;height:9px;border-radius:50%;background:var(--brand)}
.nav a.tab{padding:6px 2px;font-size:14px;color:var(--muted);border-bottom:2px solid transparent}
.nav a.tab.active{color:var(--brand);border-color:var(--brand);font-weight:600}
.spacer{flex:1}
.user{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:10px}
main{max-width:1100px;margin:0 auto;padding:26px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:14px;margin-bottom:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;
 box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card h3{margin:0 0 6px;font-size:16px}
.card .meta{color:var(--muted);font-size:13px;line-height:1.7}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 14px}
.tag{background:var(--brand-soft);color:var(--brand);font-size:12px;padding:3px 9px;border-radius:20px}
.badge{font-size:12px;padding:3px 10px;border-radius:20px;display:inline-flex;align-items:center;gap:6px}
.b-running{background:#dcfce7;color:#15803d}
.b-connecting{background:#fef3c7;color:#b45309}
.b-stopped{background:#f3f4f6;color:#4b5563}
.b-error{background:#fee2e2;color:#b91c1c}
button{font-family:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);
 background:#fff;color:var(--text);padding:7px 14px;font-size:13px;transition:.15s}
button:hover{background:#f3f4f6}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button.primary:hover{background:#4338ca}
button.danger{color:var(--err);border-color:#fecaca}
button.danger:hover{background:#fef2f2}
button:disabled{opacity:.5;cursor:not-allowed}
input,select,textarea{font-family:inherit;font-size:14px;padding:9px 11px;border:1px solid var(--line);
 border-radius:8px;width:100%;background:#fff;color:var(--text);box-sizing:border-box}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(79,70,229,.1)}
label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px;color:#374151}
.empty{text-align:center;padding:56px 20px;color:var(--muted)}
.empty .big{font-size:38px;margin-bottom:10px}
.auth{max-width:400px;margin:8vh auto;background:#fff;border:1px solid var(--line);
 border-radius:14px;padding:32px;box-shadow:0 4px 16px rgba(16,24,40,.06)}
.auth h1{font-size:21px;margin-bottom:6px}
.err{background:#fef2f2;color:#b91c1c;font-size:13px;padding:9px 12px;border-radius:8px;margin-top:14px;display:none}
.stat{display:flex;gap:22px;margin-bottom:20px;flex-wrap:wrap}
.stat div{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 18px}
.stat b{display:block;font-size:20px}
.stat span{font-size:12px;color:var(--muted)}
.logs{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
 background:#0f172a;color:#d1d5db;border-radius:10px;padding:14px;max-height:420px;overflow:auto;line-height:1.65}
.logs .i{color:#93c5fd}.logs .w{color:#fbbf24}.logs .e{color:#fca5a5}
.modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;justify-content:center;z-index:50;padding:20px}
.modal.show{display:flex}
.modal .box{background:#fff;border-radius:14px;padding:26px;width:100%;max-width:480px;max-height:90vh;overflow:auto}
.hint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.6}
.svc{display:flex;gap:13px;align-items:flex-start;margin-bottom:14px}
.svc .ico{font-size:26px;line-height:1}
.foot{text-align:center;color:var(--muted);font-size:12px;margin-top:36px}
</style>
</head>
<body>
<header><div class="nav">
  <div class="logo"><span class="dot"></span>小智 MCP 云</div>
  <a class="tab active" href="#" data-view="dash">仪表盘</a>
  <a class="tab" href="#" data-view="market">服务市场</a>
  <div class="spacer"></div>
  <div class="user" id="userbox"></div>
</div></header>
<main id="app"></main>

<div class="modal" id="modal"><div class="box" id="modalbox"></div></div>

<script>
const $ = (s,d=document)=>d.querySelector(s);
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {view:'dash', user:null, instances:[], services:{}, timer:null, logFor:null};

async function api(path, opts={}){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  if(r.status===401){ state.user=null; render(); return {error:'登录已过期'}; }
  let d={}; try{ d=await r.json(); }catch(e){}
  if(!r.ok) throw new Error(d.detail || d.error || ('HTTP '+r.status));
  return d;
}

// ------------------------------------------------------------------ auth UI
function authView(setup){
  return `<div class="auth">
    <h1>${setup?'创建管理员账号':'登录'}</h1>
    <p class="sub" style="margin-bottom:18px">${setup?'首次使用，请先创建管理员账号':'登录你的小智 MCP 云平台'}</p>
    <label>邮箱</label><input id="email" type="email" placeholder="you@example.com" autocomplete="username">
    <label>密码</label><input id="pwd" type="password" placeholder="至少 6 位" autocomplete="${setup?'new-password':'current-password'}">
    <div class="err" id="errmsg"></div>
    <button class="primary" id="go" style="width:100%;margin-top:18px;padding:10px">${setup?'创建并进入':'登录'}</button>
    ${setup?'':'<p class="hint" style="text-align:center">还没有账号？<a href="#" id="toreg" style="color:var(--brand)">立即注册</a></p>'}
  </div>`;
}

function bindAuth(setup){
  $('#go').onclick = async ()=>{
    const email=$('#email').value.trim(), pwd=$('#pwd').value;
    $('#errmsg').style.display='none';
    if(!email||!pwd){ $('#errmsg').textContent='请填写邮箱和密码'; $('#errmsg').style.display='block'; return; }
    try{
      await api('/api/auth/'+(setup?'register':'login'), {method:'POST', body:JSON.stringify({email,password:pwd})});
      state.user=email; state.view='dash'; await refresh(); render();
    }catch(e){ $('#errmsg').textContent=e.message; $('#errmsg').style.display='block'; }
  };
  const tr=$('#toreg'); if(tr) tr.onclick=()=>{ window.__setup=true; render(); };
}

// ------------------------------------------------------------------- render
function render(){
  const box=$('#userbox');
  if(state.user){
    box.innerHTML = `<span>${esc(state.user)}</span><button id="out">退出</button>`;
    $('#out').onclick = async ()=>{ await api('/api/auth/logout',{method:'POST'}); state.user=null; render(); };
  } else box.innerHTML='';

  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.view===state.view));
  if(!state.user){ $('#app').innerHTML=authView(window.__setup); bindAuth(window.__setup); return; }
  $('#app').innerHTML = state.view==='dash' ? dashView() : marketView();
  bindView();
}

function statusBadge(s){
  const map={running:['b-running','运行中'],connecting:['b-connecting','连接中'],stopped:['b-stopped','已停止'],error:['b-error','错误']};
  const [cls,txt]=map[s]||map.stopped;
  return `<span class="badge ${cls}">${txt}</span>`;
}

function fmtUptime(s){
  if(s==null) return '-';
  const h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return h>0? `${h}小时${m}分` : `${m}分${s%60}秒`;
}

function dashView(){
  const ins=state.instances;
  const running=ins.filter(i=>i.status==='running').length;
  const calls=ins.reduce((a,i)=>a+(i.tool_calls||0),0);
  const cards = ins.length ? ins.map(i=>`
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>${esc(i.name)}</h3>${statusBadge(i.status)}
      </div>
      <div class="tags">${(i.services||[]).map(s=>`<span class="tag">${esc((state.services[s]||{}).name||s)}</span>`).join('')}</div>
      <div class="meta">
        工具数：${i.tool_count ?? '-'} ｜ 工具调用：${i.tool_calls||0} 次 ｜ 重连：${i.reconnects||0} 次<br>
        运行时长：${fmtUptime(i.uptime)}<br>
        接入点：<code style="font-size:11px">${esc(i.endpoint)}</code>
        ${i.last_error? `<br><span style="color:var(--err)">最近错误：${esc(i.last_error)}</span>`:''}
      </div>
      <div class="row" style="margin-top:14px">
        ${i.status==='stopped'
          ? `<button class="primary" data-act="start" data-id="${i.id}">启动</button>`
          : `<button data-act="stop" data-id="${i.id}">停止</button>`}
        <button data-act="logs" data-id="${i.id}">日志</button>
        <button class="danger" data-act="del" data-id="${i.id}">删除</button>
      </div>
    </div>`).join('') : `
    <div class="empty" style="grid-column:1/-1">
      <div class="big">🛰️</div>
      <div>还没有实例</div>
      <div style="margin-top:6px">去「服务市场」安装一个 MCP 服务，例如云搜索</div>
      <button class="primary" style="margin-top:16px" onclick="state.view='market';render()">前往服务市场</button>
    </div>`;

  return `
  <div class="row" style="justify-content:space-between;align-items:flex-end">
    <div><h1>我的实例</h1><div class="sub">管理已安装的小智 MCP 服务连接</div></div>
    <button class="primary" onclick="state.view='market';render()">+ 安装服务</button>
  </div>
  <div class="stat">
    <div><b>${ins.length}</b><span>实例总数</span></div>
    <div><b style="color:var(--ok)">${running}</b><span>运行中</span></div>
    <div><b>${calls}</b><span>工具调用</span></div>
  </div>
  <div class="grid">${cards}</div>
  <div class="foot">数据每 5 秒自动刷新 · 连接断开会自动指数退避重连</div>`;
}

function marketView(){
  const items = Object.entries(state.services).map(([k,s])=>`
    <div class="card">
      <div class="svc">
        <div class="ico">${s.icon||'🧩'}</div>
        <div style="flex:1">
          <h3>${esc(s.name)}</h3>
          <div class="meta">${esc(s.tagline||'')}</div>
        </div>
      </div>
      <div class="meta" style="margin:10px 0 4px">${esc(s.desc||'')}</div>
      <div class="tags">${(s.tools||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>
      <button class="primary" data-install="${k}" style="width:100%">安装并绑定</button>
    </div>`).join('');
  return `<h1>服务市场</h1>
  <div class="sub">选择要安装到小智智能体的 MCP 服务，安装后填入你的小智 MCP 接入点即可生效</div>
  <div class="grid">${items}</div>`;
}

function bindView(){
  document.querySelectorAll('[data-act]').forEach(b=>{
    b.onclick = async ()=>{
      const id=b.dataset.id, act=b.dataset.act;
      if(act==='del'){ if(!confirm('确定删除该实例？')) return; await api('/api/instances/'+id,{method:'DELETE'}); }
      else if(act==='logs'){ return openLogs(id); }
      else await api('/api/instances/'+id+'/'+act,{method:'POST'});
      await refresh(); render();
    };
  });
  document.querySelectorAll('[data-install]').forEach(b=>{
    b.onclick = ()=> openInstall(b.dataset.install);
  });
}

// -------------------------------------------------------------------- modal
function closeModal(){ $('#modal').classList.remove('show'); }

function openInstall(key){
  const s=state.services[key]||{};
  $('#modalbox').innerHTML = `
    <h1 style="font-size:18px;margin-bottom:4px">安装 ${esc(s.name)}</h1>
    <p class="sub" style="margin-bottom:6px">${esc(s.tagline||'')}</p>
    <label>实例名称</label>
    <input id="iname" value="${esc(s.name)}">
    <label>小智 MCP 接入点</label>
    <textarea id="iendpoint" rows="3" placeholder="wss://api.xiaozhi.me/mcp/?token=xxxxxx"></textarea>
    <div class="hint">获取方式：登录 xiaozhi.me 控制台 → 智能体 → 配置角色 → 扩展能力 → MCP 接入点 → 复制</div>
    <div class="err" id="ierr"></div>
    <div class="row" style="margin-top:20px;justify-content:flex-end">
      <button onclick="closeModal()">取消</button>
      <button class="primary" id="iok">安装并启动</button>
    </div>`;
  $('#modal').classList.add('show');
  $('#iok').onclick = async ()=>{
    const name=$('#iname').value.trim(), endpoint=$('#iendpoint').value.trim();
    if(!endpoint){ $('#ierr').textContent='请填写 MCP 接入点'; $('#ierr').style.display='block'; return; }
    try{
      await api('/api/instances',{method:'POST',body:JSON.stringify({name,endpoint,services:[key]})});
      closeModal(); state.view='dash'; await refresh(); render();
    }catch(e){ $('#ierr').textContent=e.message; $('#ierr').style.display='block'; }
  };
}

async function openLogs(id){
  const d = await api('/api/instances/'+id+'/logs');
  const rows = (d.logs||[]).map(l=>`<div><span class="${l.level[0]}">[${l.t}] ${l.level.toUpperCase()}</span> ${esc(l.msg)}</div>`).join('') || '<div>暂无日志</div>';
  $('#modalbox').innerHTML = `<h1 style="font-size:18px;margin-bottom:14px">实例日志</h1><div class="logs">${rows}</div>
    <div class="row" style="margin-top:18px;justify-content:flex-end"><button onclick="closeModal()">关闭</button><button class="primary" onclick="openLogs('${id}')">刷新</button></div>`;
  $('#modal').classList.add('show');
}

$('#modal').onclick = e => { if(e.target.id==='modal') closeModal(); };
document.querySelectorAll('.tab').forEach(t=> t.onclick = e=>{ e.preventDefault(); state.view=t.dataset.view; render(); });

async function refresh(){
  try{
    const [i,s] = await Promise.all([api('/api/instances'), api('/api/services')]);
    state.instances=i.instances||[]; state.services=s.services||{};
  }catch(e){}
}
async function boot(){
  const b = await api('/api/bootstrap');
  try{ const m = await api('/api/me'); state.user = m.email; }catch(e){}
  window.__setup = !!b.needs_setup;
  await refresh(); render();
  setInterval(async ()=>{ if(state.user){ await refresh(); render(); } }, 5000);
}
boot();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


# ------------------------------------------------------------------ lifecycle
async def keep_alive() -> None:
    """Ping our own /healthz so free tiers (HF Spaces, Render) never sleep."""
    url = (
        os.environ.get("SELF_URL")
        or (f"https://{os.environ['SPACE_HOST']}" if os.environ.get("SPACE_HOST") else "")
        or os.environ.get("RENDER_EXTERNAL_URL", "")
    )
    if not url:
        logger.info("keep-alive disabled (no SELF_URL / SPACE_HOST / RENDER_EXTERNAL_URL)")
        return
    url = url.rstrip("/") + "/healthz"
    interval = float(os.environ.get("KEEPALIVE_INTERVAL", "180"))
    logger.info("keep-alive enabled -> %s every %.0fs", url, interval)
    async with httpx.AsyncClient(timeout=10) as c:
        while True:
            try:
                await asyncio.sleep(interval)
                await c.get(url)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("keep-alive ping failed: %s", e)


@app.on_event("startup")
async def on_startup() -> None:
    # auto-provision an admin account from env, convenient for one-click deploys
    email = os.environ.get("ADMIN_EMAIL")
    pwd = os.environ.get("ADMIN_PASSWORD")
    if email and pwd:
        users = _read_json(USERS_FILE, {})
        if email.lower() not in users:
            salt, hashed = hash_password(pwd)
            users[email.lower()] = {"salt": salt, "hash": hashed, "created_at": time.time()}
            _write_json(USERS_FILE, users)
            logger.info("admin account provisioned: %s", email)

    # restore persisted instances
    saved = _read_json(INSTANCES_FILE, [])
    for item in saved:
        try:
            manager.add(
                item["name"],
                item["endpoint"],
                item.get("services") or mcp_server.DEFAULT_SERVICES,
                autostart=bool(item.get("autostart", True)),
                instance_id=item["id"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("skip broken instance: %s", e)

    # one-shot env instance (survives container restarts on stateless hosts)
    ep = os.environ.get("MCP_ENDPOINT") or os.environ.get("XIAOZHI_MCP_ENDPOINT")
    if ep:
        want = os.environ.get("MCP_SERVICES") or ",".join(mcp_server.DEFAULT_SERVICES)
        services = [s.strip() for s in want.split(",") if s.strip() in mcp_server.SERVICE_CATALOG]
        if not any(i.endpoint.strip() == ep.strip() for i in manager.instances.values()):
            inst = manager.add("默认实例", ep, services or mcp_server.DEFAULT_SERVICES)
            _persist()
            logger.info("env instance created: %s", inst.id)

    manager.autostart_all()
    asyncio.create_task(keep_alive())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
