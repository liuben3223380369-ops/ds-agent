#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
流式输出 + IDLE 判定 —— 对齐 dsmod 的 IDLE_MS / 围栏解析那套。

dsmod 的做法（`Exp.extractBlocks` + `IDLE_MS = 0x5dc = 1500`）：
    · 流式 delta 到达时，手写 219 行状态机判断围栏是否闭合
    · 空闲 1500ms 无新内容 → 判定代码块结束 → 送去执行
    · 为什么不用正则：delta 可能停在围栏中间，正则匹配不上

我们走 API 结构化 tool_calls，本来不需要这套。
但代价是**无法边生成边显示**——必须等整条响应返回。

本模块补上的是"显示侧"的流式能力：
    · StreamRenderer：逐 chunk 渲染，模型边想边看得见
    · FenceScanner：从自由文本里切出围栏代码块（供 skill/bash 场景用）
    · IdleGate：空闲判定，IDLE_MS 可配

三个类互不依赖，可以单独用。
"""

import re
import time
import sys

# 围栏：```lang\n...\n```
FENCE_OPEN = re.compile(r"^```(?P<lang>[A-Za-z0-9_+\-]*)\s*$")
FENCE_CLOSE = re.compile(r"^```\s*$")

VALID_LANGS = {
    "bash", "sh", "shell", "zsh", "python", "py", "js", "javascript",
    "json", "yaml", "sql", "go", "rust", "java", "c", "cpp", "text",
}


class FenceScanner:
    """
    从**可能不完整的**文本里切出围栏代码块。

    与 dsmod 的 extractBlocks 同思路：必须是状态机而非正则，
    因为流式到达时文本可能停在围栏中间。
    区别在于我们只处理"已完整闭合"的块，未闭合的留在缓冲区等下一批 delta。
    """

    def __init__(self, langs=None):
        self.langs = set(langs or VALID_LANGS)
        self.buf = ""
        self.in_block = False
        self.lang = ""
        self.body = []

    def feed(self, delta):
        """
        喂入一段增量，返回本次新产生的完整块列表 [(lang, code)]。
        """
        self.buf += delta
        out = []
        # 按行处理，最后一行可能不完整，留在 buf 里
        lines = self.buf.split("\n")
        self.buf = lines[-1]                     # 未以 \n 结尾的残行
        for line in lines[:-1]:
            r = self._consume(line)
            if r:
                out.append(r)
        return out

    def flush(self):
        """流结束时调用：处理残留。未闭合的块返回 None（不完整，丢弃）。"""
        if self.buf:
            r = self._consume(self.buf)
            self.buf = ""
            if r:
                return [r]
        return []

    def _consume(self, line):
        if not self.in_block:
            m = FENCE_OPEN.match(line)
            if m:
                lang = (m.group("lang") or "").lower()
                if not lang or lang in self.langs:
                    self.in_block = True
                    self.lang = lang
                    self.body = []
            return None
        if FENCE_CLOSE.match(line):
            code = "\n".join(self.body)
            lang, self.lang, self.in_block, self.body = self.lang, "", False, []
            return (lang, code)
        self.body.append(line)
        return None


class IdleGate:
    """
    空闲判定：多久没有新内容就认为"这一段落结束了"。

    对应 dsmod 的 IDLE_MS = 1500。用途：
      · 流式渲染时决定何时换行分段
      · 围栏迟迟不闭合时，决定是继续等还是判为未闭合
    """

    def __init__(self, idle_ms=1500):
        self.idle_ms = idle_ms
        self.last = None

    def touch(self):
        self.last = time.time()

    def reset(self):
        self.last = None

    def idle_ms_now(self):
        if self.last is None:
            return 0.0
        return (time.time() - self.last) * 1000

    def is_idle(self):
        return self.last is not None and self.idle_ms_now() >= self.idle_ms


class StreamRenderer:
    """
    把流式 delta 渲染到终端。

    · 内容直接写出，不缓冲（要的就是实时）
    ·  reasoned content（思考过程）用暗色前缀区分
    · 统计字符数，结束时给出一行摘要
    """

    def __init__(self, show_reasoning=True, stream=None):
        self.show_reasoning = show_reasoning
        self.out = stream or sys.stdout
        self.chars = 0
        self.reason_chars = 0
        self.started = False
        self.in_reason = False

    def _w(self, s):
        self.out.write(s)
        try:
            self.out.flush()
        except Exception:
            pass

    def feed(self, delta, kind="content"):
        if not delta:
            return
        if kind == "reasoning" and not self.show_reasoning:
            self.reason_chars += len(delta)
            return

        if not self.started:
            self._w("\n")
            self.started = True

        if kind == "reasoning":
            if not self.in_reason:
                self._w("  ·····思考·····\n  ")
                self.in_reason = True
            self._w(delta)
            self.reason_chars += len(delta)
        else:
            if self.in_reason:
                self._w("\n  ·············\n")
                self.in_reason = False
            self._w(delta)
            self.chars += len(delta)

    def finish(self):
        if self.in_reason:
            self._w("\n  ·············\n")
            self.in_reason = False
        if self.started:
            total = self.chars + self.reason_chars
            self._w(f"\n  [流式结束] 共 {total} 字符"
                    + (f"（含思考 {self.reason_chars}）" if self.reason_chars else "")
                    + "\n")
        self.started = False


def iter_stream(resp):
    """
    把 OpenAI 流式响应适配成事件迭代器。

    产出 (kind, payload)：
      · ("content", str)    正文增量
      · ("reasoning", str)  思考过程增量（思考模式）
      · ("tool_call", dict) 工具调用增量：
          {"index": int, "id": str|None, "name": str|None, "args": str}
    """
    for chunk in resp:
        if not getattr(chunk, "choices", None):
            continue
        ch = chunk.choices[0]
        d = getattr(ch, "delta", None)
        if d is None:
            continue

        rc = getattr(d, "reasoning_content", None)
        if rc:
            yield "reasoning", rc

        c = getattr(d, "content", None)
        if c:
            yield "content", c

        tcs = getattr(d, "tool_calls", None)
        if tcs:
            for tc in tcs:
                fn = getattr(tc, "function", None)
                # 后续分片通常只有 arguments、name 为 None —— 不能因此丢掉参数
                args = ""
                if fn is not None:
                    args = getattr(fn, "arguments", "") or ""
                else:
                    args = getattr(tc, "arguments", "") or ""
                yield "tool_call", {
                    "index": getattr(tc, "index", 0) or 0,
                    "id": getattr(tc, "id", None),
                    "name": (getattr(fn, "name", None) if fn else None) or "",
                    "args": args,
                }


class ToolCallAccumulator:
    """
    把流式到达的工具调用片段拼成完整调用。

    JSON 参数会被切成很多片（可能从字符串中间断开），
    所以必须累积到流结束再整体解析——和围栏不能闭合是同一类问题。
    """

    def __init__(self):
        self.parts = {}

    def feed(self, ev):
        i = ev["index"]
        p = self.parts.setdefault(i, {"id": "", "name": "", "args": ""})
        if ev.get("id"):
            p["id"] = ev["id"]
        if ev.get("name"):
            p["name"] += ev["name"]     # 只有首片带 name，后续为 None
        if ev.get("args"):
            p["args"] += ev["args"]

    def build(self):
        """产出与 OpenAI SDK 同构的 tool_calls 列表。"""
        import types
        out = []
        for i in sorted(self.parts):
            p = self.parts[i]
            if not p["name"]:
                continue
            f = types.SimpleNamespace()
            f.name = p["name"]
            f.arguments = p["args"] or "{}"
            t = types.SimpleNamespace()
            t.id = p["id"] or f"call_{i}"
            t.function = f
            out.append(t)
        return out or None


if __name__ == "__main__":
    # 自检：模拟流式到达，验证围栏在"卡在中间"时不会被误判
    print("=== FenceScanner 流式压力测试 ===")
    sc = FenceScanner()
    full = "前面文字\n```bash\necho hello\nls -la\n```\n中间\n```python\nprint(1)\n```\n后面\n"
    got = []
    for i in range(0, len(full), 7):          # 每次喂 7 字节，故意切碎
        got += sc.feed(full[i:i + 7])
    got += sc.flush()
    for lang, code in got:
        print(f"  lang={lang!r} code={code!r}")
    assert len(got) == 2, f"应切出 2 个块，实际 {len(got)}"
    assert got[0][0] == "bash" and "ls -la" in got[0][1]
    assert got[1][0] == "python"

    print("\n=== 未闭合块不应产出 ===")
    sc2 = FenceScanner()
    r = sc2.feed("```bash\necho x\n")
    print("  未闭合时产出:", r, "| 应为 []")
    assert r == []
    print("   flush 后（仍不完整）:", sc2.flush())

    print("\n=== IdleGate ===")
    g = IdleGate(idle_ms=100)
    g.touch()
    print("  刚 touch 是否 idle:", g.is_idle())
    time.sleep(0.15)
    print("  150ms 后是否 idle:", g.is_idle())
    print("\n✅ 全部通过")
