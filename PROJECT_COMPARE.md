# 项目级对比：dsmod-* ↔ ds-agent

对照 `liuben3223380369-ops/dsmod-*` 三个仓库与本项目 `ds-agent`。
先说结论：**这是两类完全不同的东西**，一个逆向研究资料，一个可直接运行的产品；
真正的可比之处在"AI 自动写代码并执行"这条链路的工程实现上。

---

## 一、定位差异（根本性的）

| | dsmod-* | ds-agent |
|---|---|---|
| 性质 | 逆向研究资料 + 环境增强脚本 | 可部署运行的 Agent 系统 |
| 主要产物 | 109 个类的 smali 逐字节原文、分析文档、`setup-all.sh` | 8 个 Python 模块 + CI + 一键部署脚本 |
| 能不能直接跑 | 不能。它是**对别人 APK 的分析记录**，APK 本身不在仓库里 | 能。`sh setup.sh` 后即可执行任务 |
| 运行环境 | 寄生在 `com.dsmod.deepseek` 2.5.2 App 内部（Android） | 独立进程，Termux / Linux / macOS / WSL |
| 接模型的方式 | 反射桥捕获宿主 ViewModel，注入提示词走宿主发送通道 | 官方 API（OpenAI 兼容格式） |
| 代码规模 | 分析对象 1.6 万行级（smali），脚本约数百行 | 2342 行 Python（不含自检） |
| 合规边界 | 自称"仅限自建靶场学习与自研参考" | MIT，无第三方 App 依赖 |

**一句话**：dsmod 是在**拆解别人的实现**，ds-agent 是**自己实现一个**。
dsmod 里那些精巧的手法（反射桥、围栏解析）之所以存在，是因为它要在**不重新编译宿主**的前提下
往 App 里塞功能——这个约束我们完全没有。

---

## 二、核心链路：差异最大的地方

原项目 `docs/ANALYSIS.md` 自己总结过一句很关键的话：

> 920 行（handleReply 716 + extractBlocks 219）处理「文本→指令」，
> 执行本身 378 行（runShell）。**工程重心在解析与收敛，不在调用模型。**

这个重心在我们这里**整个消失了**。

| 环节 | dsmod 的做法 | ds-agent 的做法 |
|---|---|---|
| 提示词约定 | `TEACH`/`NUDGE` 常量写死三种围栏：`bash/sh`、`mcp`、`skill` | 几句自然语言 + OpenAI tools schema |
| 解析 | `extractBlocks` **219 行手写状态机**扫描围栏 | **0 行**——模型直接吐结构化 `tool_calls` |
| 为什么手写 | 流式 delta 到达时代码块可能未闭合，正则不可靠 | 不需要——JSON 由 SDK 保证完整 |
| 去重 | `EXECED` ConcurrentHashMap | 命令+参数的 MD5 哈希集合 |
| 防重入 | `PENDING` ConcurrentHashMap | `threading.Lock` |
| 轮次 | `MAX_EXEC = 0x3c = 60` | `DS_ROUNDS = 20` |
| 看门狗 | `WATCHDOG` 15s + `IDLE_MS` 1.5s 定时 auto-continue | 墙钟看门狗 `DS_WALL = 900s` |
| 输出截断 | `MAX_TEXT = 200000`，`appendText` 打补丁在头部 delete 并插标记 | 分档截断并明确打印"截断 N 字符" |

原因很直接：**DeepSeek V4 原生支持 OpenAI 格式 function calling**（1M 上下文 / 384K 输出，
思考模式下也能调工具）。它没有这个能力，只能靠提示词约定 + 手写状态机硬解析。

我们省掉的不只是 219 行代码，还有一整类**解析边界 bug**（围栏未闭合、嵌套代码块、
markdown 里恰好出现三个反引号……）。

---

## 三、执行环境

| | dsmod (PRoot) | ds-agent (Docker) |
|---|---|---|
| 隔离强度 | 假 root（`uid=0` 但 `CapEff=0`） | 真隔离：`--network=none --memory=512m --pids-limit=128` |
| 网络 | 可用 | 默认**完全断网**，装包时才开 `bridge` |
| ptrace | 不透传 → frida/strace/gdb attach 全废 | 容器内可用（受限） |
| 文件系统 | `-b` 绑 `/sdcard /storage /dev /proc /sys` | `-v` 挂载，只读模式一律 `:ro` |
| 降级 | 容器不可用回退系统 sh | 同，且明确打印警告 |
| 无 Docker 时 | — | 自动回退本地执行，**并明确告知无隔离** |

