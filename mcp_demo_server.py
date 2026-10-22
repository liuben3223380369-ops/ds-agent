#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小可用 MCP Server（stdio 传输，纯标准库）

用途有二：
  1. 验证 ds_agent_pro.py 的 MCP 通道确实握手成功、能列工具、能调工具
  2. 作为你自己写 MCP Server 的模板 —— 加工具只要往 TOOLS 里加一项

直接运行：python3 mcp_demo_server.py   （它会安静地等 stdin 上的 JSON-RPC）
"""

import json
import os
import sys
import time
import platform
from datetime import datetime

# ---------- 工具定义 ----------
TOOLS = [
    {
        "name": "get_time",
        "description": "返回当前系统时间，格式 YYYY-mm-dd HH:MM:SS，带时区。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_dir",
        "description": "列出指定目录下的文件与子目录（不递归）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认当前目录"},
            },
            "required": [],
        },
    },
    {
        "name": "sys_info",
        "description": "返回运行环境信息：操作系统、Python 版本、CPU 核数。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def tool_get_time(args):
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f" (tz={time.tzname[0]})"


def tool_list_dir(args):
    p = args.get("path") or "."
    if not os.path.isdir(p):
        return f"不是目录或不存在: {p}"
    items = []
    for n in sorted(os.listdir(p))[:200]:
        full = os.path.join(p, n)
        tag = "d" if os.path.isdir(full) else "f"
        size = os.path.getsize(full) if os.path.isfile(full) else ""
        items.append(f"{tag} {n} {size}")
    return "\n".join(items) if items else "(空目录)"


def tool_sys_info(args):
    return (f"OS: {platform.system()} {platform.release()}\n"
            f"Python: {platform.python_version()}\n"
            f"CPU: {os.cpu_count()} 核")


HANDLERS = {"get_time": tool_get_time, "list_dir": tool_list_dir, "sys_info": tool_sys_info}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    # 通知类消息不需要回包
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo-server", "version": "1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}
        try:
            return {"content": [{"type": "text", "text": str(fn(args))}], "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    if method == "ping":
        return {}
    if mid is not None:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            result = handle(msg)
        except Exception as e:
            result = {"error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}}
        if result is None:
            continue
        out = {"jsonrpc": "2.0", "id": msg.get("id")}
        if "error" in result:
            out["error"] = result["error"]
        else:
            out["result"] = result
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
