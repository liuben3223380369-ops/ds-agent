#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ds-agent-pro · DeepSeek 自动写代码 → 运行 → 检查 → 自我修复 的完整闭环

三项能力：
  1. Docker 沙箱     —— 代码在 --network=none --memory=512m 的容器里跑，真隔离（无 docker 自动回退本地）
  2. MCP 支持        —— 接任意 stdio MCP Server，工具自动并入模型可用工具集
  3. Git 快照回滚    —— 每步自动提交，跑崩了一键回退

用法：
    pip install openai
    export DEEPSEEK_API_KEY="sk-xxx"
    python3 ds_agent_pro.py "写一个快排并验证正确性"

    python3 ds_agent_pro.py --snapshots          # 查看快照列表
    python3 ds_agent_pro.py --rollback 3         # 回退 3 步
    python3 ds_agent_pro.py --mcp-only           # 只测试 MCP 通道，不调模型

环境变量：
    DS_MODEL      模型，默认 deepseek-v4-pro（省钱用 deepseek-v4-flash）
    DS_WORKDIR    工作目录，默认 ./workspace
    DS_SANDBOX    docker | local | auto（默认 auto）
    DS_NETWORK    none | bridge（默认 none，装包时设 bridge）
    DS_TIMEOUT    单次执行超时秒数（默认 30）
    DS_ROUNDS     最大轮次（默认 20）
    DS_THINKING   1 开启思考模式（更强的规划，更慢更贵）
    DS_SNAPSHOT   0 关闭 git 快照
    DS_IMAGE      docker 镜像（默认 python:3.12-slim）