PRoot 的 `CapEff=0` 是内核 capability 不透传导致的**硬限制**，不是配置问题——
原项目 README 里列了一整张"不可用"清单（frida-server、strace、gdb、iptables、
内核模块、完整进程列表）。这是诚实且有价值的信息，我们沿用了这个写法。

---

## 四、终端（已在 COMPARE.md 详述，此处摘要）

| dsmod `TermEnv.execOnce` | ds-agent `terminal.py` |
|---|---|
| 每次调用 `new ProcessBuilder` | 长驻 shell（Docker 模式长驻容器 + `docker exec`） |
| **cd / export 全部丢失**（它自己注明"故提示词要求绝对路径"） | cwd 与环境变量跨调用保留，写入 `.dsenv` 崩溃可恢复 |
| 超时检查在 `read()` 循环内 → **无输出挂死杀不掉** | `killpg` 杀进程组，4s 精确熔断 |
| `destroy()` 后 `waitFor()` 无超时，可永久阻塞 | 无此路径 |
| `redirectErrorStream(true)` → stderr 混入 stdout | 分离到 `[stderr]` 段 |
| 超 400000 字符**静默丢弃** | 明确标注截断量 |

实测：120 次混合命令（含 40 次 `exit`）**0 失败**；`sleep 30` + timeout=4 → 4.0s 熔断并重建。

---

## 五、ds-agent 有而 dsmod 没有的

这些不是"我们更好"，而是**目标不同导致的需求不同**——我们要交付一个能自己跑完的东西。

**1. Git 快照回滚**
每步自动 commit，`--rollback N` 回退。模型自己也带 `git_rollback` 工具。
原项目不需要——它的产物是 smali 文件，本来就在 git 里。

**2. 调试顾问（`debug_advisor.py`）**
双指纹（错误指纹不含行号、修复指纹统计换过几种修法），
按「重复次数 × 修法种类」分档升级建议。原项目只有"60 次硬上限"，
不会区分"小修没到位"和"思路本身错了"。

**3. 主动介入（`intervene.py`）**
卡住就暂停，生成诊断报告，请人决策（继续/回滚/手动接管/给提示/放弃）。
**手动接管是真起 shell 让人改**，`exit` 后把 diff 同步给模型。
原项目是 App 内的后台行为，没有"停下来问人"这个概念。

**4. 架构知识按需注入（`arch_cards.py`）**
普通任务注入 **0 字符**——常驻反而诱发过度设计。只有任务确实涉及模型/训练才加载，
且内容是工程坑位（"什么时候不该用"）而非架构科普。

**5. 持久会话的状态可观测**
每次返回带 `[环境]` 头（容器/裸机/uid/镜像/网络/内存）和 `[cwd]`。
学自原项目的设计（它有 `[环境] Alpine 容器 · PRoot 沙箱 · 容器内 uid=0`），但信息更全。

**6. CI + 本地自检**
9 组断言，Python 3.10/3.12。`selftest.py` 断言与 CI 完全一致，push 前跑一遍。
原项目有 `verify.sh`，但那是**环境自检**（检查 jadx 装没装），不是代码测试。

**7. 一键部署**
`setup.sh` 覆盖 Termux / Linux / macOS / WSL，pip 受限时自动建 `.venv`。
原项目是 `setup-all.sh` 装 Alpine 包，面向的是容器内部。

---

## 六、dsmod 有而我们没有的（诚实清单）

**1. 环境完备性 —— 这是最大差距**

`dsmod-full-stack` 用 `setup-all.sh` 幂等装 **305 个 Alpine 包**：

- 20+ 种语言运行时：Python3.12 / Node20 / Go1.22 / Rust1.78 / Java17 / Kotlin2.0 /
  PHP8.3 / Ruby3.3 / Perl5.38 / Lua5.4 / Elixir / Erlang / R4.4 / .NET8 / GHC9.8 …
- 逆向工具链：jadx 1.5、apktool 2.9.3、aapt2、zipalign、dexdump、r8/d8、
  uber-apk-signer、apkid 3.2
