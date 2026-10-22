#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调试顾问 —— 让 Agent 不再原地打转。

朴素循环的最大问题：模型用同一种修法反复尝试同一个错误，
20 轮下来全在打补丁，从不怀疑"思路本身错了"。

本模块做三件事：
  1. 错误指纹  —— 识别"是不是同一个错"（不含行号，改位置也算同一个）
  2. 修复指纹  —— 识别"是不是换了修法"（换了好几种修法还没好 = 思路有问题）
  3. 自适应建议 —— 根据上面两个维度，给出当前最该做什么的具体指令

纯标准库，无外部依赖。
"""

import re
import hashlib
from collections import Counter


# ---------------- 错误解析 ----------------
# 常见异常尾行：ZeroDivisionError: division by zero
EXC_LINE = re.compile(
    r"^\s*(?P<cls>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit|Interrupt|Warning))"
    r"(?::\s*(?P<msg>.*))?$", re.M)

# Python traceback 帧：  File "a.py", line 12, in foo
FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?')

# 非 Python 的运行时信号
IMPORT_HINT = re.compile(r"(ModuleNotFoundError|ImportError):\s*(?:No module named\s*)?'?([\w\.\-]+)")


def parse_error(text):
    """从工具输出里提取结构化错误信息。"""
    if not text:
        return None
    info = {"cls": None, "msg": None, "file": None, "line": None,
            "func": None, "frames": []}

    frames = list(FRAME.finditer(text))
    if frames:
        last = frames[-1]
        info["file"] = last.group("file")
        info["line"] = int(last.group("line"))
        info["func"] = last.group("func")
        info["frames"] = [(f.group("file"), int(f.group("line")), f.group("func"))
                          for f in frames]

    for m in EXC_LINE.finditer(text):
        cls, msg = m.group("cls"), (m.group("msg") or "").strip()
        # 排除掉 Warning 结尾误判，且优先取最后一个真正的异常
        if cls.endswith("Warning"):
            continue
        info["cls"], info["msg"] = cls, msg
    return info if (info["cls"] or info["file"]) else None


def fingerprint(err):
    """
    错误指纹：异常类型 + 出错文件 + 函数名。
    刻意不含行号 —— 模型改了几行但没修好，应该算同一个错。
    """
    if not err:
        return None
    key = f"{err['cls']}|{err['file']}|{err['func']}"
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ---------------- 错误分类与处置策略 ----------------
CATEGORIES = [
    ("syntax", ["SyntaxError", "IndentationError", "TabError"],
     "语法/缩进错误。直接看报错行，这类错误不需要猜。"),

    ("import", ["ModuleNotFoundError", "ImportError"],
     "缺依赖。先确认包名，再决定装还是改 import；不要为了绕过报错而删功能。"),

    ("name", ["NameError", "AttributeError", "UnboundLocalError"],
     "名字/属性问题。多半是拼写、作用域或 API 用错，去查真实定义而不是凭记忆改。"),

    ("type", ["TypeError", "ValueError", "KeyError", "IndexError"],
     "类型/取值问题。打印出真实的值和类型再改，别猜。"),

    ("arith", ["ZeroDivisionError", "OverflowError", "ArithmeticError",
               "FloatingPointError", "decimal.DivisionByZero"],
     "算术异常。分母可能为零、数值可能溢出；加保护或检查输入范围，别只 try/except 吞掉。"),

    ("io", ["FileNotFoundError", "PermissionError", "IsADirectoryError", "OSError"],
     "路径或权限问题。先 ls 确认文件真实存在，注意相对路径基准是 [cwd]。"),

    ("logic", ["AssertionError"],
     "断言失败 = 逻辑不对，不是语法问题。回到需求，确认预期行为到底是什么。"),

    ("timeout", ["TimeoutError"],
     "执行超时。先判断是死循环、等输入还是真的慢；加进度输出或用小数据集验证。"),

    ("oom", ["MemoryError", "CUDA out of memory", "CUDA OOM"],
     "内存不足。减小 batch / 数据分片 / 及时 del 大对象，不要直接加机器。"),
]


def categorize(err, text=""):
    blob = (text or "") + " " + ((err or {}).get("cls") or "")
    for name, keys, advice in CATEGORIES:
        for k in keys:
            if k in blob:
                return name, advice
    if err and err["cls"]:
        return "runtime", f"运行时异常 {err['cls']}。读完整 traceback 定位到具体行再动手。"
    return "unknown", "未识别的错误。先把完整输出贴出来看，不要盲目改。"


# ---------------- 调试状态机 ----------------
class DebugAdvisor:
    """
    跟踪一轮任务里的所有失败，判断"是不是在原地打转"，并给出下一步建议。

    三个信号：
      same_error_n   同一个错误出现了几次
      distinct_fixes 对这个错误尝试过几种不同的修法
      no_progress    连续多少轮没有新的成功信号
    """

    def __init__(self, max_same=3, max_fixes=3, max_stall=4):
        self.max_same = max_same
        self.max_fixes = max_fixes
        self.max_stall = max_stall
        self.errors = Counter()          # 指纹 -> 出现次数
        self.fixes = {}                  # 指纹 -> set(修法指纹)
        self.last_err = None
        self.stall = 0
        self.rounds = 0
        self.history = []                # [(轮次, 分类, 异常类)]

    def _fix_fingerprint(self, tool_name, args):
        """修法指纹：用什么工具、改了什么。"""
        import json
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(f"{tool_name}:{raw}".encode()).hexdigest()[:10]

    def note_success(self):
        """出现成功信号（exit=0 且无异常），重置停滞计数。"""
        self.stall = 0

    def observe(self, tool_name, args, result_text):
        """
        观察一次工具执行结果，返回给模型的建议（没有异常则返回 None）。
        """
        self.rounds += 1
        err = parse_error(result_text)
        success = ("[exit=0]" in result_text and err is None)

        if success:
            self.stall = 0
            self.last_err = None
            return None

        cat, base_advice = categorize(err, result_text)
        fp = fingerprint(err)
        self.history.append((self.rounds, cat, (err or {}).get("cls")))

        if fp is None:
            # 有失败但没有可解析的异常（比如纯逻辑错误、输出不符预期）
            self.stall += 1
            if self.stall >= self.max_stall:
                return self._stall_advice()
            return None

        self.errors[fp] += 1
        n = self.errors[fp]
        self.fixes.setdefault(fp, set())
        if tool_name in ("write_file", "run_python", "run_shell"):
            self.fixes[fp].add(self._fix_fingerprint(tool_name, args))
        nfix = len(self.fixes[fp])

        if self.last_err == fp:
            self.stall += 1
        else:
            self.stall = 1
        self.last_err = fp

        where = ""
        if err.get("file"):
            where = f"位置：{err['file']}"
            if err.get("line"):
                where += f":{err['line']}"
            if err.get("func"):
                where += f" ({err['func']})"

        parts = [f"[调试助手] {cat} · {err.get('cls') or '未识别'}"]
        if where:
            parts.append(where)
        if err.get("msg"):
            parts.append(f"信息：{err['msg'][:200]}")
        parts.append(base_advice)

        # 关键升级逻辑：按"重复次数 × 修法种类"分档给建议
        if n >= self.max_same and nfix >= self.max_fixes:
            parts.append(
                f"\n⚠️ 这个错误已出现 {n} 次，你尝试了 {nfix} 种不同修法都没解决。"
                f"\n继续打补丁大概率无效。现在最该做的是："
                f"\n  1) 写一个 10 行以内的最小复现，确认问题到底出在哪一步"
                f"\n  2) 或直接 git_rollback 回到上一个能跑的版本，换一条路重写"
                f"\n不要在同一个位置做第 {nfix+1} 次微调。")
        elif n >= self.max_same:
            parts.append(
                f"\n⚠️ 这个错误已出现 {n} 次（修法 {nfix} 种）。"
                f"\n别再微调了：完整读一遍相关代码，确认假设是否成立，"
                f"\n或者干脆换一种实现思路。")
        elif nfix >= self.max_fixes:
            parts.append(
                f"\n提示：你对同一处已经改了 {nfix} 次。"
                f"\n每次改动前先说清「为什么这次会不一样」，否则就是随机试错。")

        return "\n".join(parts)

    def _stall_advice(self):
        recent = self.history[-self.max_stall:]
        cats = {c for _, c, _ in recent}
        return (
            f"[调试助手] 已连续 {self.stall} 轮没有实质性进展"
            + (f"（涉及 {', '.join(sorted(cats))} 类问题）" if cats else "")
            + "\n建议立刻做一件事，二选一："
            "\n  1) 缩小问题：把输入降到最小，打印中间变量，定位第一次出错的位置"
            "\n  2) 回退重来：git_rollback 到最近一次能跑的版本"
            "\n继续在当前代码上叠修改，只会让它更难修。")

    def summary(self):
        if not self.history:
            return "本轮无失败记录"
        c = Counter(cat for _, cat, _ in self.history)
        top = ", ".join(f"{k}×{v}" for k, v in c.most_common(3))
        return f"共 {self.rounds} 次执行，失败 {len(self.history)} 次；主要类型：{top}"


# ---------------- 测试驱动引导 ----------------
TEST_HINT = re.compile(
    r"\b(def test_|assert |pytest|unittest|self\.assert)", re.I)


def needs_test(code_text):
    """粗略判断：写了实现但完全没写测试。"""
    if not code_text or len(code_text) < 50:
        return False
    return not TEST_HINT.search(code_text)


def test_nudge():
    return (
        "[提示] 你写了实现但没有断言或测试。"
        "加一个最小验证：至少跑一次真实输入并 print/assert 结果，"
        "否则你无法证明它能用。")