"""

import os
import sys
import json
import time
import shutil
import hashlib
import subprocess
import tempfile
import uuid
from pathlib import Path

from openai import OpenAI

from mcp_client import MCPClient, load_servers
from terminal import Terminal
from debug_advisor import DebugAdvisor, needs_test, test_nudge
from arch_cards import build_arch_prompt, skeleton_for
from intervene import (Intervention, CONTINUE, ROLLBACK, MANUAL, HINT, ABORT)

# ----------------------------- 配置 -----------------------------
MODEL = os.environ.get("DS_MODEL", "deepseek-v4-pro")
ROOT = Path(os.environ.get("DS_WORKDIR", "./workspace")).resolve()
SANDBOX = os.environ.get("DS_SANDBOX", "auto")
IMAGE = os.environ.get("DS_IMAGE", "python:3.12-slim")
NETWORK = os.environ.get("DS_NETWORK", "none")
TIMEOUT = int(os.environ.get("DS_TIMEOUT", "30"))
MAX_ROUNDS = int(os.environ.get("DS_ROUNDS", "20"))
# 全局看门狗（对应 dsmod 的 WATCHDOG，它用 Handler.postDelayed 15s 保底）
MAX_WALL = int(os.environ.get("DS_WALL", "900"))      # 整个任务的时间上限（秒）
MAX_OUT = int(os.environ.get("DS_MAX_OUT", "20000"))
THINKING = os.environ.get("DS_THINKING", "0") == "1"
SNAPSHOT_ON = os.environ.get("DS_SNAPSHOT", "1") == "1"
MEMORY = os.environ.get("DS_MEMORY", "512m")
CPUS = os.environ.get("DS_CPUS", "1")
HERE = Path(__file__).resolve().parent          # 脚本所在目录
MCP_CFG = os.environ.get("DS_MCP_CFG") or str(HERE / "mcp.json")

# 沙箱外部链接：授权给模型的真实文件夹（手机目录 / /sdcard/Download 等）
# 多路径用 : 分隔；设为只读用 DS_FS_READONLY=1
FS_ROOTS = [Path(p).expanduser().resolve()
            for p in os.environ.get("DS_FS_ROOTS", "").split(":") if p.strip()]
FS_READONLY = os.environ.get("DS_FS_READONLY", "0") in ("1", "true", "yes")

BLOCK = ["rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "reboot",
         "chmod 777 /", "> /dev/sda", "curl | sh", "wget | sh",
         "git push --force", "rm -rf .git"]
# ---------------------------------------------------------------


def clip(s, n=MAX_OUT):
    if len(s) <= n:
        return s
    half = n // 2
    return s[:half] + f"\n...[截断 {len(s)-n} 字符]...\n" + s[-half:]


def safe_path(p: str) -> Path:
    fp = (ROOT / p).resolve()
    if not str(fp).startswith(str(ROOT)):
        raise ValueError(f"路径越界（只允许在 {ROOT} 内）: {p}")
    return fp


def sh(cmd, cwd=None, timeout=30, check=False):
    r = subprocess.run(cmd, cwd=cwd or ROOT, shell=True, capture_output=True,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ============================ 1. Docker 沙箱 ============================
class Sandbox:
    """docker 可用就容器隔离；不可用回退本地 subprocess（会明确警告）。"""

    def __init__(self, root, mode="auto", mounts=None):
        self.root = root
        self.mode = mode
        self.mounts = mounts or []          # [(宿主机绝对路径, 容器内路径, 读写)]
        self.note = ""
        if mode == "auto":
            self.mode = "docker" if self._docker_ok() else "local"
        # 持久终端会话：cwd / 环境变量跨调用保留（改进 dsmod 每次新进程的做法）
        self.term = Terminal(root, self.mode, mounts=self.mounts, image=IMAGE,
                             network=NETWORK, memory=MEMORY, cpus=CPUS)
        self.mode = self.term.mode          # docker 启动失败会被 Terminal 降级
        self.note = self.term.note
        if self.mounts:
            rw = "ro" if FS_READONLY else "rw"
            self.note += f"\n[外链] 已挂载 {len(self.mounts)} 个外部目录（{rw}）："
            for host, cpath, _ in self.mounts:
                self.note += f"\n         {host} → 容器内 {cpath}"

    @staticmethod
    def _docker_ok():
        if not shutil.which("docker"):
            return False
        try:
            return sh("docker info", timeout=20)[0] == 0
        except Exception:
            return False

    def _mount_flags(self):
        """把外部目录挂进容器。只读模式一律 :ro。"""
        flags = []
        for i, (host, cpath, _rw) in enumerate(self.mounts):
            mode = "ro" if FS_READONLY else _rw
            flags.append(f"-v {json.dumps(str(host))}:{cpath}:{mode}")
        return (" " + " ".join(flags)) if flags else ""

    def _docker_run(self, argv, timeout):
        """复用长驻容器执行（省掉每次 docker run 的启动开销），cwd 跟随会话。"""
        container = self.term.container if self.term else None
        if container:
            try:
                p = subprocess.run(
                    ["docker", "exec", "-w", self.term.cwd, container] + argv,
                    capture_output=True, text=True, timeout=timeout)
                parts = [f"[环境] Docker 容器 {IMAGE} · cwd={self.term.cwd}"]
                if p.stdout.strip():
                    parts.append(clip(p.stdout.rstrip()))
                if p.stderr.strip():
                    parts.append("[stderr]\n" + clip(p.stderr.rstrip(), MAX_OUT))
                parts.append(f"[exit={p.returncode}]")
                return "\n".join(parts)
            except subprocess.TimeoutExpired:
                return f"[TIMEOUT] 超过 {timeout}s"

        name = f"dsrun_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        cmd = (f"docker run --rm --name {name} --network={NETWORK} "
               f"--memory={MEMORY} --cpus={CPUS} --pids-limit=128 "
               f"-v {self.root}:/w{self._mount_flags()} -w /w {IMAGE} " + " ".join(argv))
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            try:
                out, _ = p.communicate(timeout=timeout)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                p.kill()
                subprocess.run(f"docker rm -f {name} >/dev/null 2>&1", shell=True)
                return f"[TIMEOUT] 超过 {timeout}s，已强制销毁容器"
            return f"[exit={rc}]\n{out}".strip()
        except Exception as e:
            return f"[沙箱异常] {type(e).__name__}: {e}"

    def close(self):
        if self.term:
            self.term.close()

    def run_python(self, code, timeout=TIMEOUT):
        """一次性执行：语义清晰，不受会话状态污染。cwd 跟随终端会话。"""
        rel = f".dsrun/run_{uuid.uuid4().hex[:8]}.py"
        fp = safe_path(rel)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(code, encoding="utf-8")
        cwd = getattr(self, "cwd", self.root)
        try:
            if self.mode == "docker":
                return self._docker_run(["python", "-u", f"/w/{rel}"], timeout)
            p = subprocess.run([sys.executable, "-u", str(fp)], cwd=cwd,
                               capture_output=True, text=True, timeout=timeout)
            parts = [f"[环境] 本机 Python {sys.version.split()[0]} · cwd={cwd}"]
            if p.stdout.strip():
                parts.append(clip(p.stdout.rstrip()))
            if p.stderr.strip():
                parts.append("[stderr]\n" + clip(p.stderr.rstrip(), MAX_OUT))
            parts.append(f"[exit={p.returncode}]")
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            return f"[TIMEOUT] 超过 {timeout}s，可能死循环或等待输入"
        finally:
            try:
                fp.unlink()
            except Exception:
                pass

    def run_shell(self, cmd, timeout=TIMEOUT):
        low = cmd.lower()
        for b in BLOCK:
            if b in low:
                return f"[已拦截] 危险命令: {b}"
        return self.term.run(cmd, timeout)      # 走持久会话

    @property
    def cwd(self):
        return self.term.cwd if self.term else self.root


# ============================ 2. Git 快照 ============================
class Snapshot:
    """每步自动 git commit，支持按步数回退。"""

    def __init__(self, root, enabled=True):
        self.root = root
        self.enabled = enabled
        self.hashes = []
        if not enabled:
            return
        root.mkdir(parents=True, exist_ok=True)
        if sh("git rev-parse --git-dir")[0] != 0:
            sh("git init")
            sh('git config user.email "ds-agent@local"')
            sh('git config user.name "ds-agent"')
        gi = root / ".gitignore"
        if not gi.exists():
            gi.write_text(".dsrun/\n__pycache__/\n.venv/\n", encoding="utf-8")

    def commit(self, message):
        if not self.enabled:
            return None
        sh("git add -A")
        code, out = sh(f'git commit -m {json.dumps(message[:200])} --allow-empty')
        if code != 0:
            return None
        rc, h = sh("git rev-parse HEAD")
        if rc == 0 and h.strip():
            self.hashes.append(h.strip())
            return h.strip()
        return None

    def list(self):
        if not self.enabled:
            return "快照功能已关闭"
        rc, out = sh("git log --oneline --reverse")
        return out.strip() or "(暂无快照)"

    def rollback(self, steps=1):
        """回退 N 步。先把当前状态存成保险点，再硬回退。"""
        if not self.enabled:
            return "快照功能已关闭，无法回退"
        if steps < 1:
            return "steps 必须 >= 1"
        rc, log = sh("git log --format=%H")
        hs = [x for x in log.split() if x]
        if len(hs) <= steps:
            return f"只有 {len(hs)} 个快照，无法回退 {steps} 步（如需清空请用 git checkout . ）"
        target = hs[steps]          # hs[0] 是最新
        self.commit(f"回滚前保险点（目标 {target[:8]}）")
        rc, out = sh(f"git reset --hard {target}")
        if rc != 0:
            return f"回退失败: {out}"
        _, cur = sh("git rev-parse HEAD")
        return f"✅ 已回退 {steps} 步 → {cur.strip()[:8]}（原状态已存为保险点，可用 git reflog 找回）"


# ============================ 3. MCP 集成 ============================
class MCPHub:
    def __init__(self, cfg_path):
        self.cfg_path = cfg_path
        self.clients = {}          # name -> MCPClient
        self.mapping = {}          # openai函数名 -> (client_name, 真实工具名)
        self.tools = []

    def start(self):
        specs = load_servers(self.cfg_path)
        if not specs:
            return []
        for name, command, args, env in specs:
            if not command:
                continue
            try:
                c = MCPClient(name, command, args, env)
                c.start()
                c.initialize()
                c.list_tools()
                self.clients[name] = c
                for t in c.to_openai_tools():
                    fname = t["function"]["name"]
                    real = fname.split("__", 1)[1]
                    self.mapping[fname] = (name, real)
                    self.tools.append(t)
                print(f"[MCP] {name}: 已连接，{len(c.tools)} 个工具", file=sys.stderr)
            except Exception as e:
                print(f"[MCP] {name}: 连接失败 —— {type(e).__name__}: {e}", file=sys.stderr)
        return self.tools

    def call(self, fname, arguments):
        name, real = self.mapping[fname]
        return self.clients[name].call_tool(real, arguments, timeout=TIMEOUT * 2)

    def close(self):
        for c in self.clients.values():
            c.close()


# ============================ 主流程 ============================
SYSTEM = """你是一个会自己验证代码的程序员。
工作流：写代码 → 立刻运行 → 看报错 → 修 → 再运行 → 直到全部通过。
规则：
1. 工作目录内的文件用相对路径；外部授权目录用绝对路径。
2. 每写完代码就立刻 run_python 验证，不许凭空断言它能跑。
3. 失败时先读完整 traceback 再改，不要盲目重写整个文件。
4. 每一步改动都会自动 git 快照；如果改崩了，用 git_rollback 回退。
5. 通过后给结论：你跑了什么、输出是什么、为什么算通过。

