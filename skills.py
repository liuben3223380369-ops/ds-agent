#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skill 抽象层 —— 对齐 dsmod 的第三种围栏类型。

dsmod 支持三种围栏：bash/sh、mcp、skill。其中 skill 是**可复用的能力包**，
与一次性工具调用并列。我们此前只有工具，缺这一层。

设计取向（避免做成花架子）：
  · Skill = 一段**带参数的提示词模板** + 可选的**执行步骤**
  · 它不是一个"新工具"，而是把"怎么做某类事"的know-how固化下来
  · 加载后并入系统提示，模型看到的是"可用的方法"，不是"能调的函数"

目录约定：
    skills/*.md     每个文件一个 skill，用 YAML front matter 声明元信息

格式：
    ---
    name: tdd
    description: 测试驱动开发流程
    trigger: 写代码|实现|重构
    ---
    正文即给模型的指令……

纯标准库，不引入 PyYAML（front matter 只解析 k: v 与列表）。
"""

import os
import re
import sys
import json
from pathlib import Path

FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


class Skill:
    def __init__(self, path, meta, body):
        self.path = Path(path)
        self.name = meta.get("name") or self.path.stem
        self.description = meta.get("description", "")
        self.trigger = meta.get("trigger", "")
        self.tags = meta.get("tags", [])
        self.body = body.strip()

    def matches(self, task):
        """按 trigger 关键词判断是否该加载。空 trigger = 始终可用。"""
        if not self.trigger:
            return True
        t = (task or "").lower()
        for kw in re.split(r"[|,、\s]+", self.trigger):
            kw = kw.strip().lower()
            if kw and kw in t:
                return True
        return False

    def render(self):
        head = f"### skill: {self.name}"
        if self.description:
            head += f" — {self.description}"
        return f"{head}\n{self.body}\n"

    def __repr__(self):
        return f"<Skill {self.name}>"


def parse_front_matter(text):
    """
    极简 YAML 子集解析：支持 `k: v`、`k: [a, b]`、`- item` 列表。
    刻意不引 PyYAML —— 保持零依赖。
    """
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta, body = {}, text[m.end():]
    key = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if key:
                meta.setdefault(key, [])
                if isinstance(meta[key], list):
                    meta[key].append(line.strip()[2:].strip())
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if v.startswith("[") and v.endswith("]"):
                meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            elif v == "":
                meta[k] = []          # 可能是接下来的列表
                key = k
            else:
                meta[k] = v.strip("'\"")
                key = None
    return meta, body


def load_dir(directory):
    """加载目录下所有 .md skill。目录不存在返回空列表。"""
    d = Path(directory)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        meta, body = parse_front_matter(text)
        out.append(Skill(p, meta, body))
    return out


class SkillBook:
    def __init__(self, directory=None):
        self.dir = Path(directory) if directory else Path(__file__).parent / "skills"
        self.skills = load_dir(self.dir)

    def select(self, task, limit=2):
        """按 trigger 挑出最相关的 skill。最多 limit 个，避免提示词膨胀。"""
        hits = [s for s in self.skills if s.matches(task)]
        return hits[:limit]

    def render(self, task, limit=2):
        picked = self.select(task, limit)
        if not picked:
            return ""
        parts = ["\n【可用方法】以下是与当前任务相关的既有方法，优先按它执行：\n"]
        for s in picked:
            parts.append(s.render())
        return "\n".join(parts)

    def names(self):
        return [s.name for s in self.skills]

    def as_tools(self):
        """
        另一种暴露方式：把 skill 变成"列出可用方法"的工具结果。
        模型看不到全文，但可以请求加载某个 skill。
        """
        return [{"name": s.name, "description": s.description,
                 "trigger": s.trigger} for s in self.skills]


# ---------------- 内置 skill 模板（首次运行自动落盘） ----------------
BUILTIN = {
    "tdd.md": """---
name: tdd
description: 测试驱动开发 —— 先写失败的测试，再写让它通过的最小实现
trigger: 写代码|实现|函数|重构|修复bug
tags: [流程, 质量]
---

按这个顺序走，不要跳步：

1. **先写测试**：明确输入、输出、边界条件。测试必须能跑，且**当前是失败的**。
2. **跑一次确认它失败**：如果测试一开始就通过，说明测试写错了或功能已存在。
3. **写最小实现**：只写让测试通过的代码，不要顺手加功能。
4. **跑测试确认通过**。
5. **重构**：在测试保护下整理代码，每改一次跑一次。

禁止：
· 先写实现再补测试（那样测试会迁就实现，失去验证价值）
· 一次改多处再跑（失败时无法归因）
· 用 `try/except: pass` 让测试"看起来通过"

完成标准：所有测试通过 + 你能量化说明覆盖了哪些边界。
""",
    "debug.md": """---
name: debug
description: 系统化调试 —— 先定位再修改，禁止盲目试错
trigger: 报错|失败|不对|bug|异常|调试
tags: [流程, 调试]
---

调试的顺序（按性价比）：

1. **复现**：找到稳定复现的最小输入。不能稳定复现就不要开始改。
2. **定位**：用打印/断点确认**第一次**偏离预期的位置，而不是看报错在哪一行——
   报错位置往往只是"受害者"。
3. **提出假设**：明确写出"我认为原因是 X"。
4. **验证假设**：设计一个能**否证**它的检查。只做能证伪的实验。
5. **修改**：假设被证实后再改，且一次只改一处。
6. **回归**：确认问题消失且没引入新问题。

禁止：
· 没读完整 traceback 就改代码
· 同一个位置连续微调超过 3 次（说明假设错了，回到第 3 步）
· 用 try/except 吞掉异常让报错消失（问题还在，只是看不见了）

如果连续 3 种修法都无效：**停下来**，写一个 10 行以内的最小复现。
""",
    "review.md": """---
name: review
description: 代码自查 —— 交付前过一遍清单
trigger: review|审查|检查代码|提交前
tags: [质量]
---

交付前逐项确认：

**正确性**
· 边界：空输入、0、负数、超长、重复值
· 异常路径：文件不存在、网络失败、权限不足是否都有处理
· 有没有 off-by-one

**安全**
· 路径拼接是否可能被 `../` 穿越
· 用户输入是否直接进了 shell / SQL / eval
· 有没有硬编码密钥

**可维护**
· 函数是否只做一件事
· 命名是否表达了意图（不是 `data`、`tmp`、`res`）
· 有没有重复的魔法数字

**验证充分性**
· 有没有实际跑过，而不是"看起来对"
· 测试是否覆盖了失败路径，不只测 happy path

发现问题时：**先说问题，再给修改**，不要默默改掉。
""",
}


def ensure_builtin(directory=None):
    """首次运行时把内置 skill 落盘，用户可直接编辑。"""
    d = Path(directory) if directory else Path(__file__).parent / "skills"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    written = []
    for name, content in BUILTIN.items():
        p = d / name
        if not p.exists():
            try:
                p.write_text(content, encoding="utf-8")
                written.append(name)
            except Exception:
                pass
    return written


if __name__ == "__main__":
    here = Path(__file__).parent
    ensure_builtin(here / "skills")
    bk = SkillBook(here / "skills")
    print("已加载 skill:", bk.names())
    for t in ["帮我写个函数", "这个报错怎么修", "写个快排"]:
        print(f"\n任务「{t}」命中:", [s.name for s in bk.select(t)])
