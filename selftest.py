#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地自检 —— 与 .github/workflows/ci.yml 完全相同的断言，
push 之前先跑一遍，避免 CI 挂了才知道。

用法： python3 selftest.py
"""

import os
import sys
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"✅ {name}")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"❌ {name}: {e}")
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"❌ {name}: {type(e).__name__}: {e}")


def t_syntax():
    import py_compile
    here = os.path.dirname(os.path.abspath(__file__))
    for f in os.listdir(here):
        if f.endswith(".py"):
            py_compile.compile(os.path.join(here, f), doraise=True)


def t_mcp():
    from mcp_client import MCPClient
    here = os.path.dirname(os.path.abspath(__file__))
    c = MCPClient("demo", sys.executable, [os.path.join(here, "mcp_demo_server.py")])
    c.start()
    assert c.initialize().get("serverInfo"), "initialize 未返回 serverInfo"
    assert len(c.list_tools()) == 3, "工具数应为 3"
    assert c.call_tool("get_time", {}), "get_time 无返回"
    assert "未知工具" in c.call_tool("nope", {})
    c.close()

    c2 = MCPClient("fs", sys.executable, [os.path.join(here, "mcp_fs_server.py")],
                   env={"MCP_FS_ROOTS": "/tmp"})
    c2.start(); c2.initialize()
    assert "fs_read" in [t["name"] for t in c2.list_tools()]
    assert "PermissionError" in c2.call_tool("fs_read", {"path": "/etc/shadow"}), "越界未拦截"
    c2.close()


def t_terminal():
    from terminal import Terminal
    d = tempfile.mkdtemp()
    t = Terminal(d, "local")
    t.run("cd /tmp && pwd")
    assert "/tmp" in t.run("pwd"), "cwd 未保留"
    t.run("export CIVAR=hello")
    assert "hello" in t.run("echo $CIVAR"), "环境变量未保留"
    for code in (0, 1, 42):
        assert f"[exit={code}]" in t.run(f"echo x; exit {code}"), f"exit {code} 回传错误"
    s = time.time()
    assert "TIMEOUT" in t.run("while true; do sleep 1; done", timeout=4), "超时未熔断"
    assert time.time() - s < 10, "熔断耗时过长"
    assert "alive" in t.run("echo alive"), "熔断后会话未恢复"
    t.close()
    shutil.rmtree(d, ignore_errors=True)


def t_stress():
    from terminal import Terminal
    d = tempfile.mkdtemp()
    t = Terminal(d, "local")
    fail = []
    for i in range(60):
        cmd = (f"echo n{i}; exit 0" if i % 3 == 0
               else f"echo n{i}; echo e{i} >&2" if i % 5 == 0 else f"echo n{i}")
        if f"n{i}" not in t.run(cmd):
            fail.append(i)
    t.close()
    shutil.rmtree(d, ignore_errors=True)
    assert not fail, f"解析失败轮次: {fail}"


def t_advisor():
    from debug_advisor import parse_error, categorize, fingerprint, DebugAdvisor
    cases = [
        ("...\nZeroDivisionError: division by zero", "arith"),
        ("...\nTypeError: bad", "type"),
        ("ModuleNotFoundError: No module named 'x'", "import"),
        ("...\nAssertionError: got 5", "logic"),
        ("CUDA out of memory", "oom"),
        ("  File \"m.py\", line 3\nSyntaxError: invalid syntax", "syntax"),
        ("FileNotFoundError: [Errno 2] no such file", "io"),
    ]
    for txt, want in cases:
        got, _ = categorize(parse_error(txt), txt)
        assert got == want, f"分类错误 期望{want} 实际{got}"

    a = parse_error('File "s.py", line 10, in run\nZeroDivisionError: x')
    b = parse_error('File "s.py", line 88, in run\nZeroDivisionError: x')
    d = parse_error('File "s.py", line 10, in other\nZeroDivisionError: x')
    assert fingerprint(a) == fingerprint(b), "同函数不同行号应同一个错"
    assert fingerprint(a) != fingerprint(d), "不同函数应不同的错"

    ad = DebugAdvisor()
    err = 'File "s.py", line 10, in run\nZeroDivisionError: x'
    warned = False
    for i in range(3):
        r = ad.observe("run_python", {"code": f"fix{i}"}, err)
        if r and "⚠️" in r:
            warned = True
    assert warned, "重复失败未触发升级建议"


def t_arch():
    from arch_cards import (is_ml_task, is_arch_task, build_arch_prompt, skeleton_for)
    assert not is_ml_task("写一个快速排序"), "普通任务不应注入"
    assert len(build_arch_prompt("写一个快排")) == 0, "普通任务注入应为空"
    assert is_ml_task("用 pytorch 训练分类模型"), "ML 任务应识别"
    assert is_arch_task("实现 mamba 混合架构"), "架构任务应识别"
    assert len(build_arch_prompt("实现 mamba 混合架构")) > \
        len(build_arch_prompt("用 torch 训练模型")), "架构卡未额外注入"
    assert "HybridBlock" in skeleton_for("mamba 混合架构"), "骨架选择错误"


def t_safe():
    d = tempfile.mkdtemp()
    os.environ["DS_WORKDIR"] = d
    import ds_agent_pro as A
    try:
        A.safe_path("../../etc/passwd")
        raise AssertionError("路径逃逸未被拦截")
    except ValueError:
        pass
    sb = A.Sandbox(A.ROOT, "local", [])
    assert "已拦截" in sb.run_shell("rm -rf / tmp"), "危险命令未被拦截"
    sb.close()
    shutil.rmtree(d, ignore_errors=True)


def t_setup():
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    r = subprocess.run(["sh", "-n", os.path.join(here, "setup.sh")],
                       capture_output=True)
    assert r.returncode == 0, f"setup.sh 语法错误: {r.stderr.decode()}"


if __name__ == "__main__":
    print("=== ds-agent 本地自检（与 CI 同断言）===\n")
    check("语法检查", t_syntax)
    check("MCP 协议端到端", t_mcp)
    check("终端会话（状态/退出码/超时）", t_terminal)
    check("终端可靠性压测 60 次", t_stress)
    check("调试顾问（分类/指纹/升级）", t_advisor)
    check("架构卡片（按需注入）", t_arch)
    check("路径与命令安全", t_safe)
    check("setup.sh 语法", t_setup)
    print(f"\n通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
    if FAIL:
        print("失败项：")
        for n, e in FAIL:
            print(f"  - {n}: {e}")
        sys.exit(1)
    print("✅ 全部通过，可以 push")
