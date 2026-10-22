#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP HTTP 传输 —— 补齐 stdio 之外的第二个通道。

对应 dsmod 的 Mcp.smali（2370 行，httpRequest L614 / httpPost L425 / stdioRequest L2007）。
它同时实现了 HTTP 和 stdio 两路，我们此前只有 stdio，接不了远端服务。

支持两种 HTTP 传输：

1. **Streamable HTTP**（协议版本 2025-03-26，新标准）
   单个端点 POST /mcp，响应可能是 JSON，也可能是 `text/event-stream`。
   服务端可能返回 `Mcp-Session-Id`，后续请求必须带上。

2. **HTTP + SSE**（协议版本 2024-11-05，旧标准）
   GET /sse 建立事件流，服务端在 `event: endpoint` 里告知 POST 地址，
   之后 POST 发请求，结果从 SSE 流里异步回来。

纯标准库实现（urllib + threading），不引入 requests / httpx，
保持和 mcp_client.py 一致的零依赖风格。
"""

import os
import json
import time
import uuid
import queue
import threading
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse

UA = "ds-agent-mcp-http/1.0"


class StreamableHTTPClient:
    """
    Streamable HTTP（2025-03-26）。

    一次 POST 可能返回：
      · application/json        → 直接是 JSON-RPC 响应
      · text/event-stream       → SSE 流，需要从中取消息
      · 202 Accepted            → 纯通知，无响应体
    """

    def __init__(self, url, headers=None, timeout=30, env=None):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.session_id = None
        self.protocol_version = "2025-03-26"

    def _build_headers(self, stream=False):
        h = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream" if stream
                      else "application/json, text/event-stream",
        }
        h.update(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _read_sse(resp, deadline):
        """
        SSE 响应没有 Content-Length，直接 read() 会一直等到 EOF（超时）。
        这里增量读，拿到第一个完整事件（空行分隔）就收工。
        """
        buf = b""
        read1 = getattr(resp, "read1", None) or resp.read
        while time.time() < deadline:
            try:
                chunk = read1(4096)
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            buf += chunk
            if b"\n\n" in buf:                 # 一个完整事件已到达
                break
        return buf

    def _post(self, payload, stream=False):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST", headers=self._build_headers(stream))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    raw = self._read_sse(resp, time.time() + self.timeout)
                else:
                    raw = resp.read()
                return resp.status, ctype, raw
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            return e.code, (e.headers.get("Content-Type") or "").lower() if e.headers else "", raw

    def request(self, method, params=None, timeout=None):
        """发一个 JSON-RPC 请求，返回 result（或抛 RuntimeError）。"""
        msg = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            msg["params"] = params

        old = self.timeout
        if timeout:
            self.timeout = timeout
        try:
            status, ctype, raw = self._post(msg)
        finally:
            self.timeout = old

        if status == 202 or not raw:
            return {}                                  # 通知已接受，无响应体

        if "text/event-stream" in ctype:
            return _first_result_from_sse(raw.decode("utf-8", "replace"))

        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise RuntimeError(f"无法解析响应（HTTP {status}）: {raw[:200]!r}")

        if "error" in data:
            e = data["error"]
            raise RuntimeError(f"[{e.get('code')}] {e.get('message')}")
        return data.get("result", {})

    def notify(self, method, params=None):
        """发通知（无 id，不期待响应）。"""
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._post(msg)
        except Exception:
            pass

    def close(self):
        """Streamable HTTP 是无状态的短连接，无需显式关闭。"""
        self.session_id = None


class SSEClient:
    """
    HTTP + SSE 传输（2024-11-05）。

    GET /sse 长连接收事件；服务端先发 `event: endpoint` 告知 POST 地址；
    之后 POST 发 JSON-RPC，响应从 SSE 流里按 id 匹配回来。
    """

    def __init__(self, url, headers=None, timeout=30, connect_timeout=15):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.protocol_version = "2024-11-05"
        self.endpoint = None
        self._ready = threading.Event()
        self._inbox = {}                 # id -> Queue
        self._thread = None
        self._stop = threading.Event()
        self._error = None

    # ---------- SSE 解析 ----------
    @staticmethod
    def parse_sse(text):
        """
        解析一段 SSE 文本，产出 (event, data) 序列。
        严格遵守规范：字段间以 \\n 分隔，消息间以空行分隔，
        data 多行用 \\n 拼接（不是 \\r\\n）。
        """
        out = []
        event, data = None, []
        # 统一换行，避免 \\r\\n 干扰
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line == "":
                if data or event:
                    out.append((event or "message", "\n".join(data)))
                event, data = None, []
                continue
            if line.startswith(":"):          # 注释/心跳
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                if v.startswith(" "):
                    v = v[1:]
            else:
                k, v = line, ""
            if k == "event":
                event = v
            elif k == "data":
                data.append(v)
            elif k == "id":
                pass                            # 暂不使用 Last-Event-ID
        if data or event:
            out.append((event or "message", "\n".join(data)))
        return out

    # ---------- 连接 ----------
    def start(self):
        h = {"User-Agent": UA, "Accept": "text/event-stream", "Cache-Control": "no-cache"}
        h.update(self.headers)
        req = urllib.request.Request(self.url, method="GET", headers=h)
        try:
            self._resp = urllib.request.urlopen(req, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError(f"SSE 连接失败: {type(e).__name__}: {e}")

        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        if not self._ready.wait(self.connect_timeout):
            raise RuntimeError(
                f"SSE 已连接但 {self.connect_timeout}s 内未收到 endpoint 事件"
                + (f"（{self._error}）" if self._error else ""))
        return self

    def _pump(self):
        """后台线程读 SSE 流，把消息分发到对应 id 的队列。"""
        buf = ""
        try:
            while not self._stop.is_set():
                chunk = self._resp.read(1)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                # 只在遇到完整消息（空行）时才解析
                if buf.endswith("\n\n"):
                    for event, data in self.parse_sse(buf):
                        self._dispatch(event, data)
                    buf = ""
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
        finally:
            self._stop.set()

    def _dispatch(self, event, data):
        if event == "endpoint":
            self.endpoint = urljoin(self.url, data.strip())
            self._ready.set()
            return
        if event in ("message", ""):
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                return
            mid = msg.get("id")
            if mid is not None and mid in self._inbox:
                self._inbox[mid].put(msg)
            elif "method" in msg and "id" in msg:
                # 服务端主动调用我们（sampling 等）——当前不支持
                self._inbox.setdefault("__server__", queue.Queue()).put(msg)

    # ---------- 请求 ----------
    def _post(self, payload):
        if not self.endpoint:
            raise RuntimeError("尚未拿到 POST endpoint")
        body = json.dumps(payload).encode("utf-8")
        h = {"User-Agent": UA, "Content-Type": "application/json",
             "Accept": "application/json"}
        h.update(self.headers)
        req = urllib.request.Request(self.endpoint, data=body, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
        except urllib.error.HTTPError as e:
            e.read()
            # SSE 模式下服务端通常只回 202，响应走事件流，不视为错误
            if e.code not in (200, 201, 202, 204):
                raise RuntimeError(f"POST 失败 HTTP {e.code}")

    def request(self, method, params=None, timeout=None):
        mid = str(uuid.uuid4())
        q = queue.Queue()
        self._inbox[mid] = q
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._post(msg)
            try:
                reply = q.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise RuntimeError(f"等待 {method} 响应超时（{timeout or self.timeout}s）")
        finally:
            self._inbox.pop(mid, None)

        if "error" in reply:
            e = reply["error"]
            raise RuntimeError(f"[{e.get('code')}] {e.get('message')}")
        return reply.get("result", {})

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._post(msg)
        except Exception:
            pass

    def close(self):
        self._stop.set()
        # 先关底层 socket，pump 线程可能正阻塞在 read() 上
        try:
            sock = getattr(getattr(self._resp, "fp", None), "raw", None) or \
                   getattr(getattr(self._resp, "fp", None), "_sock", None)
            if sock is not None:
                sock.close()
        except Exception:
            pass
        try:
            self._resp.close()
        except Exception:
            pass


def _first_result_from_sse(text):
    """从 SSE 文本里取出第一条 JSON-RPC 响应。"""
    for _event, data in SSEClient.parse_sse(text):
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            continue
        if "error" in msg:
            e = msg["error"]
            raise RuntimeError(f"[{e.get('code')}] {e.get('message')}")
        if "result" in msg:
            return msg["result"]
    raise RuntimeError("SSE 流中没有找到 result")


def _expand_env(headers):
    """header 值支持 ${ENV_VAR} 展开，避免把密钥写进配置。"""
    out = {}
    for k, v in (headers or {}).items():
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            out[k] = os.environ.get(v[2:-1], "")
        else:
            out[k] = v
    return out


def build_http_client(spec, timeout=30):
    """
    按配置构造 HTTP 客户端。

    支持两种写法：
      {"type": "streamable-http", "url": "https://x/mcp", "headers": {...}}
      {"type": "sse", "url": "https://x/sse", "headers": {...}}
      {"url": "https://x/mcp"}   → 默认尝试 streamable，失败自动降级 SSE
    """
    url = spec.get("url") or ""
    if not url:
        raise ValueError("HTTP MCP server 缺少 url")
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的 URL 协议: {scheme}")

    headers = _expand_env(spec.get("headers"))
    kind = (spec.get("type") or spec.get("transport") or "auto").lower()

    if kind in ("sse", "http+sse", "legacy"):
        return SSEClient(url, headers=headers, timeout=timeout)
    if kind in ("streamable-http", "streamable", "http"):
        return StreamableHTTPClient(url, headers=headers, timeout=timeout)
    return AutoHTTPClient(url, headers=headers, timeout=timeout)


class AutoHTTPClient:
    """
    未显式指定 transport 时：先按 Streamable HTTP 试，
    连不上或协议不匹配再降级到 HTTP+SSE。
    """

    def __init__(self, url, headers=None, timeout=30):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.impl = None
        self.kind = None

    def _ensure(self):
        if self.impl:
            return
        s = StreamableHTTPClient(self.url, headers=self.headers, timeout=self.timeout)
        try:
            res = s.request("initialize", {
                "protocolVersion": s.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "ds-agent", "version": "1.0"},
            }, timeout=min(self.timeout, 15))
            self.impl, self.kind = s, "streamable-http"
            self._init_result = res
            return
        except Exception:
            pass
        # 降级：把 /mcp 换成 /sse（常见约定），再退 SSE
        sse_url = self.url
        for tail in ("/mcp", "/messages", "/rpc"):
            if sse_url.endswith(tail):
                sse_url = sse_url[: -len(tail)] + "/sse"
                break
        c = SSEClient(sse_url, headers=self.headers, timeout=self.timeout)
        c.start()
        self.impl, self.kind = c, "sse"
        self._init_result = c.request("initialize", {
            "protocolVersion": c.protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "ds-agent", "version": "1.0"},
        })

    def initialize(self):
        self._ensure()
        return getattr(self, "_init_result", {})

    def request(self, method, params=None, timeout=None):
        self._ensure()
        return self.impl.request(method, params, timeout=timeout)

    def notify(self, method, params=None):
        self._ensure()
        self.impl.notify(method, params)

    def close(self):
        if self.impl:
            self.impl.close()
