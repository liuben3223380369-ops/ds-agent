#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地自检 —— 与 .github/workflows/ci.yml 完全相同的断言，
push 之前先跑一遍，避免 CI 挂了才知道。

用法： python3 selftest.py
"""

import os
import sys
import json
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
    import ds_agent_pro as A
    # 模块可能已被前面的用例导入（ROOT 在导入时固定），这里直接用它的 ROOT
    os.makedirs(A.ROOT, exist_ok=True)
    try:
        A.safe_path("../../etc/passwd")
        raise AssertionError("路径逃逸未被拦截")
    except ValueError:
        pass
    sb = A.Sandbox(A.ROOT, "local", [])
    assert "已拦截" in sb.run_shell("rm -rf / tmp"), "危险命令未被拦截"
    sb.close()


def t_mcp_http():
    """两种 HTTP 传输 + 集成到 MCPHub（起本地测试服务端）。"""
    import subprocess, signal, tempfile
    from mcp_http import StreamableHTTPClient, SSEClient, build_http_client

    port = 8931
    srv = subprocess.Popen(
        [sys.executable, "tests/mcp_http_test_server.py", str(port)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.5)
        assert srv.poll() is None, "测试服务端启动失败"
        base = f"http://127.0.0.1:{port}"

        # Streamable HTTP
        c = StreamableHTTPClient(f"{base}/mcp", timeout=10)
        info = c.request("initialize", {"protocolVersion": "2025-03-26",
                                        "capabilities": {}, "clientInfo": {}})
        assert info.get("serverInfo"), "initialize 未返回 serverInfo"
        tools = c.request("tools/list")
        assert len(tools["tools"]) == 2, "工具数不符"
        r = c.request("tools/call", {"name": "remote_echo", "arguments": {"text": "hi"}})
        assert "remote echo: hi" in r["content"][0]["text"], "调用结果不符"

        # 旧式 SSE
        s = SSEClient(f"{base}/sse", timeout=10, connect_timeout=8)
        s.start()
        assert s.endpoint, "未拿到 endpoint"
        assert s.request("tools/list")["tools"], "SSE tools/list 为空"
        r2 = s.request("tools/call", {"name": "remote_echo",
                                      "arguments": {"text": "sse"}}, timeout=10)
        assert "remote echo: sse" in r2["content"][0]["text"], "SSE 调用结果不符"
        s.close()

        # 经 MCPHub 集成（streamable）
        import ds_agent_pro as A
        cfgp = os.path.join(tempfile.mkdtemp(), "mcp.json")
        with open(cfgp, "w") as f:
            json.dump({"mcpServers": {"remote": {
                "url": f"{base}/mcp", "type": "streamable-http"}}}, f)
        hub = A.MCPHub(cfgp)
        ts = hub.start()
        assert ts, "MCPHub 未拉到远端工具"
        assert "remote echo" in hub.call(ts[0]["function"]["name"], {"text": "hub"})
        hub.close()
    finally:
        srv.send_signal(signal.SIGKILL)
        try:
            srv.wait(timeout=3)
        except Exception:
            pass


def t_streaming():
    """围栏状态机（流式切碎）+ 工具调用碎片累积。"""
    from streaming import FenceScanner, ToolCallAccumulator, IdleGate

    # 流被切成 7 字节的碎片，围栏仍要正确切出
    sc = FenceScanner()
    full = "前\n```bash\necho a\nls\n```\n中\n```python\nprint(1)\n```\n后\n"
    got = []
    for i in range(0, len(full), 7):
        got += sc.feed(full[i:i + 7])
    got += sc.flush()
    assert len(got) == 2, f"应切出 2 块，实际 {len(got)}: {got}"
    assert got[0][0] == "bash" and "ls" in got[0][1]
    assert got[1][0] == "python"

    # 未闭合不应产出
    sc2 = FenceScanner()
    assert sc2.feed("```bash\necho x\n") == [], "未闭合块不应产出"

    # 工具调用参数碎片累积（后续分片 name 为 None，参数不能丢）
    acc = ToolCallAccumulator()
    acc.feed({"index": 0, "id": "c1", "name": "run_shell", "args": '{"cmd":"ech'})
    acc.feed({"index": 0, "id": None, "name": None, "args": "o hi"})
    acc.feed({"index": 0, "id": None, "name": None, "args": '"}'})
    calls = acc.build()
    assert len(calls) == 1 and calls[0].function.name == "run_shell", "名称未拼出"
    assert json.loads(calls[0].function.arguments) == {"cmd": "echo hi"}, "参数未拼全"

    g = IdleGate(idle_ms=50)
    g.touch()
    assert not g.is_idle(), "刚 touch 不应 idle"
    time.sleep(0.08)
    assert g.is_idle(), "超过 idle_ms 应判 idle"


def t_skills():
    from skills import SkillBook, ensure_builtin, parse_front_matter
    import tempfile

    meta, body = parse_front_matter(
        "---\nname: tdd\ntrigger: 写代码|实现\ntags: [a, b]\n---\n正文")
    assert meta["name"] == "tdd" and meta["trigger"] == "写代码|实现"
    assert "a" in meta["tags"] and body.strip() == "正文"

    d = tempfile.mkdtemp()
    ensure_builtin(d)
    bk = SkillBook(d)
    assert set(["tdd", "debug", "review"]).issubset(set(bk.names())), "内置 skill 缺失"
    assert [s.name for s in bk.select("帮我写个函数")] == ["tdd"]
    assert [s.name for s in bk.select("这个报错怎么修")] == ["debug"]
    assert bk.select("写个快排") == [], "无关任务不应命中"
    assert "### skill:" in bk.render("帮我写代码")


def t_intervene():
    import tempfile
    import builtins
    from intervene import Intervention, CONTINUE, ROLLBACK, MANUAL, HINT, ABORT
    from debug_advisor import DebugAdvisor

    d = tempfile.mkdtemp()

    # 触发条件：连续失败
    g = Intervention(d, stall=3)
    assert g.should_intervene() is None, "初始不应触发"
    for _ in range(3):
        g.tick(False)
    assert g.should_intervene(), "连续 3 轮失败应触发"

    # 触发条件：同一错误累计
    g2 = Intervention(d, stall=99, same=4)
    ad = DebugAdvisor(); g2.advisor = ad
    err = 'File "s.py", line 10, in run\nZeroDivisionError: x'
    for i in range(4):
        ad.observe("run_python", {"code": f"f{i}"}, err)
    g2.tick(False)
    assert g2.should_intervene(), "同错 4 次应触发"

    # 成功信号重置
    g3 = Intervention(d, stall=3)
    g3.tick(False); g3.tick(False)
    assert not g3.should_intervene(), "2 连败不应触发"
    g3.tick(True)
    assert g3.fail_streak == 0, "成功后未重置"

    # 报告落盘且不覆盖（同秒多次）
    g4 = Intervention(d)
    paths = [g4.save_report("x") for _ in range(3)]
    assert all(p and p.exists() for p in paths), "报告未落盘"
    assert len({p.name for p in paths}) == 3, "同秒报告互相覆盖"

    # 决策解析（模拟输入）
    def ask_with(answers, gg):
        it = iter(answers)
        builtins.input = lambda *a, **k: next(it)
        gg._interactive = lambda: True
        return gg.ask("t", "r")
    real_input = builtins.input
    try:
        assert ask_with(["1"], Intervention(d))[0] == CONTINUE
        assert ask_with([""], Intervention(d))[0] == CONTINUE, "回车应默认继续"
        assert ask_with(["2", "3"], Intervention(d)) == (ROLLBACK, 3)
        assert ask_with(["4", "用 Decimal"], Intervention(d)) == (HINT, "用 Decimal")
        assert ask_with(["m"], Intervention(d))[0] == MANUAL
        assert ask_with(["x", "5"], Intervention(d))[0] == ABORT, "非法输入后应可重选"
    finally:
        builtins.input = real_input

    # 非交互策略
    g5 = Intervention(d); g5._interactive = lambda: False
    saved = os.environ.get("DS_ON_STUCK")
    try:
        for pol, exp in [("continue", CONTINUE), ("abort", ABORT),
                         ("rollback", ROLLBACK), ("hint", HINT)]:
            os.environ["DS_ON_STUCK"] = pol
            os.environ["DS_HINT"] = "换个思路"
            assert g5.ask("t", "r")[0] == exp, f"策略 {pol} 不符"
    finally:
        if saved is None:
            os.environ.pop("DS_ON_STUCK", None)
        else:
            os.environ["DS_ON_STUCK"] = saved

    # 提示注入与清空
    g6 = Intervention(d)
    g6.add_note("用 Decimal"); g6.add_note("别用 float")
    txt = g6.drain_notes()
    assert "用 Decimal" in txt and "别用 float" in txt, "提示未全部注入"
    assert g6.notes == [], "取出后未清空"

    import shutil
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
    check("MCP HTTP 传输（streamable + SSE）", t_mcp_http)
    check("流式（围栏状态机 + 调用碎片累积）", t_streaming)
    check("Skill 抽象层（解析/命中/渲染）", t_skills)
    check("主动介入（触发/决策/注入）", t_intervene)
    check("路径与命令安全", t_safe)
    check("setup.sh 语法", t_setup)
    print(f"\n通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
    if FAIL:
        print("失败项：")
        for n, e in FAIL:
            print(f"  - {n}: {e}")
        sys.exit(1)
    print("✅ 全部通过，可以 push")
