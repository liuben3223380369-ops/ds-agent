# ds-agent

DeepSeek 驱动的「自动写代码 → 运行 → 检查 → 自我修复」完整闭环。

仓库：<https://github.com/liuben3223380369-ops/ds-agent>

## 一键部署（推荐）

适用于 Termux(Android) / Linux / macOS / WSL，幂等可重复执行：

```bash
curl -fsSL https://raw.githubusercontent.com/liuben3223380369-ops/ds-agent/main/setup.sh | sh
```

或手动：

```bash
git clone https://github.com/liuben3223380369-ops/ds-agent.git
cd ds-agent && sh setup.sh
```

脚本会自动：装 Python 依赖（系统 pip 受限时自动建 `.venv`）、生成 `mcp.json`
（server 路径改写为绝对路径，换目录也不会失效）、建 `workspace/`、输出下一步指引。

后续更新：

```bash
cd ds-agent && git pull
```

## 快速开始

```bash
pip install openai
export DEEPSEEK_API_KEY="sk-xxx"
python3 ds_agent_pro.py "写一个快速排序并验证正确性"
```

首次运行会自动生成 `mcp.json`（指向内置 demo server）。

## 环境装 Docker（强烈建议）

```bash
docker pull python:3.12-slim
```

没有 Docker 也能跑，会**自动回退本地执行**并在启动时警告。本地模式 = 你的真实环境，跑不信任代码等于裸奔。

## 十三项能力

### 0. 主动介入（卡住就停下来问人）

朴素循环卡住后仍会跑满 20 轮，每轮都在烧钱且几乎必然失败。
`intervene.py` 在"已可判定为卡住"时暂停，把现场交给你。

触发条件（任一）：

| 条件 | 默认 | 环境变量 |
|---|---|---|
| 连续失败轮数 | 3 | `DS_ITER_STALL` |
| 同一错误累计 | 4 | `DS_ITER_SAME` |
| 已用时长且仍在失败 | 240s | `DS_ITER_TIME` |

暂停后生成诊断报告落盘到 `diagnostics/report-*.md`（任务、错误时间线、
最近改动、可用快照），终端打印精简版，然后请你决策：

```
1) 继续      让模型再试
2) 回滚重来  回到上一个能跑的版本
3) 手动接管  你自己进 shell 修，exit 后回来
4) 给提示    写一句方向性提示，模型带着它继续
5) 放弃      停在这里保留现场
```

**手动接管**是真把终端交给你（在真实 shell 里改文件），`exit` 后
把改动 diff 同步给模型，并明确告知"不要重复用户已做的修改"。

非交互环境（CI、管道、nohup）不询问，按 `DS_ON_STUCK` 处理：
`continue`（默认）/ `rollback` / `hint` / `abort`。
整个机制可用 `DS_INTERVENE=0` 关闭。

### 1. MCP HTTP 通道（补齐远端能力）

此前只有 stdio，接不了远端服务。现支持两种 HTTP 传输（纯标准库）：

| 传输 | 协议版本 | 用法 |
|---|---|---|
| Streamable HTTP | 2025-03-26 | `{"url": "...", "type": "streamable-http"}` |
| HTTP + SSE | 2024-11-05 | `{"url": "...", "type": "sse"}` |
| 自动探测 | — | 只给 `url`，先试 streamable，失败降级 SSE |

对应 dsmod 的 `Mcp.smali`（它 2370 行实现了双通道）。header 支持
`${ENV_VAR}` 展开，密钥不用写进配置。

要点：SSE 响应没有 Content-Length，直接 `read()` 会等到超时——
实现里改成增量读，拿到第一个完整事件就收工。

### 2. 环境工具链：完全对齐 305 包

`docker/Dockerfile.full` 按 dsmod-full-stack 的 `setup-all.sh` 五段结构重建：

```bash
docker build -f docker/Dockerfile.full -t ds-agent-full:3.20 .
docker run --rm ds-agent-full:3.20 verify-full     # 逐项自检
DS_IMAGE=ds-agent-full:3.20 python3 ds_agent_pro.py "..."
```

覆盖 20+ 语言运行时（Python/Node/Go/Rust/Java/Kotlin/Scala/Groovy/Clojure/
PHP/Ruby/Perl/Lua/Elixir/Erlang/R/.NET/GHC）+ jadx/apktool/aapt2/r8/d8/
uber-apk-signer/apkid + SQLite/PgSQL/MariaDB/Redis/Nginx。

