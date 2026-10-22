#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主动介入 —— 卡住时停下来问人，而不是烧 token 干等。

朴素循环的问题：模型卡住后仍会跑满 20 轮，每轮都在烧钱且几乎必然失败。
与其让它硬撑，不如在"已可判定为卡住"时暂停，把现场交给用户。

触发条件（任一满足）：
  · 连续 N 轮失败（默认 3，DS_ITER_STALL）
  · 同一错误累计 M 次（默认 4，DS_ITER_SAME）
  · 墙钟超过 X 秒且仍未成功（默认 240，DS_ITER_TIME）

暂停后做什么：
  1. 生成诊断报告落盘（diagnostics/report-*.md）
  2. 在终端打印精简版
  3. 请用户决策：继续 / 回滚重来 / 手动接管 / 给提示 / 放弃

非交互环境（CI、管道、nohup）自动跳过询问，按 DS_ON_STUCK 处理。
"""

import os
import sys
import time
import json
import shutil
import subprocess
from pathlib import Path

# 决策常量
CONTINUE = "continue"      # 继续让模型跑
ROLLBACK = "rollback"      # 回滚 N 步后继续
MANUAL = "manual"          # 交给人，手动修完再回来
HINT = "hint"              # 带一句人的提示继续
ABORT = "abort"            # 放弃

CHOICES = {
    "1": CONTINUE, "2": ROLLBACK, "3": MANUAL, "4": HINT, "5": ABORT,
    "c": CONTINUE, "r": ROLLBACK, "m": MANUAL, "h": HINT, "a": ABORT,
}


class Intervention:
    def __init__(self, root, snap=None, sandbox=None, advisor=None,
                 stall=3, same=4, budget=240, enabled=True):
        self.root = Path(root).resolve()
        self.snap = snap
        self.sandbox = sandbox
        self.advisor = advisor
        self.stall_limit = stall
        self.same_limit = same
        self.time_limit = budget
        self.enabled = enabled

        self.t0 = time.time()
        self.fail_streak = 0       # 连续失败轮数
        self.round_no = 0
        self.intervened = 0        # 已介入次数
        self.notes = []            # 人工提示，会注入给模型
        self.report_dir = self.root / "diagnostics"
        self.last_report = None

    # ---------------- 状态跟踪 ----------------
    def tick(self, ok):
        """每轮结束调用。ok=True 表示本轮有实质进展。"""
        self.round_no += 1
        if ok:
            self.fail_streak = 0
        else:
            self.fail_streak += 1

    def _same_error_count(self):
        if not self.advisor or not getattr(self.advisor, "errors", None):
            return 0
        return max(self.advisor.errors.values())

    def should_intervene(self):
        """判断是否该停下来问人。"""
        if not self.enabled:
            return None
        if self.fail_streak >= self.stall_limit:
            return f"连续 {self.fail_streak} 轮失败"
        n = self._same_error_count()
        if n >= self.same_limit:
            return f"同一错误累计 {n} 次"
        el = time.time() - self.t0
        if el >= self.time_limit and self.fail_streak > 0:
            return f"已用 {int(el)}s 且仍在失败"
        return None

    # ---------------- 报告生成 ----------------
    def _git(self, args, timeout=10):
        try:
            p = subprocess.run(f"git {args}", shell=True, cwd=self.root,
                               capture_output=True, text=True, timeout=timeout)
            return (p.stdout or "").strip()
        except Exception:
            return ""

    def build_report(self, task, reason):
        el = time.time() - self.t0
        L = []
        L.append(f"# 卡住诊断报告\n")
        L.append(f"- **任务**：{task}")
        L.append(f"- **触发原因**：{reason}")
        L.append(f"- **已用**：{int(el)}s / {self.round_no} 轮（介入 {self.intervened + 1} 次）")
        if self.sandbox:
            L.append(f"- **环境**：{self.sandbox.mode}"
                     + (f" · cwd={self.sandbox.cwd}" if getattr(self.sandbox, "cwd", None) else ""))
        L.append("")

        # 错误时间线
        if self.advisor and getattr(self.advisor, "history", None):
            L.append("## 错误时间线\n")
            L.append("| 轮次 | 类型 | 异常 |")
            L.append("|---|---|---|")
            for rnd, cat, cls in self.advisor.history[-15:]:
                L.append(f"| {rnd} | {cat} | {cls or '—'} |")
            if self.advisor.errors:
                top = max(self.advisor.errors.values())
                L.append(f"\n同一错误最多重复 **{top}** 次"
                         f"（阈值 {self.same_limit}）。")
            L.append("")

        # 最近改动
        stat = self._git("diff --stat HEAD~1 HEAD")
        if not stat:
            stat = self._git("diff --stat")
        if stat:
            L.append("## 最近改动\n```\n" + stat + "\n```\n")

        files = self._git("ls-files --others --exclude-standard")
        if files:
            L.append("## 新增未跟踪文件\n```\n" + files[:1500] + "\n```\n")

        # 快照，方便直接回退
        log = self._git("log --oneline -8")
        if log:
            L.append("## 可用快照（--rollback N）\n```\n" + log + "\n```\n")

        L.append("## 可选的下一步\n")
        L.append("1. **继续** —— 让模型再试（适合它其实在收敛，只是慢）")
        L.append("2. **回滚重来** —— 回到上一个能跑的版本，换条路")
        L.append("3. **手动接管** —— 你自己进 shell 修，修完回来")
        L.append("4. **给提示** —— 你写一句方向性提示，模型带着它继续")
        L.append("5. **放弃** —— 停在这里，保留现场")
        return "\n".join(L)

    def save_report(self, text):
        """落盘。同一秒内多次介入会重名，这里自动加序号避免覆盖。"""
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            base = time.strftime("report-%Y%m%d-%H%M%S")
            p = self.report_dir / f"{base}.md"
            i = 2
            while p.exists():          # 同秒多次介入，别互相覆盖
                p = self.report_dir / f"{base}-{i}.md"
                i += 1
            p.write_text(text, encoding="utf-8")
            self.last_report = p
            return p
        except Exception:
            return None

    # ---------------- 交互 ----------------
    def _interactive(self):
        """能拿到 TTY 就问，否则返回 None。"""
        try:
            return sys.stdin.isatty() and sys.stdout.isatty()
        except Exception:
            return False

    def ask(self, task, reason):
        """
        停下来问人。返回 (决策, 附加数据)。
        非交互环境按 DS_ON_STUCK 自动处理。
        """
        self.intervened += 1
        report = self.build_report(task, reason)
        path = self.save_report(report)

        # 终端打印精简版，报告全文落盘
        print("\n" + "=" * 56)
        print(f"⚠️  卡住了：{reason}")
        print("=" * 56)
        if self.advisor:
            print(f"   {self.advisor.summary()}")
        if path:
            print(f"   完整报告：{path}")

        # 非交互：按环境变量决定
        if not self._interactive():
            policy = os.environ.get("DS_ON_STUCK", "continue").lower()
            print(f"   非交互环境，按 DS_ON_STUCK={policy} 处理")
            if policy == "abort":
                return ABORT, None
            if policy == "rollback":
                return ROLLBACK, 1
            if policy == "hint":
                return HINT, os.environ.get("DS_HINT", "请换一条实现思路")
            return CONTINUE, None

        print("\n  1) 继续      让模型再试")
        print("  2) 回滚重来  回到上一个能跑的版本")
        print("  3) 手动接管  你自己进 shell 修，exit 后回来")
        print("  4) 给提示    写一句方向性提示再继续")
        print("  5) 放弃      停在这里保留现场")

        while True:
            try:
                raw = input("\n  选择 [1-5]（默认 1）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  （输入中断，按继续处理）")
                return CONTINUE, None

            if raw == "":
                return CONTINUE, None
            if raw.lower() in ("3", "m", "manual"):
                return MANUAL, None
            if raw.lower() in ("4", "h", "hint"):
                try:
                    tip = input("  请输入提示: ").strip()
                except (EOFError, KeyboardInterrupt):
                    tip = ""
                return HINT, tip or "请换一条实现思路"
            if raw.lower() in ("2", "r", "rollback"):
                steps = self._ask_steps()
                return ROLLBACK, steps
            if raw.lower() in ("5", "a", "abort"):
                return ABORT, None
            if raw.lower() in ("1", "c", "continue"):
                return CONTINUE, None
            print("  未识别，请输入 1-5")

    def _ask_steps(self):
        while True:
            try:
                raw = input("  回退几步？(默认 1): ").strip()
            except (EOFError, KeyboardInterrupt):
                return 1
            if raw == "":
                return 1
            try:
                n = int(raw)
                return max(1, n)
            except ValueError:
                print("  请输入数字")

    # ---------------- 手动接管 ----------------
    def manual_session(self):
        """
        把终端交给用户。用户在真实 shell 里自己修，
        exit 之后把改动同步给模型继续。
        """
        cwd = getattr(self.sandbox, "cwd", None) or str(self.root)
        if not os.path.isdir(cwd):
            cwd = str(self.root)
        print("\n" + "-" * 56)
        print("已进入手动接管。你在这个 shell 里的改动会保留。")
        print(f"目录：{cwd}")
        print("修完输入 exit 回到 Agent。")
        print("-" * 56 + "\n")

        shell = os.environ.get("SHELL") or ("/bin/bash" if shutil.which("bash") else "/bin/sh")
        before = self._git("rev-parse HEAD")
        try:
            subprocess.run([shell], cwd=cwd)
        except Exception as e:
            print(f"[接管异常] {type(e).__name__}: {e}")
        after = self._git("rev-parse HEAD")

        changed = self._git("status --porcelain")
        diff = self._git("diff --stat") or "(无未提交改动)"

        summary = []
        if changed:
            summary.append("你在手动接管期间的改动：")
            summary.append(diff)
        else:
            summary.append("（手动接管期间未检测到文件改动）")
        if before != after:
            summary.append(f"HEAD 变化：{before[:8]} → {after[:8]}")

        print("\n" + "-" * 56)
        print("已交回 Agent。同步给模型的信息：")
        for s in summary:
            print("  " + s)
        print("-" * 56 + "\n")

        # 人工改动后重置失败计数，给它一个干净的重新评估
        self.fail_streak = 0
        return "\n".join(summary)

    # ---------------- 给模型的注入文本 ----------------
    def drain_notes(self):
        """取出并清空人工提示，注入进下一轮消息。"""
        if not self.notes:
            return ""
        text = "\n".join(f"- {n}" for n in self.notes)
        self.notes.clear()
        return "\n【来自用户的提示】\n" + text + "\n请先按用户的思路调整，再继续。"

    def add_note(self, text):
        if text:
            self.notes.append(text)