- 数据库：SQLite / PostgreSQL client / MariaDB client / Redis / Nginx

我们只有 `python:3.12-slim` 一个镜像。策略不同：**用镜像切换替代全量预装**
（`DS_IMAGE=python:3.12-bookworm` 换带 gcc 的），代价是每次切镜像要重新拉。

**2. MCP 的 HTTP 传输通道**

原项目 `Mcp.smali` 2370 行，**HTTP 和 stdio 双通道**：
`httpRequest`(L614) / `httpPost`(L425) / `stdioRequest`(L2007)。
我们只有 stdio（`mcp_client.py` 166 行，纯标准库）。

影响：接**远端** MCP 服务（SSE / HTTP 端点）目前做不到，只能接本地进程。
stdio 是本地 server 的主流，但这确实是个能力缺口。

**3. 流式 delta 处理**

`IDLE_MS = 1500` 是"流式输出空闲多久判定代码块已闭合"的 debounce，
配合手写状态机处理**边生成边解析**。我们走 API 的结构化 `tool_calls`，
不需要这个——但代价是**无法做流式实时显示**（必须等整条响应返回）。

**4. 反射注入技术**

`Exp.captureVm`(L1244) 反射捕获宿主 ViewModel/ChatManager，
`injectPrompt`(L2710) 在发送前插提示词，改造层与宿主**无编译期依赖**。
这套"插件化改造第三方 App"的手法通用价值很高，但我们需要它——我们就是宿主。

**5. skill 围栏类型**

原项目支持三种围栏：`bash/sh`、`mcp`、`skill`。我们只有工具调用，
没有独立的 skill 抽象（能力被并进了工具集）。

---

## 七、工程重心对比（各自 README/分析文档的自我总结）

| | dsmod | ds-agent |
|---|---|---|
| 重心 | 解析与收敛（920 行 vs 执行 378 行） | 调试质量与可靠性 |
| 主要风险 | 围栏解析边界、收敛失控 | 模型原地打转、烧 token |
| 应对 | 硬上限 + 四个 ConcurrentHashMap | 双指纹诊断 + 主动介入 + 墙钟看门狗 |

原项目的风险是"**解析不准**"，我们的风险是"**一直错还继续跑**"。
这决定了两套完全不同的防护设计。

---

## 八、可以互相借鉴的

**从 dsmod 学到并已吸收的**

1. **环境头**：每条命令输出前先说明自己在什么环境（容器/裸机/uid），
   避免模型误以为有 root 而写出跑不了的命令。
2. **降级透明**：容器不可用时明确回退并告知，不静默降级。
3. **proot 绑定参数思路**：`-w` 指定工作目录、PATH 追加工具链目录、绑定 `/sdcard`——
   对应我们的 `mounts` 外链挂载。
4. **"不可用清单"的写法**：明确列出硬限制并说明是内核限制而非配置问题，
   避免使用者白折腾。我们把它用在了 Termux 说明里（`CapEff=0` → frida 不可用）。
5. **命令 wrapper 落地方式**：原项目把工具做成 `/usr/local/bin/` 下的 sh wrapper，
   不依赖 PATH 是否加载 profile。我们的 `setup.sh` 把 server 路径改写为绝对路径，
   是同一个思路。

**dsmod 若要做成产品，我们这边可反哺的**

1. function calling 替代围栏解析（省掉 219 行 + 一整类 bug）
2. 持久终端（解决 cd/export 丢失）
3. git 快照 + 主动介入（解决"卡住还硬跑"）
4. Docker 真隔离替代 PRoot 假 root

---

## 九、边界声明

- 本对比基于 dsmod 三个仓库的公开内容（README、`docs/ANALYSIS.md`、
  `docs/key-methods/` 下 8 个 smali 及常量）。**未运行过其 APK**，
  所有关于运行时行为的判断来自 smali 静态分析。
- dsmod 仓库自称"仅限自建靶场学习与自研参考"。本文只做工程实现对比，
  不涉及、也不评价其分发或使用改造后 APK 的合规性。
- "实测数据"均指 ds-agent 在本沙盒环境的验证结果（120 次终端压测、
  9 组 CI 断言等），与 dsmod 无对照实验——后者无法在此环境运行。
