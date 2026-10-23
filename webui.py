#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web UI —— 服务器跑 Agent，手机浏览器访问。

为什么这么设计：
  安卓上没有 Docker，打包成 APK 必然把隔离降级为本地执行。
  与其在手机上裸跑 AI 生成的代码，不如让它在服务器（有真正 Docker 隔离）上跑，
  手机只做交互 —— 功能一样，安全性不打折。

架构：
  浏览器 ──SSE──> webui (stdlib http.server)
                    │
                    ├─ AgentSession：后台线程跑 ds_agent_pro.run()
                    └─ 介入通道：卡住时推问题给浏览器，等用户点选

纯标准库（http.server + threading），不引入 Flask / FastAPI，
保持项目一贯的零依赖风格。

启动：
    DEEPSEEK_API_KEY=sk-xxx python3 ds_agent_pro.py --serve 8080
    手机浏览器打开 http://<服务器IP>:8080

安全：
  · 必须设 DS_WEB_TOKEN，否则拒绝启动（这个服务能执行代码）
  · 默认只监听 127.0.0.1，要局域网访问需显式 DS_WEB_HOST=0.0.0.0
"""

import os
import io
import re
import sys
import json
import time
import queue
import shutil
import threading
import contextlib
import http.server
import socketserver
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ============================ 会话 ============================
class Tee:
    """把 print 的输出同时送到终端和事件队列。"""

    def __init__(self, emit):
        self.emit = emit
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.emit(line)
        return len(s)

    def flush(self):
        if self.buf:
            self.emit(self.buf)
            self.buf = ""


class AgentSession:
    """
    一次任务的执行会话。后台线程跑主循环，输出进队列，介入走应答通道。
    """

    def __init__(self, task, emit):
        self.task = task
        self.events = queue.Queue()
        self.emit = emit or (lambda t, k="log": self.events.put({"t": t, "k": k}))
        self.thread = None
        self.done = threading.Event()
        self._ask = None              # 当前待答的介入请求
        self._answer = None
        self._answered = threading.Event()
        self._cancel = threading.Event()
        self.result = None

    # ---------- 输出 ----------
    def log(self, text, kind="log"):
        for line in str(text).split("\n"):
            self.emit(line, kind)

    # ---------- 介入通道 ----------
    def asker(self, question):
        """
        给 Intervention 用。把问题推给浏览器，阻塞等应答。
        超时或无人应答返回 None（Intervention 会落到 DS_ON_STUCK 策略）。
        """
        wait = float(os.environ.get("DS_WEB_WAIT", "600"))
        self._ask = question
        self._answered.clear()
        self._answer = None
        self.emit(json.dumps(question, ensure_ascii=False), "ask")
        got = self._answered.wait(wait)
        self._ask = None
        if not got:
            self.emit("（等待决策超时，按 DS_ON_STUCK 处理）", "warn")
            return None
        return self._answer

    def answer(self, decision, data=None):
        """浏览器回答案。"""
        self._answer = (decision, data)
        self._answered.set()

    def pending(self):
        return self._ask

    # ---------- 执行 ----------
    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        # 延迟导入：让 --serve 在没有 openai 时也能启动并给出提示
        try:
            import ds_agent_pro as A
        except Exception as e:
            self.log(f"[启动失败] {type(e).__name__}: {e}", "err")
            self.done.set()
            return

        tee = Tee(lambda line: self.emit(line, "log"))
        try:
            A.ensure_builtin_skills()
            A.ensure_mcp_cfg()
            hub = A.MCPHub(A.MCP_CFG)
            mcp_tools = hub.start()
            tools = A.BASE_TOOLS + mcp_tools
            snap = A.Snapshot(A.ROOT, A.SNAPSHOT_ON)
            snap.commit("任务开始")
            sandbox = A.Sandbox(A.ROOT, A.SANDBOX, A.build_mounts())
            self.log(sandbox.note, "sys")
            dispatch = A.make_dispatch(sandbox, snap, hub)

            # 让主循环把介入请求转给浏览器
            A.WEB_ASKER = self.asker
            A.WEB_APPROVER = self._approver

            with contextlib.redirect_stdout(tee), \
                 contextlib.redirect_stderr(tee):
                A.run(self.task, sandbox, snap, hub, tools, dispatch)
            hub.close()
            sandbox.close()
        except Exception as e:
            import traceback
            self.log(f"[异常] {type(e).__name__}: {e}", "err")
            self.log(traceback.format_exc(), "err")
        finally:
            tee.flush()
            self.done.set()

    def _approver(self, cmd, why):
        """本地模式下写操作的审批回调（手机端确认）。"""
        return self.asker({
            "task": "命令审批",
            "reason": why,
            "summary": f"```\n{cmd}\n```",
            "report": "",
            "report_path": "",
            "round": 0,
            "choices": [
                {"key": "1", "value": "allow", "label": "允许执行",
                 "desc": "这条命令我确认安全"},
                {"key": "2", "value": "deny", "label": "拒绝",
                 "desc": "不执行，让模型换条路"},
            ],
            "kind": "approve",
            "cmd": cmd,
        }) and True


# ============================ HTTP ============================
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ds-agent</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#0f1115;color:#e6e6e6;font:14px/1.6 -apple-system,
 "PingFang SC","Microsoft YaHei",sans-serif;padding-bottom:env(safe-area-inset-bottom)}
#top{position:sticky;top:0;background:#0f1115;border-bottom:1px solid #23262d;
 padding:10px 12px;z-index:10}
#bar{display:flex;gap:8px}
#task{flex:1;background:#1a1d24;border:1px solid #2b2f38;color:#e6e6e6;
 border-radius:10px;padding:10px 12px;font-size:16px;outline:none}
#go{background:#3b6fd4;border:0;color:#fff;border-radius:10px;padding:0 18px;
 font-size:15px;font-weight:600}
#go:disabled{background:#2b2f38;color:#7a7f8a}
#out{padding:10px 12px;white-space:pre-wrap;word-break:break-all;
 font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#c8ccd4}
.l-err{color:#ff7b72}.l-warn{color:#e3b341}.l-sys{color:#79c0ff}
.l-ok{color:#7ee787}.l-ask{color:#ffa657}
#ask{display:none;position:fixed;left:0;right:0;bottom:0;background:#161a21;
 border-top:1px solid #2b2f38;padding:14px 12px 20px;max-height:70vh;overflow:auto}
#ask.on{display:block}
#ask h3{margin:0 0 6px;font-size:15px;color:#ffa657}
#ask .why{color:#9aa0aa;font-size:13px;margin-bottom:10px}
#ask pre{background:#0f1115;border:1px solid #23262d;border-radius:8px;
 padding:10px;overflow:auto;max-height:34vh;font-size:12px;color:#c8ccd4}
.ch{display:block;width:100%;text-align:left;background:#1f2530;
 border:1px solid #2b2f38;color:#e6e6e6;border-radius:10px;padding:11px 12px;
 margin-bottom:8px;font-size:15px}
.ch:active{background:#2b3340}
.ch b{color:#fff}.ch span{color:#9aa0aa;font-size:12.5px}
#hint{display:none;width:100%;background:#1a1d24;border:1px solid #2b2f38;
 color:#e6e6e6;border-radius:10px;padding:10px 12px;margin-bottom:8px;font-size:16px}
#dot{display:inline-block;width:8px;height:8px;border-radius:50%;
 background:#3fb950;margin-right:6px}
#dot.run{background:#e3b341}
</style>
</head>
<body>
<div id="top">
  <div id="bar">
    <input id="task" placeholder="要做什么？例如：写个快排并验证" autocomplete="off">
    <button id="go">运行</button>
  </div>
  <div style="margin-top:6px;font-size:12px;color:#7a7f8a">
    <span id="dot"></span><span id="st">空闲</span>
  </div>
</div>
<div id="out"></div>
<div id="ask">
  <h3 id="askh">需要你决策</h3>
  <div class="why" id="askw"></div>
  <pre id="askp"></pre>
  <div id="askc"></div>
  <input id="hint" placeholder="输入提示">
</div>
<script>
const out=document.getElementById('out'),st=document.getElementById('st'),
 dot=document.getElementById('dot'),go=document.getElementById('go'),
 task=document.getElementById('task'),ask=document.getElementById('ask'),
 askh=document.getElementById('askh'),askw=document.getElementById('askw'),
 askp=document.getElementById('askp'),askc=document.getElementById('askc'),
 hint=document.getElementById('hint');
let es=null,cur=null;
function line(t,k){const d=document.createElement('div');d.className='l-'+k;
 d.textContent=t;out.appendChild(d);window.scrollTo(0,document.body.scrollHeight);
 while(out.childElementCount>3000)out.removeChild(out.firstChild);}
function setRun(r){dot.className=r?'run':'';st.textContent=r?'运行中…':'空闲';
 go.disabled=r;go.textContent=r?'运行中':'运行';}
function showAsk(q){
 cur=q;askh.textContent=q.kind==='approve'?'命令审批':'卡住了，需要你决策';
 askw.textContent=q.reason||'';askp.textContent=q.summary||'';
 askc.innerHTML='';hint.style.display='none';
 q.choices.forEach(c=>{
  const b=document.createElement('button');b.className='ch';
  b.innerHTML='<b>'+c.key+') '+c.label+'</b> — <span>'+(c.desc||'')+'</span>';
  b.onclick=()=>{
   if(c.needs_text){hint.style.display='block';hint.focus();
     b.textContent='提交';b.onclick=()=>send(c.value,hint.value);return;}
   if(c.needs_steps){const n=prompt('回退几步？',1);send(c.value,n?parseInt(n):1);return;}
   send(c.value,null);};
  askc.appendChild(b);});
 ask.classList.add('on');}
function send(v,d){ask.classList.remove('on');
 fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json',
  'X-Token':TOKEN},body:JSON.stringify({value:v,data:d})});}
function openSSE(t){
 if(es)es.close();setRun(true);
 es=new EventSource('/api/events?token='+encodeURIComponent(TOKEN)+'&t='+Date.now());
 es.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.k==='ask'){try{showAsk(JSON.parse(m.t));}catch(_){}}
  else if(m.k==='end'){setRun(false);es.close();es=null;}
  else line(m.t,m.k);};
 es.onerror=()=>{setRun(false);if(es){es.close();es=null;}};}
go.onclick=()=>{const t=task.value.trim();if(!t)return;
 out.innerHTML='';
 fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json',
  'X-Token':TOKEN},body:JSON.stringify({task:t})}).then(r=>r.json())
  .then(j=>{if(j.error){line(j.error,'err');return;}openSSE(t);});};
task.addEventListener('keydown',e=>{if(e.key==='Enter')go.click();});
hint.addEventListener('keydown',e=>{if(e.key==='Enter'){
 const b=askc.lastElementChild;if(b)b.click();}});
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ds-agent-webui"

    def log_message(self, *a):
        pass

    # ---------- helpers ----------
    def _send(self, body, ctype="text/html; charset=utf-8", status=200,
              extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", status)

    def _authorized(self):
        token = self.server.token
        if not token:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        got = (self.headers.get("X-Token") or ""
               or (q.get("token") or [""])[0])
        # 常量时间比较，别用 ==
        import hmac
        return hmac.compare_digest(got, token)

    def _denied(self):
        self._json({"error": "未授权"}, 401)

    # ---------- routes ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return self._denied()
        if path in ("/", "/index.html"):
            page = PAGE.replace("TOKEN", json.dumps(self.server.token or ""))
            return self._send(page)
        if path == "/api/events":
            return self._events()
        if path == "/api/status":
            s = self.server.session
            return self._json({
                "running": bool(s and not s.done.is_set()),
                "pending": bool(s and s.pending()),
            })
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return self._denied()
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}

        if path == "/api/start":
            return self._start(body)
        if path == "/api/decide":
            s = self.server.session
            if not s:
                return self._json({"error": "无进行中的会话"}, 400)
            s.answer(body.get("value"), body.get("data"))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    # ---------- 实现 ----------
    def _start(self, body):
        srv = self.server
        # 先校验参数，再判断忙不忙 —— 否则空任务会被误报成"已有任务在跑"
        task = (body.get("task") or "").strip()
        if not task:
            return self._json({"error": "任务为空"}, 400)
        if srv.session and not srv.session.done.is_set():
            return self._json({"error": "已有任务在跑"}, 409)
        if not os.environ.get("DEEPSEEK_API_KEY"):
            return self._json({"error": "服务器未设置 DEEPSEEK_API_KEY"}, 500)

        s = AgentSession(task, None)
        srv.session = s
        s.start()
        return self._json({"ok": True, "task": task})

    def _events(self):
        """SSE：把会话队列里的事件推给浏览器。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        srv = self.server
        s = srv.session
        if s is None:
            # 没任务时也要给个 end，否则浏览器一直转圈
            self._sse({"t": "（当前没有任务）", "k": "sys"})
            self._sse({"t": "", "k": "end"})
            return

        deadline = time.time() + float(os.environ.get("DS_WEB_MAX", "3600"))
        try:
            while time.time() < deadline:
                try:
                    ev = s.events.get(timeout=0.5)
                except queue.Empty:
                    if s.done.is_set():
                        break
                    continue
                if not self._sse(ev):
                    return
            self._sse({"t": "", "k": "end"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, ev):
        try:
            data = json.dumps(ev, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, token=""):
        super().__init__(addr, handler)
        self.token = token
        self.session = None


def serve(port=8080, host=None, token=None, open_browser=False):
    host = host or os.environ.get("DS_WEB_HOST", "127.0.0.1")
    token = token if token is not None else os.environ.get("DS_WEB_TOKEN", "")

    if not token:
        print("❌ 拒绝启动：未设置 DS_WEB_TOKEN。\n"
              "   这个服务能执行代码，必须设访问口令：\n"
              "   export DS_WEB_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')\n"
              "   仅本机访问且确认无风险时，可设 DS_WEB_ALLOW_NO_TOKEN=1 跳过。",
              file=sys.stderr)
        if os.environ.get("DS_WEB_ALLOW_NO_TOKEN") != "1":
            sys.exit(2)

    srv = ThreadedHTTPServer((host, port), Handler, token)
    lan = host in ("0.0.0.0", "::")
    print(f"\n  ds-agent Web UI\n"
          f"  本机:  http://127.0.0.1:{port}\n"
          f"  监听:  {host}:{port}" +
          (f"\n  口令:  {token}" if token else "\n  ⚠️  未设口令，任何人都能执行代码") +
          (f"\n  ⚠️  监听在 {host}，同网段任何人可访问" if lan else "\n  （仅本机）"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