沿用它两条经验：**工具做成 `/usr/local/bin` 下的 sh wrapper**（`env -i` 也能找到，
不依赖 profile），以及 **verify.sh 逐项自检**。镜像源默认 tuna + ghfast。

日常仍用 `python:3.12-slim`（默认），全量镜像按需切——对齐能力但不拖慢日常任务。

### 3. 流式实时输出 + 围栏兜底

对齐 dsmod 的 `IDLE_MS = 1500` 那套：

- `FenceScanner`：状态机而非正则。流式到达时文本可能停在围栏中间，
  正则匹配不上——这正是它写 219 行的原因。实测把输入切成 7 字节碎片仍能正确切出。
- `ToolCallAccumulator`：结构化 tool_calls 的参数会被切成碎片（可能从字符串中间断开），
  累积到流结束再整体解析。
- `StreamRenderer`：边生成边显示，思考过程单独用暗色前缀。

**优先用结构化 tool_calls，没有时才用围栏兜底**——且兜底只自动执行
`bash/sh` 围栏，其他语言围栏只展示不执行。

开关：`DS_STREAM=0` 关闭，`DS_STREAM_REASON=0` 隐藏思考过程，`DS_IDLE_MS` 调阈值。

### 4. Skill 抽象层

对齐 dsmod 的第三种围栏类型（bash/sh、mcp、**skill**）。

Skill = 带 trigger 的提示词模板，命中才注入，最多 2 个避免提示词膨胀。
内置三个：`tdd`（测试驱动）、`debug`（系统化调试）、`review`（交付前自查）。

```bash
ls skills/          # 可直接编辑，改完生效
DS_SKILL_MAX=3 ...  # 调最多注入几个
```

### 5. Android APK（Chaquopy 打包）

**这是"要 APK"的答案，但请先读 [`android/README.md`](android/README.md) 的执行能力对比。**

```bash
cd android
./gradlew assembleDebug     # 需要本机 Python 3.13.x（仅构建期编译 .pyc）
```

产物 `app/build/outputs/apk/debug/app-debug.apk`，约 60–90 MB。

核心难点是 **Android 上没有独立 Python 解释器**（`libpython3.so` 嵌进 App 进程），
`subprocess.run([sys.executable, ...])` 直接失效。解决办法：

| 失去的 | 补偿 |
|---|---|
| 独立 Python 进程 | `inprocess.py` 进程内执行 |
| 超时 kill 死循环 | **指令预算**（`sys.settrace` 计数），墙钟超时杀不掉同进程字节码 |
| `exit()` 只结束子进程 | 屏蔽该内建函数，否则它会带走整个 App |
| stdio MCP | 显式跳过；**HTTP MCP 仍可用**（远端服务照样接） |

另有两个 Chaquopy 特有的坑已处理：`__file__` 是虚拟路径（skills 改用常量落盘）、
`DS_WORKDIR` 必须在 import 主模块**之前**设好（ROOT 在模块级就固定了）。

后台不被杀靠**前台服务**；Android 15+ 强制 16KB 页对齐，故固定 Python 3.13+。

### 6. Web UI —— 服务器跑，手机访问

**这是推荐用法。** 安卓上没有 Docker，打包成 APK 必然把隔离降级为本地执行；
与其在手机上裸跑 AI 生成的代码，不如让它在服务器上跑（有真正隔离），手机只做交互。

```bash
export DS_WEB_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')
export DS_WEB_HOST=0.0.0.0
python3 ds_agent_pro.py --serve 8080
# 手机浏览器打开 http://<服务器IP>:8080
```

手机端能看到：实时输出流、卡住时的决策弹窗（继续/回滚/手动接管/给提示/放弃）、
本地模式下的写操作确认。

**必须设 `DS_WEB_TOKEN`**——这个服务能执行代码，未设口令会拒绝启动。
默认只监听 `127.0.0.1`，要局域网访问需显式设 `DS_WEB_HOST=0.0.0.0`。

### 7. 本地模式加固（无 Docker 时）

没有 Docker 时 AI 的代码跑在真实环境里。命令黑名单能被拼接绕过，
所以做**多层**而不是单层：

