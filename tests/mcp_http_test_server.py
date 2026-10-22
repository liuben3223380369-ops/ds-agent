#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP HTTP 传输的本地测试服务端（仅用于验证，不是产品代码）。

同时提供两种端点：
  · POST /mcp   —— Streamable HTTP（返回 SSE 或 JSON）
  · GET  /sse   —— 旧式 SSE，配合 POST /messages
"""

import json
import time
import threading
import http.server
import socketserver

TOOLS = [
    {"name": "remote_echo", "description": "回显输入（远端工具）",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "remote_time", "description": "返回远端服务器时间",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
]

SESSIONS = {}          # sid -> {"queue": [...], "event": threading.Event}
LOCK = threading.Lock()


def handle_rpc(msg):
    m, mid = msg.get("method"), msg.get("id")
    p = msg.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "remote-test-server", "version": "1.0"}}
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        name = p.get("name")
        args = p.get("arguments") or {}
        if name == "remote_echo":
            out = f"remote echo: {args.get('text','')}"
        elif name == "remote_time":
            out = time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            return {"content": [{"type": "text", "text": f"未知工具: {name}"}],
                    "isError": True}
        return {"content": [{"type": "text", "text": out}], "isError": False}
    return {}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------- helpers ----------
    def _json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _sse_send(self, data, event=None):
        if event:
            self.wfile.write(f"event: {event}\n".encode())
        for line in str(data).split("\n"):
            self.wfile.write(f"data: {line}\n".encode())
        self.wfile.write(b"\n")
        self.wfile.flush()

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return None
        return json.loads(self.rfile.read(n).decode())

    # ---------- Streamable HTTP ----------
    def do_POST(self):
        msg = self._read_body()
        if msg is None:
            self._json({}, 400)
            return

        path = self.path.split("?")[0]
        is_stream = "text/event-stream" in (self.headers.get("Accept") or "")

        if path == "/messages" or path == "/message":     # SSE 模式的 POST
            sid = self.headers.get("Mcp-Session-Id") or "default"
            with LOCK:
                st = SESSIONS.setdefault(sid, {"queue": [], "event": threading.Event()})
                st["queue"].append(self._reply(handle_rpc(msg), msg))
                st["event"].set()
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        res = handle_rpc(msg)
        reply = self._reply(res, msg)

        if is_stream:
            self._sse_start()
            self._sse_send(json.dumps(reply), event="message")
            self.wfile.write(b": done\n\n")
            self.wfile.flush()
        else:
            self._json(reply, extra={"Mcp-Session-Id": "test-session"})

    @staticmethod
    def _reply(res, msg):
        out = {"jsonrpc": "2.0", "id": msg.get("id")}
        if res:
            out["result"] = res
        return out

    # ---------- 旧式 SSE ----------
    def do_GET(self):
        if not self.path.startswith("/sse"):
            self._json({"error": "not found"}, 404)
            return
        sid = "default"
        with LOCK:
            st = SESSIONS.setdefault(sid, {"queue": [], "event": threading.Event()})
        self._sse_start()
        self._sse_send("/messages", event="endpoint")
        while True:
            st["event"].wait(0.5)
            if st["event"].is_set():
                with LOCK:
                    pending, st["queue"] = st["queue"], []
                st["event"].clear()
                for item in pending:
                    try:
                        self._sse_send(json.dumps(item), event="message")
                    except Exception:
                        return


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadedHTTPServer(("127.0.0.1", port), Handler)
    print(f"[test-server] http://127.0.0.1:{port}  (/mcp 流able, /sse 旧式)", flush=True)
    srv.serve_forever()
