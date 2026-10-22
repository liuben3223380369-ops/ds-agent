#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filesystem MCP Server（stdio 传输，纯标准库）

把「沙箱外面的真实文件夹」安全地暴露给模型。
核心机制是 ROOTS 白名单 —— 模型只能看到显式授权的目录，越界一律拒绝。

配置方式（二选一）：
    命令行：python3 mcp_fs_server.py --root /sdcard/Download --root /sdcard/Documents
    环境变量：MCP_FS_ROOTS="/sdcard/Download:/sdcard/Documents" MCP_FS_READONLY=1

手机上（Termux）常见根目录：
    /sdcard/Download          下载目录
    /sdcard/Documents         文档目录
    /sdcard/DCIM              相册
    /data/data/com.termux/files/home   Termux 家目录
首次使用需先在 Termux 里执行 termux-setup-storage 授权存储访问。
"""

import os
import sys
import json
import shutil
import fnmatch
import time
from pathlib import Path

# ---------------- 配置解析 ----------------
def parse_roots():
    roots = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            roots.append(argv[i + 1]); i += 2
        elif argv[i].startswith("--root="):
            roots.append(argv[i].split("=", 1)[1]); i += 1
        elif argv[i] == "--readonly":
            os.environ["MCP_FS_READONLY"] = "1"; i += 1
        else:
            i += 1
    env = os.environ.get("MCP_FS_ROOTS", "")
    if env:
        sep = ";" if os.name == "nt" else ":"
        roots += [x for x in env.split(sep) if x]
    return [Path(r).expanduser().resolve() for r in roots]

ROOTS = parse_roots()
READONLY = os.environ.get("MCP_FS_READONLY", "0") in ("1", "true", "yes")
MAX_FILE = int(os.environ.get("MCP_FS_MAXFILE", str(2 * 1024 * 1024)))  # 单次读取上限 2MB

# 二进制扩展名，读这些文件只会得到乱码
BIN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf",
           ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".apk",
           ".so", ".dex", ".o", ".a", ".exe", ".dll", ".mp3", ".mp4",
           ".mkv", ".avi", ".mov", ".flac", ".wav", ".ttf", ".otf", ".db",
           ".sqlite", ".parquet", ".pyc", ".class", ".jar"}


def ok(p: str) -> Path:
    """白名单检查：解析后的绝对路径必须落在某个 ROOT 之内。"""
    fp = Path(p).expanduser().resolve()
    for r in ROOTS:
        if str(fp) == str(r) or str(fp).startswith(str(r) + os.sep):
            return fp
    raise PermissionError(
        f"拒绝访问：{p} 不在授权目录内。允许的根目录：{[str(r) for r in ROOTS] or '（未配置）'}")


def check_write():
    if READONLY:
        raise PermissionError("当前为只读模式（MCP_FS_READONLY=1），禁止写操作")


def human(n):
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


# ---------------- 工具实现 ----------------
def fs_roots(a):
    if not ROOTS:
        return "（未配置任何根目录）"
    return "\n".join(f"{'📁' if r.is_dir() else '❌不存在'} {r}  可写={not READONLY}" for r in ROOTS)


def fs_list(a):
    d = ok(a.get("path") or str(ROOTS[0]))
    if not d.is_dir():
        return f"不是目录: {d}"
    recursive = bool(a.get("recursive"))
    limit = int(a.get("limit", 300))
    out, count = [], 0
    walker = d.rglob("*") if recursive else d.glob("*")
    for p in sorted(walker):
        if count >= limit:
            out.append(f"...(已达 {limit} 条上限，请缩小范围)")
            break
        rel = p.relative_to(d)
        if p.is_dir():
            out.append(f"d {rel}/")
        else:
            try:
                out.append(f"f {rel}  {human(p.stat().st_size)}")
            except OSError:
                out.append(f"f {rel}  ?")
        count += 1
    return "\n".join(out) or "(空目录)"


def fs_read(a):
    p = ok(a["path"])
    if not p.is_file():
        return f"不是文件或不存在: {p}"
    if p.suffix.lower() in BIN_EXT:
        return f"[跳过] 疑似二进制文件（{p.suffix}），读取无意义"
    size = p.stat().st_size
    if size > MAX_FILE:
        return f"[过大] {human(size)} > 上限 {human(MAX_FILE)}，请用 offset/limit 分段读取"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[读取失败] {type(e).__name__}: {e}"
    lines = text.splitlines()
    off = int(a.get("offset", 0))
    lim = int(a.get("limit", 0))
    seg = lines[off:(off + lim) if lim > 0 else None]
    head = f"[共 {len(lines)} 行 / {human(size)}]\n"
    return head + "\n".join(f"{off+i}\t{l}" for i, l in enumerate(seg))


def fs_write(a):
    check_write()
    p = ok(a["path"])
    content = a.get("content", "")
    mode = a.get("mode", "overwrite")
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append" and p.exists():
        old = p.read_text(encoding="utf-8", errors="replace")
        if not old.endswith("\n"):
            content = "\n" + content
        p.write_text(old + content, encoding="utf-8")
    else:
        p.write_text(content, encoding="utf-8")
    return f"已写入 {p}（{len(content)} 字符）"


def fs_mkdir(a):
    check_write()
    p = ok(a["path"])
    p.mkdir(parents=True, exist_ok=True)
    return f"目录已就绪: {p}"


def fs_delete(a):
    check_write()
    p = ok(a["path"])
    if not p.exists():
        return f"不存在: {p}"
    if p.is_dir():
        if any(p.iterdir()):
            return f"[拒绝] 目录非空: {p}（防误删，请先清空或明确指定文件）"
        p.rmdir()
    else:
        p.unlink()
    return f"已删除 {p}"


def fs_move(a):
    check_write()
    src, dst = ok(a["src"]), ok(a["dst"])
    if not src.exists():
        return f"源不存在: {src}"
    shutil.move(str(src), str(dst))
    return f"已移动 {src} → {dst}"


def fs_copy(a):
    check_write()
    src, dst = ok(a["src"]), ok(a["dst"])
    if not src.exists():
        return f"源不存在: {src}"
    shutil.copy2(str(src), str(dst))
    return f"已复制 {src} → {dst}"


def fs_stat(a):
    p = ok(a["path"])
    if not p.exists():
        return f"不存在: {p}"
    s = p.stat()
    return (f"路径: {p}\n类型: {'目录' if p.is_dir() else '文件'}\n"
            f"大小: {human(s.st_size)}\n"
            f"修改: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.st_mtime))}\n"
            f"权限: {oct(s.st_mode)[-3:]}")


def fs_search(a):
    """按文件名 glob 或内容关键字搜索。"""
    root = ok(a.get("path") or str(ROOTS[0]))
    pattern = a.get("pattern", "*")
    content = a.get("content")
    limit = int(a.get("limit", 100))
    hits = []
    files = root.rglob(pattern) if pattern != "*" else root.rglob("*")
    for p in files:
        if len(hits) >= limit:
            hits.append(f"...(已达 {limit} 条上限)"); break
        if not p.is_file() or p.suffix.lower() in BIN_EXT:
            continue
        if content:
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines()):
                    if content in line:
                        hits.append(f"{p.relative_to(root)}:{i+1}: {line.strip()[:160]}")
                        break
            except Exception:
                continue
        else:
            hits.append(str(p.relative_to(root)))
    return "\n".join(hits) or "(无匹配)"


HANDLERS = {
    "fs_roots": fs_roots, "fs_list": fs_list, "fs_read": fs_read,
    "fs_write": fs_write, "fs_mkdir": fs_mkdir, "fs_delete": fs_delete,
    "fs_move": fs_move, "fs_copy": fs_copy, "fs_stat": fs_stat,
    "fs_search": fs_search,
}

TOOLS = [
    {"name": "fs_roots", "description": "列出当前授权的根目录清单。不确定能访问哪里时先调这个。",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "fs_list", "description": "列出目录内容。默认不递归，显示 d/f 标记与文件大小。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "目录绝对路径"},
         "recursive": {"type": "boolean", "description": "是否递归子目录"},
         "limit": {"type": "integer", "description": "最多返回条数，默认300"}},
         "required": ["path"]}},
    {"name": "fs_read", "description": "读取文本文件内容（自动跳过二进制）。支持按行分页。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "文件绝对路径"},
         "offset": {"type": "integer", "description": "起始行号，从0开始"},
         "limit": {"type": "integer", "description": "最多读多少行"}},
         "required": ["path"]}},
    {"name": "fs_write", "description": "写入文本文件。可覆盖或追加。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "文件绝对路径"},
         "content": {"type": "string", "description": "文件内容"},
         "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "默认overwrite"}},
         "required": ["path", "content"]}},
    {"name": "fs_mkdir", "description": "创建目录（含父目录）。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "目录绝对路径"}}, "required": ["path"]}},
    {"name": "fs_delete", "description": "删除文件或空目录。非空目录会被拒绝以防误删。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "绝对路径"}}, "required": ["path"]}},
    {"name": "fs_move", "description": "移动或重命名。",
     "inputSchema": {"type": "object", "properties": {
         "src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]}},
    {"name": "fs_copy", "description": "复制文件或目录。",
     "inputSchema": {"type": "object", "properties": {
         "src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]}},
    {"name": "fs_stat", "description": "查看文件元信息：类型、大小、修改时间、权限。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "fs_search", "description": "搜索文件：按文件名 glob 匹配，或按内容关键字定位（返回 路径:行号:内容）。",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "搜索起始目录"},
         "pattern": {"type": "string", "description": "文件名 glob，如 *.py，默认 *"},
         "content": {"type": "string", "description": "内容关键字，给了就按内容搜"},
         "limit": {"type": "integer", "description": "最多返回条数，默认100"}},
         "required": ["path"]}},
]

# 只读模式下把写工具整个摘掉，模型根本看不到
if READONLY:
    TOOLS = [t for t in TOOLS if t["name"] not in
             ("fs_write", "fs_mkdir", "fs_delete", "fs_move", "fs_copy", "fs_copy")]


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "filesystem-server",
                               "version": "1.0",
                               "roots": [str(r) for r in ROOTS],
                               "readonly": READONLY}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return {"content": [{"type": "text", "text": f"未知工具: {name}"}], "isError": True}
        try:
            return {"content": [{"type": "text", "text": str(fn(params.get("arguments") or {}))}],
                    "isError": False}
        except Exception as e:
            # PermissionError 是最常见的越界拒绝，单独提示得更清楚
            return {"content": [{"type": "text", "text": f"[{type(e).__name__}] {e}"}], "isError": True}
    if mid is not None:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


def main():
    if not ROOTS:
        sys.stderr.write("[fs-server] 警告：未配置任何根目录，所有访问都会被拒绝。\n")
        sys.stderr.write("[fs-server] 用法: --root /path 或 MCP_FS_ROOTS=/path\n")
    sys.stderr.write(f"[fs-server] 授权根目录: {[str(r) for r in ROOTS]} 只读={READONLY}\n")
    sys.stderr.flush()
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
