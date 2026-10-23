"""
Kotlin 与 ds-agent 之间的桥接层。

Chaquopy 把 Python 代码打成 asset 从 APK 直接加载，**没有独立可执行文件**，
所以 Android 上 `subprocess.run([sys.executable, ...])` 一定失败。
android_shim / inprocess 已经处理了这条降级路径，这里只负责接线。

Kotlin 侧调用：
    val bridge = Python.getInstance().getModule("agent_bridge")
    bridge.callAttr("env_report", filesDir.absolutePath)      -> String
    bridge.callAttr("run", task, filesDir.absolutePath, sink) -> None
                                          # sink 需有 onEvent(text, kind)
"""

import os
import sys
import traceback

# Chaquopy 下模块从 APK asset 加载，当前目录未必可用；
# 显式把仓库的 python 目录加进搜索路径，保证能 import 主模块。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "python")):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


def _emit(sink, text, kind="log"):
    if sink is None:
        print(text, flush=True)
        return
    try:
        sink.onEvent(str(text), str(kind))
    except Exception:
        # 回调断了不能让任务崩掉，退化到 stdout（Chaquopy 会转发到 logcat）
        print(text, flush=True)


def env_report(files_dir=None):
    """给 UI 用的一段环境说明，帮助确认降级状态。"""
    try:
        import android_shim
        android_shim.init(files_dir)
        return android_shim.report()
    except Exception as e:
        return f"环境探测失败：{type(e).__name__}: {e}\n{traceback.format_exc()}"


def run(task, files_dir=None, api_key=None, sink=None):
    """
    执行一次任务，输出实时推给 sink。

    files_dir: context.filesDir 的绝对路径（App 私有可写目录）
    api_key:   DeepSeek API Key（Android 上没有环境变量，只能显式传）
    sink:      有 onEvent(text, kind) 方法的对象
    """
    try:
        if api_key:
            os.environ["DEEPSEEK_API_KEY"] = str(api_key)
        import android_shim
        # 必须在 import ds_agent_pro 之前 —— ROOT 在模块级就固定了
        android_shim.init(files_dir)
        _emit(sink, f"工作目录：{os.environ['DS_WORKDIR']}", "sys")

        android_shim.run_task(task, on_event=lambda t, k="log": _emit(sink, t, k))
    except Exception as e:
        _emit(sink, f"任务异常：{type(e).__name__}: {e}", "err")
        _emit(sink, traceback.format_exc(), "err")
    finally:
        _emit(sink, "", "end")