| 层 | 机制 | 能否绕过 |
|---|---|---|
| 资源上限 | rlimit：CPU 60s / 内存 2GB / 文件 256 / 进程 128 | **内核强制，绕不过** |
| 危险命令 | 模式拦截（含 base64 解码执行、下载即执行等绕过） | 能，仅拦常见写法 |
| 写操作审批 | 写/删类命令需人工确认（手机端弹窗） | 取决于确认 |
| 网络隔离 | `unshare -n`（需 root，无权限则不启用） | — |

rlimit 施加在**常驻 shell** 上，它 fork 出的所有命令都继承。
实测：`while :; do :; done` 在 CPU 上限 2s 下 4.0s 被终止。

`DS_NO_LIMIT=1` 关闭限额，`DS_LIMIT_CPU` 等可单项调。

### 8. 调试链优化（不再原地打转）

朴素循环最大的浪费：模型用同一种修法反复试同一个错，20 轮全在打补丁。

`debug_advisor.py` 用两个指纹解决：

- **错误指纹** = 异常类型 + 文件 + 函数名（**不含行号**，改位置也算同一个错）
- **修复指纹** = 用什么工具改了什么 → 统计"换过几种修法"

按「重复次数 × 修法种类」分档给建议：

| 状态 | 给模型的指令 |
|---|---|
| 第 1 次 | 分类 + 定位（语法/导入/类型/算术/路径/逻辑/超时/OOM 共 8 类） |
| 同错 ≥3 次 | 停止微调，完整读一遍代码，确认假设成立 |
| 同错 ≥3 次 **且** 换过 ≥3 种修法 | 写最小复现，或 `git_rollback` 换条路重写 |
| 连续 4 轮无进展 | 缩小问题 / 回退重来，二选一 |

另有：成功信号自动重置停滞计数；写了实现却无断言时提示补最小验证。

### 9. 架构知识按需注入

**结论先说：绝大多数任务用不上，常驻反而诱发过度设计。** 所以做成条件注入——
只有任务确实涉及模型/训练才加载，且注入的是**工程坑位**而非架构科普。

```python
is_ml_task("写一个快速排序")        # False → 注入 0 字符
is_ml_task("用 pytorch 训练分类")    # True  → 只给基础卡（稳定性检查 6 条）
is_arch_task("实现 mamba 混合架构")  # True  → 追加 2026 前沿卡
```

前沿卡覆盖（已核对 2026 公开资料）：混合架构比例 1:5~1:8、DeepSeek V4 的 CSA/HCA、
mHC 的 Sinkhorn 约束为何能防数值爆炸、Muon+AdamW 分工、MoE 的 expert collapse、
MLA 压缩。**每条都标注了坑在哪，以及"什么时候不该用"。**

### 10. 持久终端会话（对照 dsmod 优化）

`terminal.py` 是对 GitHub 项目 `dsmod-*` 终端部分的针对性重写。核心差异：

| dsmod (TermEnv.execOnce) | 本实现 |
|---|---|
| 每次调用新起进程，**cd / export 全丢** | 长驻会话，**cwd 与环境变量跨调用保留** |
| 超时检查写在 read 循环内，**无输出挂死杀不掉** | `killpg` 杀进程组，4s 精确熔断 |
| stderr 混进 stdout | 分离到 `[stderr]` 段 |
| 超 400000 字符**静默丢弃** | 超限明确标注「截断 N 字符」 |
| `destroy()` 后 `waitFor()` 无超时 | 无此路径 |

实测：120 次混合命令（含 40 次 `exit`）**0 失败**；`sleep 30` + timeout=4 → 4.0s 熔断并自动重建。

详细对比见 `COMPARE.md`。

模型侧获得的能力：可以 `cd` 后连续执行、`export` 后续有效、每次返回带 `[cwd]`；
另有 `shell_reset` 工具重置被污染的状态。

### 11. Docker 沙箱

容器参数：`--network=none --memory=512m --cpus=1 --pids-limit=128`，工作目录只读挂载为 `/w`。

- 需要装包时：`DS_NETWORK=bridge python3 ds_agent_pro.py "..."`（否则 pip 连不上网）
- 换镜像（要 gcc / node 等）：`DS_IMAGE=python:3.12-bookworm`
- 强制本地：`DS_SANDBOX=local`

另有两层兜底，容器内外都生效：命令黑名单（`rm -rf /`、`dd if=`、fork 炸弹等）、路径逃逸检查（拒绝 `../../etc/passwd`）、超时熔断。

### 12. 文件系统外链（手机 / 外部文件夹）

用 `DS_FS_ROOTS` 授权真实目录给模型，多路径用 `:` 分隔：

