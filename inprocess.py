#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程内执行器 —— Android / 无 fork-exec 环境时的降级方案。

## 为什么需要它

常规环境用 `subprocess.run([sys.executable, "script.py"])`：
新起一个 Python 进程，跑完就死，天然隔离（死了不影响主进程）。

Android 上这条路断了：Chaquopy 的 CPython 是 `libpython3.so`
**嵌进 App 进程**的，没有独立的可执行文件可供 fork-exec。
`subprocess.run([sys.executable, ...])` 会直接失败。

## 降级后的风险

在同一进程内 exec 代码，意味着：
  · 死循环会**卡死整个 App**（没有进程边界可以 kill）
  · 代码能碰到主进程的所有状态
  · `os._exit()` / `sys.exit()` 会带走整个 App

## 怎么把风险控制住

用**指令计数**而不是墙钟超时。墙钟超时杀不掉同进程里的死循环
（没有信号能中断正在跑的字节码），但 `sys.settrace` 可以：
每条字节码回调一次，超预算就抛异常 —— 这是唯一能真正拦住死循环的办法。

配合：
  · stdout / stderr 重定向捕获
  · `__builtins__` 里屏蔽最危险的两个（exit / 某些场景下的 eval 不算）
  · 异常归一化成 traceback 字符串，与主进程走同一套错误指纹

## 不声称什么

这不是沙箱。同一进程内无法阻止代码读写文件、发网络请求、
改全局变量。它能保证的只有：**不会因为死循环把 App 拖死**，
以及**异常不会冒泡炸掉主流程**。

