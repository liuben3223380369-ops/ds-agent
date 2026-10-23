#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Android 适配层 —— 让 ds-agent 能在 Chaquopy 嵌入的 CPython 里跑起来。

## 必须处理的环境差异

| 项 | 桌面 / 服务器 | Android (Chaquopy) |
|---|---|---|
| 工作目录 | 任意，默认 `./workspace` | **必须用 `context.filesDir`**，其余位置多半只读 |
| `__file__` | 真实磁盘路径 | APK 内的虚拟路径（asset），**不能当磁盘路径用** |
| 独立 Python 进程 | 有 | **没有**（libpython3.so 嵌进 App 进程） |
| Docker | 可有 | 没有，必然 local 模式 |
| sh | 有 | `/system/bin/sh` 通常可用 |
| rlimit | 可用 | 部分可用，Bionic 上 RLIMIT_AS 行为不同 |

## 三条铁律

1. **必须在 import ds_agent_pro 之前设置 `DS_WORKDIR`**
   —— 它在模块级就把 ROOT 固定了，之后再改环境变量没用。

2. **不能依赖 `__file__` 找数据文件**
   —— Chaquopy 下模块从 APK asset 加载，路径是虚拟的。
   skills / mcp.json 一律落到 filesDir，用绝对路径访问。

3. **Python 代码执行走进程内**
   —— 没有独立解释器可 fork-exec。死循环靠指令预算拦（见 inprocess.py）。
"""

import os
import sys
import json
from pathlib import Path

# 由 Kotlin 侧在启动 Python 前注入；为空则走兜底
FILES_DIR = None


def init(files_dir=None):
    """
    在任何 ds_agent_pro 相关 import 之前调用。

    files_dir: Android 的 context.filesDir（可写私有目录）
    返回实际使用的工作目录。
    """
    global FILES_DIR
    FILES_DIR = files_dir or os.environ.get("DS_FILES_DIR")
    if not FILES_DIR:
        # 兜底：非 Android 或没传，就用当前目录下的 workspace
        FILES_DIR = str(Path.cwd() / "workspace")

    root = Path(FILES_DIR)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # === 铁律 1：ROOT 在 import 时就固定，环境变量必须先设 ===
    os.environ["DS_WORKDIR"] = str(root / "workspace")
    os.environ.setdefault("DS_SANDBOX", "local")     # Android 上没有 Docker
    os.environ.setdefault("DS_SKILL_DIR", str(root / "skills"))
    os.environ.setdefault("DS_MCP_CFG", str(root / "mcp.json"))
    os.environ.setdefault("DS_NO_LIMIT", "0")

    # 流式输出在手机上不需要逐字刷屏，但保留能实时看到的能力
    os.environ.setdefault("DS_STREAM", "1")

    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)

    _seed_skills(root / "skills")
    _seed_mcp(root / "mcp.json")
    return os.environ["DS_WORKDIR"]


def _seed_skills(d: Path):
    """
    把内置 skill 落到可写目录。

    为什么不直接读仓库里的 skills/*.md：
    Chaquopy 下模块是从 APK asset 加载的，`__file__` 是虚拟路径，
    `Path(__file__).parent / "skills"` 在磁盘上不存在。
    *.md 也不是 Python 模块，不会被打包进 import 路径。

    所以直接用 skills.py 里的 BUILTIN 常量落盘 —— 它是纯字符串，
    不依赖任何文件系统查找。
    """
    try:
        from skills import BUILTIN
    except Exception:
        return []
    written = []
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    for name, content in BUILTIN.items():
        p = d / name
        if not p.exists():
            try:
                p.write_text(content, encoding="utf-8")
                written.append(name)
            except Exception:
                pass
    return written


def _seed_mcp(path: Path):
    """生成一份干净的 mcp.json（不启用任何 server）。"""
    if path.exists():
        return False
    try:
        path.write_text(json.dumps({"mcpServers": {}}, ensure_ascii=False,
                                   indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def report():
    """给 UI 用的一段环境说明。"""
    try:
        from inprocess import detect
        info = detect()
    except Exception:
        info = {}
    lines = [
        f"Python      {sys.version.split()[0]}",
        f"工作目录    {os.environ.get('DS_WORKDIR')}",
        f"独立解释器  {'可用' if info.get('python_subprocess') else '不可用 → 进程内执行'}",
        f"shell       {'可用' if info.get('sh_subprocess') else '不可用'}",
        f"沙箱        {os.environ.get('DS_SANDBOX')}（Android 无 Docker）",
        f"资源限制    {'可用' if info.get('has_setrlimit') else '不可用'}",
    ]
    return "\n".join(lines)


# ---------------- 给 Kotlin 调用的接口 ----------------
def run_task(task, on_event=None):
    """
    执行一次任务。on_event(text, kind) 用于把输出实时推给 UI。

    这个是 Android 上唯一的入口；Kotlin 侧通过 Chaquopy 调用它。
    """
    init()
    import ds_agent_pro as A

    if on_event is None:
        def on_event(text, kind="log"):
            print(text, flush=True)

    # 把主流程的 print 全部转成事件
    class _Tee:
        def __init__(self):
            self.buf = ""

        def write(self, s):
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                on_event(line, "log")
            return len(s)

        def flush(self):
            if self.buf:
                on_event(self.buf, "log")
                self.buf = ""

    A.ensure_builtin_skills()
    A.ensure_mcp_cfg()
    hub = A.MCPHub(A.MCP_CFG)
    mcp_tools = hub.start()
    tools = A.BASE_TOOLS + mcp_tools
    snap = A.Snapshot(A.ROOT, A.SNAPSHOT_ON)
    snap.commit("任务开始")
    sandbox = A.Sandbox(A.ROOT, A.SANDBOX, A.build_mounts())
    on_event(sandbox.note, "sys")
    dispatch = A.make_dispatch(sandbox, snap, hub)

    import contextlib
    tee = _Tee()
    try:
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            A.run(task, sandbox, snap, hub, tools, dispatch)
    finally:
        tee.flush()
        hub.close()
        sandbox.close()


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp()
    print("=== init ===")
    print("  工作目录:", init(d))
    print("  skills:", sorted(p.name for p in (Path(d) / "skills").iterdir()))
    print("  mcp.json:", (Path(d) / "mcp.json").exists())
    print("\n=== 环境报告 ===")
    print(report())