```bash
# 可读写
DS_FS_ROOTS="/sdcard/Download:/sdcard/Documents" python3 ds_agent_pro.py "..."

# 只读（推荐先这样跑，确认行为没问题再放开写）
DS_FS_READONLY=1 DS_FS_ROOTS="/sdcard/DCIM" python3 ds_agent_pro.py "..."
```

启动时自动挂上 filesystem server，10 个工具：`fs_roots / fs_list / fs_read / fs_write / fs_mkdir / fs_delete / fs_move / fs_copy / fs_stat / fs_search`。

授权机制是**白名单**：只有显式列出的目录能碰，其余一律拒绝。已验证 `/etc/passwd`、`/root/.ssh/id_rsa`、`..` 穿越全部被拦。

Docker 模式下外部目录会同步挂进容器（`/ext/0`、`/ext/1`…），代码里可用绝对路径访问；只读模式一律 `:ro` 挂载。

### 13. MCP 支持

`mcp.json` 用 Claude Desktop 同款格式：

```json
{
  "mcpServers": {
    "demo":  { "command": "python3", "args": ["mcp_demo_server.py"] },
    "fs":    { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"] },
    "git":   { "command": "uvx", "args": ["mcp-server-git", "--repository", "."] },
    "off":   { "command": "...", "disabled": true }
  }
}
```

启动时自动 `initialize → tools/list`，工具名转成 `mcp_<server>__<tool>` 并入模型可用工具集，冲突自动隔离。

只测 MCP 通道、不烧 token：

```bash
python3 ds_agent_pro.py --mcp-only
```

`mcp_demo_server.py` 是纯标准库的 Server 模板，加工具只需往 `TOOLS` 和 `HANDLERS` 里各加一项。

### 14. Git 快照回滚

每步改动自动 commit。跑崩了三种回法：

```bash
python3 ds_agent_pro.py --snapshots      # 看快照列表
python3 ds_agent_pro.py --rollback 3     # 回退 3 步
```

模型自己也带 `git_rollback` 工具，改崩了会自己回退重试。回退前自动存保险点，误操作可用 `git reflog` 找回。

