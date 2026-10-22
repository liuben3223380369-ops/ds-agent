#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP stdio 客户端 —— JSON-RPC 2.0 over subprocess

协议要点（与 dsmod 那套 Mcp.smali 同源，都是 JSON-RPC 2.0）：
    initialize -> notifications/initialized -> tools/list -> tools/call
传输层：newline-delimited JSON，走子进程 stdin/stdout。
只依赖标准库。
"""

import json
import os
import subprocess
import threading
import time


class MCPClient:
    def __init__(self, name, command, args=None, env=None, cwd=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.proc = None
        self.tools = []          # [{"name","description","inputSchema"}]
        self.server_info = {}
        self._id = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._responses = {}

    # ---------- 生命周期 ----------
    def start(self):
        env = os.environ.copy()
        env.update(self.env)
        self.proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=self.cwd, text=True, bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    # ---------- 传输层 ----------
    def _reader(self):
        """后台线程：读 stdout，把带 id 的响应存起来，通知主流程。"""
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._cond:
                        self._responses[msg["id"]] = msg
                        self._cond.notify_all()
        except Exception:
            pass

    def request(self, method, params=None, timeout=30):
        with self._lock:
            self._id += 1
            rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

        deadline = time.time() + timeout
        with self._cond:
            while rid not in self._responses:
                if time.time() > deadline:
                    raise TimeoutError(f"{self.name}: {method} 超时 {timeout}s")
                self._cond.wait(0.2)
            msg = self._responses.pop(rid)
        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(f"{self.name} RPC错误 {err.get('code')}: {err.get('message')}")
        return msg.get("result", {})

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    # ---------- 协议握手 ----------
    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ds-agent", "version": "1.0"},
        }, timeout=30)
        self.server_info = result.get("serverInfo", {})
        self.notify("notifications/initialized", {})
        return result

    def list_tools(self):
        result = self.request("tools/list", {}, timeout=30)
        self.tools = result.get("tools", [])
        return self.tools

    def call_tool(self, name, arguments, timeout=60):
        result = self.request("tools/call",
                              {"name": name, "arguments": arguments or {}},
                              timeout=timeout)
        # MCP 标准返回：{"content":[{"type":"text","text":"..."}],"isError":bool}
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        text = "\n".join(parts) or json.dumps(result, ensure_ascii=False)
        if result.get("isError"):
            text = f"[MCP错误] {text}"
        return text

    # ---------- 转成 OpenAI tools 格式 ----------
    def to_openai_tools(self, prefix=None):
        """MCP 工具 → OpenAI function schema。名字加前缀避免与内置工具冲突。"""
        prefix = prefix or f"mcp_{_safe(self.name)}"
        out = []
        for t in self.tools:
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            if not schema.get("type"):
                schema["type"] = "object"
            out.append({
                "type": "function",
                "function": {
                    "name": f"{prefix}__{_safe(t['name'])}",
                    "description": (t.get("description") or t["name"])[:1024],
                    "parameters": schema,
                },
            })
        return out


def _safe(s: str) -> str:
    """OpenAI 函数名只允许 [a-zA-Z0-9_-]"""
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in s)[:60]


def load_servers(cfg_path: str):
    """读 mcp.json（Claude Desktop 同格式），返回 [(name, command, args, env)]"""
    import os
    if not os.path.exists(cfg_path):
        return []
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    out = []
    for name, spec in (cfg.get("mcpServers") or {}).items():
        if spec.get("disabled"):
            continue
        out.append((name, spec.get("command"), spec.get("args", []), spec.get("env", {})))
    return out
