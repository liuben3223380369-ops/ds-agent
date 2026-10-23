#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地模式加固 —— 没有 Docker 时，把破坏半径尽量锁小。

安卓 / Termux / 无 Docker 的机器上必然回退本地执行，此时 AI 的代码
跑在真实环境里。命令黑名单拦不住所有情况（拼接、编码、间接调用），
所以这里做**多层**而不是单层：

  1. 资源上限   —— CPU 秒数、内存、文件数、进程数（rlimits，内核强制）
  2. 危险命令   —— 模式匹配拦截（保留原有黑名单，并补充常见绕过）
  3. 写操作确认 —— 工作目录外的写/删，需经审批回调（Web UI 会弹确认）
  4. 网络默认断 —— 可选，防止代码外传数据

rlimits 是关键：黑名单会被绕过，但内核的 RLIMIT_CPU / RLIMIT_AS
绕不过去。这是"能接受降级隔离"时最值得做的一层。
"""

import os
import re
import sys
import resource

# ---------------- 1. 资源上限 ----------------
DEFAULT_LIMITS = {
    "cpu": 60,             # CPU 秒（不是墙钟）
    "as": 2 * 1024 ** 3,   # 地址空间 2GB
    "nofile": 256,         # 打开的文件数
    "nproc": 128,          # 进程数
    "fsize": 512 * 1024 ** 2,   # 单文件最大 512MB
}

RLIMIT_MAP = {
    "cpu": resource.RLIMIT_CPU,
    "as": resource.RLIMIT_AS,
    "nofile": resource.RLIMIT_NOFILE,
    "nproc": resource.RLIMIT_NPROC,
    "fsize": resource.RLIMIT_FSIZE,
}


def build_limits(env=None):
    """从环境变量读限额，未设则用默认。DS_NO_LIMIT=1 可整体关闭。"""
    env = os.environ if env is None else env
    if env.get("DS_NO_LIMIT") == "1":
        return {}
    out = {}
    for k, default in DEFAULT_LIMITS.items():
        raw = env.get(f"DS_LIMIT_{k.upper()}")
        if raw:
            try:
                out[k] = int(raw)
            except ValueError:
                out[k] = default
        else:
            out[k] = default
    return out


def _apply(limits):
    """在子进程里应用 rlimit。只能降不能升，所以越界会抛异常。"""
    for k, v in limits.items():
        if k not in RLIMIT_MAP or v is None:
            continue
        try:
            soft, hard = resource.getrlimit(RLIMIT_MAP[k])
            # 不能超过硬上限，否则内核直接拒绝
            new = min(v, hard) if hard != resource.RLIM_INFINITY else v
            resource.setrlimit(RLIMIT_MAP[k], (new, hard))
        except (ValueError, OSError):
            pass


def preexec(limits=None):
    """给 subprocess 的 preexec_fn 用。返回 None 表示不加限制。"""
    limits = DEFAULT_LIMITS if limits is None else limits
    if not limits:
        return None

    def _fn():
        try:
            os.setsid()          # 独立进程组，便于整组 kill
        except OSError:
            pass
        _apply(limits)

    return _fn


# ---------------- 2. 危险命令拦截 ----------------
# 注意：这是**辅助**层，拦的是常见写法与懒惰绕过，不声称能拦住一切。
DANGEROUS = [
    # 删除类
    # 注意：路径后面的边界不能用 \b（/ 不是单词字符，\b 会失配），
    # 必须用 lookahead 匹配空白 / 命令分隔符 / 行尾
    (re.compile(r"\brm\s+(-[rRfF]+\s+)*(/|/\*|/bin|/boot|/etc|/lib|/lib64|"
                r"/proc|/root|/sbin|/sys|/usr|/var|/data|/system|/sdcard)"
                r"(?=[\s;&|)/]|$)"),
     "删除系统/根目录"),
    (re.compile(r"\brm\s+-[rRfF]*[rR][rRfF]*\s+(\.|~|~/?\*|\$HOME)(?=[\s;&|)]|$)"),
     "删除家目录"),
    (re.compile(r"\brm\s+-[rRfF]*[rR][rRfF]*\s+\*"), "通配符递归删除"),
    (re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"), "格式化文件系统"),
    (re.compile(r"\bdd\s+.*\bof\s*=\s*/dev/[sh]d[a-z]"), "直接写块设备"),
    (re.compile(r":\(\)\s*\{.*\}\s*;"), "fork 炸弹"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "关机/重启"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*7[0-7][0-7]\s+/"), "根目录开放权限"),
    (re.compile(r"\bchown\s+-R\s+.*\s+/\s*$"), "递归改根目录属主"),
    (re.compile(r"\b(mv|cp)\s+.*\s+/dev/(null|zero)\s*$"), "覆盖设备文件"),
    (re.compile(r">\s*/dev/sd[a-z]"), "重定向覆盖磁盘"),
    (re.compile(r"\bsu\s+-\s*$|\bsudo\s+"), "提权"),
    # 常见绕过：base64 / eval 解码执行
    (re.compile(r"\b(base64|base32)\s+(-d|--decode)\b.*\|\s*(sh|bash|python)"),
     "解码后直接执行"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sh|bash)\b"), "下载即执行"),
]


def scan_command(cmd):
    """返回 (是否危险, 原因)。不危险则 (False, None)。"""
    if not cmd:
        return False, None
    text = cmd
    for pat, why in DANGEROUS:
        if pat.search(text):
            return True, why
    return False, None


# ---------------- 3. 写操作审批 ----------------
WRITE_PATTERNS = [
    re.compile(r"\brm\b|\brmdir\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\bdd\b"),
    re.compile(r"\btee\b"),
    re.compile(r"\btruncate\b"),
    re.compile(r"\bsed\s+-i\b"),
    re.compile(r"\b chmod \b"),
    re.compile(r">\s*[^>&]"),            # 重定向写（排除 >> 追加以免误伤日志）
    re.compile(r"\bmkdir\b|\btouch\b"),
    re.compile(r"\bgit\s+(push|reset\s+--hard|clean\s+-fd)\b"),
]


def needs_approval(cmd, outside_root):
    """
    是否需要对这条命令做人工确认。

    两个条件任一成立：
      · 命中写操作模式
      · 命令里出现了工作目录之外的绝对路径
    """
    if not cmd:
        return False, ""
    for pat in WRITE_PATTERNS:
        if pat.search(cmd):
            return True, "写/删类操作"
    if outside_root and outside_root(cmd):
        return True, "涉及工作目录之外的路径"
    return False, ""


class Gate:
    """
    审批闸门。approver 为 None 时放行（保持与旧行为一致）。

    approver(cmd, why) -> True/False
    Web UI 里接到请求会弹确认框；无人应答按 timeout 后的默认处理。
    """

    def __init__(self, approver=None, default_allow=False,
                 outside_root=None, enabled=True):
        self.approver = approver
        self.default_allow = default_allow
        self.outside_root = outside_root
        self.enabled = enabled

    def check(self, cmd):
        """返回 (放行, 拦截原因)。"""
        if not self.enabled:
            return True, ""

        danger, why = scan_command(cmd)
        if danger:
            return False, f"危险命令（{why}）"

        need, why = needs_approval(cmd, self.outside_root)
        if not need:
            return True, ""

        if self.approver is None:
            return True, ""

        try:
            ok = self.approver(cmd, why)
        except Exception:
            ok = self.default_allow
        if ok is None:                     # 无人应答
            ok = self.default_allow
        return bool(ok), "" if ok else f"已拒绝（{why}）"


# ---------------- 4. 网络默认断（本地模式） ----------------
def netwrap(cmd):
    """
    本地模式下尽量切断网络。无 unshare 时返回 None（不包裹）。

    Linux 上用 unshare -n 建空网络命名空间；无权限则放弃，
    不假装成功——这一点很重要。
    """
    if os.environ.get("DS_LOCAL_NET", "0") != "1":
        return None
    import shutil
    if not shutil.which("unshare"):
        return None
    return f"unshare -n sh -c {_shquote(cmd)}"


def _shquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ---------------- 汇总 ----------------
def describe(limits, gate, net):
    parts = []
    if limits:
        l = []
        if "cpu" in limits:
            l.append(f"CPU {limits['cpu']}s")
        if "as" in limits:
            l.append(f"内存 {limits['as'] // 1024 ** 3}GB")
        if "nofile" in limits:
            l.append(f"文件 {limits['nofile']}")
        if "nproc" in limits:
            l.append(f"进程 {limits['nproc']}")
        parts.append("资源上限: " + " / ".join(l))
    else:
        parts.append("资源上限: 未启用")
    parts.append(f"命令闸门: {'开' if gate.enabled else '关'}"
                 + ("（带审批回调）" if gate.approver else "（无审批回调，仅拦截危险命令）"))
    parts.append(f"网络隔离: {'unshare -n' if net else '未启用（需要 root 权限）'}")
    return "\n".join(parts)