关闭：`DS_SNAPSHOT=0`。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DS_MODEL` | `deepseek-v4-pro` | 省钱换 `deepseek-v4-flash`（约 1/12 价格） |
| `DS_WORKDIR` | `./workspace` | 工作目录，也会被 git 接管 |
| `DS_SANDBOX` | `auto` | `docker` / `local` / `auto` |
| `DS_IMAGE` | `python:3.12-slim` | 沙箱镜像 |
| `DS_NETWORK` | `none` | 装包时改 `bridge` |
| `DS_TIMEOUT` | `30` | 单次执行超时（秒） |
| `DS_ROUNDS` | `20` | 最大轮次，防跑飞 |
| `DS_WALL` | `900` | 墙钟看门狗（秒），到点强制停止 |
| `DS_THINKING` | `0` | 设 `1` 开思考模式，规划更强、更慢更贵 |
| `DS_SNAPSHOT` | `1` | 设 `0` 关 git 快照 |
| `DS_MCP_CFG` | `./mcp.json` | MCP 配置文件路径 |
| `DS_FS_ROOTS` | 空 | 外链目录，`:` 分隔，如 `/sdcard/Download:/sdcard/Documents` |
| `DS_FS_READONLY` | `0` | 设 `1` 外链只读，写工具直接不暴露给模型 |
| `DS_FS_MAXFILE` | `2MB` | 单次读取上限，超出提示分段读 |
| `DS_MAX_SAME` | `3` | 同一错误出现几次后升级建议 |
| `DS_MAX_FIXES` | `3` | 换过几种修法后判定"思路有问题" |
| `DS_MAX_STALL` | `4` | 连续几轮无进展后强制换策略 |
| `DS_INTERVENE` | `1` | 设 `0` 关闭主动介入 |
| `DS_ITER_STALL` | `3` | 连续失败几轮后介入 |
| `DS_ITER_SAME` | `4` | 同一错误累计几次后介入 |
| `DS_ITER_TIME` | `240` | 已用多久且仍在失败时介入（秒） |
| `DS_ON_STUCK` | `continue` | 非交互环境策略：`continue`/`rollback`/`hint`/`abort` |
| `DS_HINT` | 空 | `DS_ON_STUCK=hint` 时的提示内容 |
| `DS_STREAM` | `1` | 设 `0` 关闭流式输出 |
| `DS_STREAM_REASON` | `1` | 设 `0` 隐藏思考过程 |
| `DS_IDLE_MS` | `1500` | 空闲判定阈值（对齐 dsmod 的 `IDLE_MS`） |
| `DS_SKILL_DIR` | `./skills` | skill 目录 |
| `DS_SKILL_MAX` | `2` | 最多注入几个 skill |
| `DS_WEB_TOKEN` | 空 | `--serve` 必填，访问口令 |
| `DS_WEB_HOST` | `127.0.0.1` | 要局域网访问设 `0.0.0.0` |
| `DS_WEB_WAIT` | `600` | 等待手机端决策的秒数，超时按 `DS_ON_STUCK` |
| `DS_NO_LIMIT` | `0` | 设 `1` 关闭本地模式的资源上限 |
| `DS_LIMIT_CPU` | `60` | 本地模式 CPU 秒上限 |
| `DS_LIMIT_AS` | 2GB | 本地模式地址空间上限 |
| `DS_LIMIT_NPROC` | `128` | 本地模式进程数上限 |
| `DS_LOCAL_NET` | `0` | 设 `1` 尝试 `unshare -n` 断网（需 root） |
| `DS_IP_BUDGET` | `30000000` | Android 进程内执行的指令预算（任务重就调大） |

## 手机上怎么用

有两条路，取决于你能不能接受隔离降级：

| | **服务器跑 + 手机访问**（推荐） | **打包成 APK** |
|---|---|---|
| 隔离 | Docker 真隔离 | 本地执行，**无隔离** |
| 死循环防护 | 进程边界 + 超时 kill | 仅指令预算 |
| 需要一台常开的机器 | 是 | 否 |
| 上手成本 | 一条命令 | 需 Android Studio + Python 3.13 |
| 详见 | 下文 | [`android/README.md`](android/README.md) |

### 路线 A：服务器跑 + 手机访问（推荐）

**方案：服务器跑 + 手机访问。** 手机上没有 Docker，直接在 Termux 跑等于让 AI 的代码
在你真实环境里执行。

```
服务器（有 Docker）              手机
┌──────────────────┐        ┌──────────────┐
│ ds-agent --serve │◀─SSE──▶│  浏览器访问   │
│  Docker 真隔离    │        │ 看输出/做决策 │
└──────────────────┘        └──────────────┘
```

服务器可以是闲置电脑、NAS、云主机。手机只需要一个浏览器。
卡住时手机端会弹出决策框（继续/回滚/给提示/放弃）。

如果确实要在手机本地跑，见下节 Termux 说明，并务必开启加固。

## 手机（Termux）专用说明

常见根目录：

| 路径 | 说明 |
|---|---|
| `/sdcard/Download` | 下载目录 |
| `/sdcard/Documents` | 文档目录 |
| `/sdcard/DCIM` | 相册 |
| `/data/data/com.termux/files/home` | Termux 家目录 |

**首次必须授权存储**，否则 Termux 读不到 `/sdcard`：

```bash
termux-setup-storage        # 会弹权限框，点允许
pkg install git python      # git 用于快照，别忘装
```

PRoot / 非 root 环境注意：`CapEff=0`，ptrace 不透传，frida、strace、gdb attach 都不可用（这是容器硬限制，不是配置问题）。Docker 在手机上基本跑不了，会回退本地执行——**所以手机上更要开 `DS_FS_READONLY=1`**，把破坏半径锁死。

典型用法：

```bash
DS_FS_READONLY=1 DS_FS_ROOTS="/sdcard/Download" \
python3 ds_agent_pro.py "统计下载目录里所有 csv 的行数，写个脚本跑出结果"
```

## 模型怎么选

日常代码生成 `deepseek-v4-flash` 够用且便宜；需要复杂多步规划再上 `deepseek-v4-pro`。V4 两档都原生支持 OpenAI 格式 function calling，1M 上下文 / 384K 输出，思考模式下也能调工具。

## 已验证

- MCP 端到端：`initialize` → `tools/list` → `tools/call`（含错误分支）
- Filesystem：10 个工具全通；`/etc/passwd`、`/root/.ssh/id_rsa`、`..` 穿越、未授权目录**全部拒绝**
- Filesystem 边界：二进制自动跳过、2MB 大文件提示分段、非空目录拒删、append 追加正确
- 只读模式：写工具从工具列表里彻底摘除，模型根本看不到
- Git 快照：连续提交 4 次 → 回退 1 步 → 文件内容正确还原
- 沙箱：Docker 缺失自动回退、危险命令拦截、路径逃逸拦截、超时熔断
- 主循环：MCP 工具并入、重复调用去重、回滚后收敛退出
- 外链集成：`DS_FS_ROOTS` 自动挂载，系统提示正确告知模型可用路径
- 终端：120 次混合命令 0 失败、`exit 0/1/2/42` 码全对、超时精确熔断并重建
- 终端竞态：BrokenPipe 与空输出两种场景均自动重建重试，已压测确认不再复现
- 调试顾问：8 类错误分类全对、指纹判定正确、重复 3 次×3 修法触发升级建议
- 架构卡片：普通任务注入 0 字符、ML 任务只给基础卡、架构任务追加前沿卡
- 主动介入：三种触发条件、5 种决策解析（含非法输入重选）、4 种非交互策略
- 主动介入：报告同秒多次落盘不覆盖；用户提示正确注入并取出后清空
- Web UI：无/错/对 token 分别 401/401/200；介入经浏览器应答链路贯通（含超时回落）
- 本地加固：14 条危险命令全拦、7 条正常命令无误拦；rlimit 实测终止死循环

## 持续集成

`.github/workflows/ci.yml` 在每次 push / PR 时跑 15 组断言 + Android 工程静态检查（语法、MCP 协议、
终端状态与超时、60 次压测、调试顾问、架构卡片、主动介入、安全隔离、setup.sh）。

本地先自检再 push，断言与 CI 完全一致：

```bash
python3 selftest.py
```

## 与 dsmod-* 的关系

本项目受 `liuben3223380369-ops/dsmod-*` 启发并做了针对性重写。
两者性质不同：dsmod 是**逆向研究资料**（smali 逐字节分析 + 环境增强脚本），
ds-agent 是**可直接部署运行的产品**。

- `COMPARE.md` —— 终端实现的逐项对比（execOnce vs 持久会话）与实测数据
- `PROJECT_COMPARE.md` —— 项目级对比：定位差异、核心链路、双方能力缺口

核心差异一句话：dsmod 要手写 219 行状态机解析 ```bash``` 围栏（因为模型不支持
function calling），ds-agent 用原生 tool_calls，**解析代码为 0 行**。

## 文件

| 文件 | 作用 |
|---|---|
| `ds_agent_pro.py` | 主程序：沙箱 + git + MCP + 收敛控制 + 外链挂载 |
| `terminal.py` | 持久终端会话（对 dsmod execOnce 的重写） |
| `debug_advisor.py` | 调试顾问：错误分类、双指纹、自适应升级建议 |
| `intervene.py` | 主动介入：卡住暂停、诊断报告、人工接管（支持外部决策通道） |
| `webui.py` | Web UI：SSE 流式输出 + 手机端决策（服务器跑，手机访问） |
| `hardening.py` | 本地模式加固：rlimit + 命令闸门 + 写操作审批 |
| `inprocess.py` | 进程内执行器：无独立解释器时的降级（指令预算拦死循环） |
| `android_shim.py` | Android 适配：工作目录、skills 落盘、能力探测 |
| `arch_cards.py` | 架构知识按需注入（工程坑位取向，非架构科普） |
| `selftest.py` | 本地自检（与 CI 同断言，push 前跑一遍） |
| `COMPARE.md` | 与 dsmod 终端实现的逐项对比与实测数据 |
| `PROJECT_COMPARE.md` | 项目级对比：定位差异、核心链路、能力缺口 |
| `mcp_fs_server.py` | 文件系统 MCP Server（白名单授权，读写可切） |
| `mcp_client.py` | MCP 客户端与传输适配（stdio / HTTP 统一接口） |
| `mcp_http.py` | MCP HTTP 传输：Streamable HTTP + HTTP/SSE |
| `streaming.py` | 流式渲染、围栏状态机、工具调用碎片累积 |
| `skills.py` | Skill 抽象层（对齐 dsmod 的第三种围栏） |
| `docker/Dockerfile.full` | 全量工具链镜像（对齐 305 包） |
| `mcp_demo_server.py` | 示例 MCP Server，也是写新 Server 的模板 |
| `ds_agent.py` | 精简版（仅本地执行，无上面四项） |