【终端会话】run_shell 是持久会话，cwd 和环境变量跨调用保留：
· 可以 cd 到某目录后连续执行，不必每条命令都写绝对路径
· export 的变量后续命令仍然有效
· 每次返回都会带上 [cwd]，告诉你当前在哪；输出里的 [exit] 是退出码
· 想回到初始状态用 shell_reset
· 注意：直接执行 exit 会结束当前会话（系统会自动重建并恢复环境）
"""

BASE_TOOLS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "把内容写入工作目录下的文件（自动建父目录）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对路径，如 sort.py"},
            "content": {"type": "string", "description": "文件全文"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取工作目录下的文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对路径"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": "在沙箱中执行 Python 代码，返回 stdout+stderr 与退出码。用于验证代码是否跑通。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "要执行的 Python 代码全文"}},
            "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "在沙箱中执行 shell 命令（pytest、ls、pip install 等）。破坏性命令会被拦截。",
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "shell 命令"}},
            "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "git_rollback",
        "description": "把工作目录回退到之前的 git 快照。改崩了才用。",
        "parameters": {"type": "object", "properties": {
            "steps": {"type": "integer", "description": "回退几步，默认 1"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "shell_reset",
        "description": "重置终端会话：清掉累计的环境变量、把 cwd 恢复到初始目录。状态被污染时用。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def make_dispatch(sandbox: Sandbox, snap: Snapshot, hub: MCPHub):
    def write_file(path, content):
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        snap.commit(f"写入 {path}")
        return f"已写入 {fp.relative_to(ROOT)}（{len(content)} 字符）"

    def read_file(path):
        fp = safe_path(path)
        return clip(fp.read_text(encoding="utf-8", errors="replace")) if fp.exists() else f"文件不存在: {path}"

    def git_rollback(steps=1):
        return snap.rollback(int(steps or 1))

    d = {
        "write_file": lambda a: write_file(a["path"], a["content"]),
        "read_file": lambda a: read_file(a["path"]),
        "run_python": lambda a: sandbox.run_python(a["code"]),
        "run_shell": lambda a: sandbox.run_shell(a["cmd"]),
        "git_rollback": lambda a: git_rollback(a.get("steps", 1)),
        "shell_reset": lambda a: sandbox.term.reset(),
    }
    for fname in hub.mapping:                      # MCP 工具并入
        d[fname] = (lambda f: (lambda a: clip(hub.call(f, a))))(fname)
    return d


def build_system(sandbox, task=""):
    """在系统提示里告诉模型：外部目录在哪、容器里怎么访问、能读能不能写；
    涉及模型/训练时再按需注入架构知识。"""
    s = SYSTEM
    arch = build_arch_prompt(task)
    if arch:
        s += "\n" + arch
        skel = skeleton_for(task)
        if skel:
            s += "\n【可用骨架】按需取用，不要照抄：\n" + skel
    if not FS_ROOTS:
        return s
    rw = "只读，禁止写入/删除" if FS_READONLY else "可读写"
    lines = [f"\n\n【沙箱外部链接】以下真实目录已授权给你（{rw}）："]
    for i, r in enumerate(FS_ROOTS):
        cpath = f"/ext/{i}"
        if sandbox.mode == "docker":
            lines.append(f"  - 宿主机 {r}  →  容器内 {cpath}")
            lines.append(f"    在 run_python 里用绝对路径 {cpath} 访问；用 fs_* 工具时传宿主机路径 {r}")
        else:
            lines.append(f"  - {r}")
            lines.append(f"    本地执行模式下，run_python 可直接用绝对路径 {r}；也可用 fs_* 工具")
    lines.append("用 fs_roots 可随时确认授权范围；越界路径会被拒绝。")
    return s + "\n".join(lines)


def run(task, sandbox, snap, hub, tools, dispatch, client=None):
    client = client or OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                              base_url="https://api.deepseek.com")
    messages = [{"role": "system", "content": build_system(sandbox, task)},
                {"role": "user", "content": task}]
    seen = set()
    advisor = DebugAdvisor(
        max_same=int(os.environ.get("DS_MAX_SAME", "3")),
        max_fixes=int(os.environ.get("DS_MAX_FIXES", "3")),
        max_stall=int(os.environ.get("DS_MAX_STALL", "4")),
    )
    t0 = time.time()
    gate = Intervention(
        ROOT, snap=snap, sandbox=sandbox, advisor=advisor,
        stall=int(os.environ.get("DS_ITER_STALL", "3")),
        same=int(os.environ.get("DS_ITER_SAME", "4")),
        budget=int(os.environ.get("DS_ITER_TIME", "240")),
        enabled=os.environ.get("DS_INTERVENE", "1") != "0",
    )

    for rnd in range(1, MAX_ROUNDS + 1):
        if time.time() - t0 > MAX_WALL:          # 看门狗：不看轮次，只看墙钟
            print(f"\n[看门狗] 已运行 {int(time.time()-t0)}s，超过上限 {MAX_WALL}s，强制停止。"
                  f"\n可用 --rollback N 回退，或调大 DS_WALL。")
            return

        # 用户的提示在下一轮请求前注入
        note = gate.drain_notes()
        if note:
            messages.append({"role": "user", "content": note})

        kw = dict(model=MODEL, messages=messages, tools=tools)
        if THINKING:
            kw["extra_body"] = {"thinking": {"type": "enabled"}}
        msg = client.chat.completions.create(**kw).choices[0].message
        messages.append(msg)          # 整条回传，thinking 模式下 reasoning_content 不丢

        if not msg.tool_calls:
            print(f"\n===== 最终结论 =====\n{msg.content}")
            snap.commit("任务完成")
            return

        round_ok = False
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            key = hashlib.md5(f"{name}:{json.dumps(args, sort_keys=True)}".encode()).hexdigest()

            if key in seen:
                result = "[跳过] 完全相同的调用已执行过，请勿原地打转；换个思路或直接给结论。"
            elif name in dispatch:
                try:
                    result = dispatch[name](args)
                except Exception as e:
                    result = f"[工具异常] {type(e).__name__}: {e}"
            else:
                result = f"[未知工具] {name}"

            seen.add(key)

            # 调试顾问：识别重复失败，按次数升级建议，避免原地打转
            advice = advisor.observe(name, args, result)
            # 写了实现却没有任何断言/测试 → 提示补一个最小验证
            if (name in ("write_file", "run_python") and not advice
                    and needs_test(args.get("content") or args.get("code", ""))):
                advice = test_nudge()

            body = clip(result)
            if advice:
                body = body + "\n\n" + advice

            print(f"\n--- 轮{rnd} · {name} ---\n{clip(result, 2000)}")
            if advice:
                print(f"  {advice.splitlines()[0]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": body})

            # 本轮是否有实质进展：执行成功且无异常
            if "[exit=0]" in result and "[调试助手]" not in body:
                round_ok = True

        gate.tick(round_ok)

        # 主动介入：卡住就停下来问人，而不是烧 token 硬撑
        reason = gate.should_intervene()
        if reason:
            decision, data = gate.ask(task, reason)
            if decision == ABORT:
                print("\n[已放弃] 现场保留在工作目录与 git 快照中。")
                return
            if decision == ROLLBACK:
                print(f"\n[回滚] {snap.rollback(data or 1)}")
                gate.fail_streak = 0
                continue
            if decision == MANUAL:
                summary = gate.manual_session()
                gate.add_note("用户手动介入并修改了代码：\n" + summary
                              + "\n请读取当前文件最新内容，在此基础上继续，"
                                "不要重复用户已经做过的修改。")
                continue
            if decision == HINT:
                gate.add_note(data)
                continue
            # CONTINUE：重置计数，再给一轮机会
            gate.fail_streak = 0

    print(f"\n[停止] 已达 {MAX_ROUNDS} 轮上限，未收敛。可用 --rollback N 回退。")
    print(f"[调试汇总] {advisor.summary()}")


def ensure_mcp_cfg():
    """自动生成 mcp.json。若设置了 DS_FS_ROOTS，同步写入 filesystem server。"""
    cfg = {}
    if Path(MCP_CFG).exists():
        try:
            cfg = json.loads(Path(MCP_CFG).read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    servers = cfg.setdefault("mcpServers", {})
    servers.setdefault("demo", {"command": sys.executable,
                                "args": [str(HERE / "mcp_demo_server.py")]})
    if FS_ROOTS:
        # 用环境变量传根目录，避免路径里的空格/中文在命令行上出问题
        servers["filesystem"] = {
            "command": sys.executable,
            "args": [str(HERE / "mcp_fs_server.py")],
            "env": {"MCP_FS_ROOTS": ":".join(str(r) for r in FS_ROOTS),
                    "MCP_FS_READONLY": "1" if FS_READONLY else "0"},
        }
    # 注意：FS_ROOTS 为空时不动 filesystem 键 —— 用户可能已手写配置，不能误删

    is_new = not Path(MCP_CFG).exists()
    Path(MCP_CFG).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    if is_new:
        print(f"[MCP] 已生成配置 {MCP_CFG}", file=sys.stderr)
    if FS_ROOTS:
        print(f"[外链] filesystem server 已启用，授权目录: "
              f"{[str(r) for r in FS_ROOTS]} 只读={FS_READONLY}", file=sys.stderr)


def build_mounts():
    """外部目录 → 容器内路径映射（Docker 模式生效）。"""
    mounts = []
    for i, r in enumerate(FS_ROOTS):
        if not r.exists():
            print(f"[外链] ⚠️ 目录不存在，已跳过: {r}", file=sys.stderr)
            continue
        mounts.append((r, f"/ext/{i}", "rw"))
    return mounts


def main():
    args = sys.argv[1:]
    ROOT.mkdir(parents=True, exist_ok=True)
    snap = Snapshot(ROOT, SNAPSHOT_ON)

    if "--snapshots" in args:
        print(snap.list()); return
    if "--rollback" in args:
        i = args.index("--rollback")
        print(snap.rollback(int(args[i + 1]) if i + 1 < len(args) else 1)); return

    if not os.environ.get("DEEPSEEK_API_KEY") and "--mcp-only" not in args:
        sys.exit("请先 export DEEPSEEK_API_KEY=sk-xxx")

    ensure_mcp_cfg()
    hub = MCPHub(MCP_CFG)
    mcp_tools = hub.start()
    tools = BASE_TOOLS + mcp_tools

    sandbox = Sandbox(ROOT, SANDBOX, build_mounts())
    print(sandbox.note, file=sys.stderr)
    snap.commit("任务开始")

    if "--mcp-only" in args:
        print("\n[MCP 工具列表]")
        for t in mcp_tools:
            print(f"  - {t['function']['name']}: {t['function']['description'][:60]}")
        if mcp_tools:
            fname = mcp_tools[0]["function"]["name"]
            print(f"\n[试调 {fname}] -> {hub.call(fname, {})}")
        hub.close(); return

    task = " ".join(a for a in args if not a.startswith("-")) or "写一个快速排序并验证正确性"
    dispatch = make_dispatch(sandbox, snap, hub)
    try:
        run(task, sandbox, snap, hub, tools, dispatch)
    finally:
        hub.close()
        sandbox.close()          # 销毁长驻容器 / 常驻 shell


if __name__ == "__main__":
    main()