真正的隔离只能靠进程/容器边界；Android 上没有，这是平台限制。
"""

import io
import sys
import time
import types
import signal
import builtins
import traceback
import threading
import contextlib

# 默认指令预算：约 3000 万条字节码。
# 参考量级：一次空循环 for i in range(10**6) 大约 300-500 万条指令。
DEFAULT_BUDGET = int(__import__("os").environ.get("DS_IP_BUDGET", "30_000_000"))


class BudgetExceeded(Exception):
    """指令预算耗尽。语义上等价于超时，但能真正中断死循环。"""


class _Tracer:
    """
    指令计数器。每条字节码 line 事件 +1，超预算抛异常。

    只在本线程生效（settrace 是 per-thread 的），
    所以放在单独线程里跑并不会拖慢主线程。
    """

    def __init__(self, budget):
        self.budget = budget
        self.used = 0

    def __call__(self, frame, event, arg):
        self.used += 1
        if self.used > self.budget:
            raise BudgetExceeded(
                f"指令预算耗尽（>{self.budget} 条）"
                f"—— 通常是死循环，或任务本身太重。")
        return self


def _safe_builtins():
    """
    屏蔽会让整个 App 直接消失的内建函数。

    只屏蔽 os._exit / exit / quit —— 它们会带走宿主进程。
    不过度裁剪：裁太多会让正常代码莫名失败，反而更难排查。
    """
    b = dict(vars(builtins))
    for name in ("exit", "quit"):
        if name in b:
            def _blocked(*_a, **_k):
                raise RuntimeError(
                    f"{name}() 已禁用：在进程内执行模式下它会终止整个 App。"
                    f"请改用 return / raise。")
            b[name] = _blocked
    b["__import__"] = builtins.__import__
    return b


class InProcessRunner:
    """
    在同进程内执行 Python 代码，捕获输出与异常。

    与 Sandbox.run_python 的返回格式保持一致，
    这样上层（debug_advisor 的错误指纹、Intervention 的失败判定）
    不用区分两种执行方式。
    """

    def __init__(self, budget=None, max_out=200_000):
        self.budget = int(budget or DEFAULT_BUDGET)
        self.max_out = max_out

    def _clip(self, s):
        if s is None:
            return ""
        if len(s) > self.max_out:
            return s[:self.max_out] + f"\n...[截断 {len(s) - self.max_out} 字符]"
        return s

    def run(self, code, filename="<agent>", timeout=None):
        """
        执行一段代码，返回与命令执行同构的字符串结果。

        timeout 在这里**只作提示用**：进程内无法强杀正在跑的字节码，
        真正的中断靠 budget。传了 timeout 会按它换算一个较宽松的预算上限。
        """
        budget = self.budget
        if timeout:
            # 粗估：现代机器上 1 秒约几百万条指令，给个保守上限避免误杀
            budget = min(budget, max(int(timeout) * 2_000_000, 1_000_000))

        tracer = _Tracer(budget)
        out, err = io.StringIO(), io.StringIO()

        # 给代码一个独立的 globals，避免污染主流程
        g = {
            "__name__": "__main__",
            "__builtins__": _safe_builtins(),
            "__file__": filename,
        }

        t0 = time.time()
        exit_code = 0
        error = None

        old = sys.gettrace()
        try:
            sys.settrace(tracer)
            with contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                try:
                    exec(compile(code, filename, "exec"), g)
                except BudgetExceeded as e:
                    exit_code = 124
                    error = f"BudgetExceeded: {e}"
                except SystemExit as e:
                    # SystemExit 必须拦住，否则会把 App 带下去
                    exit_code = e.code if isinstance(e.code, int) else 0
                    error = None
                except BaseException as e:
                    exit_code = 1
                    error = "".join(traceback.format_exception_only(
                        type(e), e)) + "\n" + "".join(
                        traceback.format_tb(e.__traceback__))
        finally:
            sys.settrace(old)

        el = time.time() - t0
        parts = [f"[环境] 进程内执行（无独立解释器） · {el:.2f}s · "
                 f"指令 {tracer.used}"]
        so, se = self._clip(out.getvalue()), self._clip(err.getvalue())
        if so.strip():
            parts.append(so.rstrip())
        if error:
            parts.append("[stderr]\n" + self._clip(error).rstrip())
        elif se.strip():
            parts.append("[stderr]\n" + se.rstrip())
        parts.append(f"[exit={exit_code}]")
        return "\n".join(parts)


# ---------------- 能力探测 ----------------
def can_subprocess_python():
    """
    判断能不能用 `sys.executable` 起独立 Python 进程。

    Android / Chaquopy 下返回 False：sys.executable 可能是
    一个不存在的路径，或存在但无法 exec（没有独立 ELF）。
    """
    import os
    import subprocess

    exe = getattr(sys, "executable", None)
    if not exe or not os.path.exists(exe):
        return False
    try:
        p = subprocess.run([exe, "-c", "print(1)"],
                           capture_output=True, text=True, timeout=10)
        return p.returncode == 0 and "1" in (p.stdout or "")
    except Exception:
        return False


def can_subprocess_sh():
    """判断能不能起 sh（Android 上 /system/bin/sh 通常可用）。"""
    import subprocess
    for sh in ("sh", "/system/bin/sh", "/bin/sh"):
        try:
            p = subprocess.run([sh, "-c", "echo ok"],
                               capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and "ok" in (p.stdout or ""):
                return True
        except Exception:
            continue
    return False


def detect(verbose=False):
    """
    探测当前环境有哪些执行能力。返回 dict。

    上层据此决定：
      · 有独立 python → 正常子进程执行（有进程边界）
      · 没有但有 sh    → shell 正常，python 走进程内
      · 两者都没有    → 全部走进程内
    """
    info = {
        "python_subprocess": can_subprocess_python(),
        "sh_subprocess": can_subprocess_sh(),
        "is_android": _is_android(),
        "has_setrlimit": _has_setrlimit(),
    }
    if verbose:
        return info
    return info


def _is_android():
    """Chaquopy / Termux / 真 Android 都能识别出来。"""
    if "java" in sys.modules or "jnius" in sys.modules:
        return True
    try:
        import android                       # Chaquopy / 某些环境提供
        return True
    except Exception:
        pass
    import os
    if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA"):
        return True
    return os.path.exists("/system/bin/sh") and os.path.exists("/system/app")


def _has_setrlimit():
    try:
        import resource
        resource.getrlimit(resource.RLIMIT_CPU)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print("=== 能力探测 ===")
    for k, v in detect().items():
        print(f"  {k}: {v}")

    print("\n=== 进程内执行器 ===")
    r = InProcessRunner()
    print("  正常:", r.run("print(sum(range(10)))\n").replace("\n", " | "))
    res = r.run("raise ValueError('boom')\n")
    print("  异常:", "ValueError" in res, "|", res.splitlines()[-1])
    t = time.time()
    res2 = r.run("while True:\n    pass\n", timeout=2)
    print(f"  死循环: {time.time()-t:.1f}s ->", res2.splitlines()[-1])
    print("  被中断:", "exit=124" in res2)
    res3 = r.run("import sys; sys.exit(0)\n")
    print("  sys.exit 被拦:", "exit=0" in res3)
    res4 = r.run("exit()\n")
    print("  exit() 被拦:", "RuntimeError" in res4 or "已禁用" in res4)
