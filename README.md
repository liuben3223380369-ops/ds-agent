# ds-agent-pro

DeepSeek 驱动的「自动写代码 → 运行 → 检查 → 自我修复」完整闭环。三项能力都已实现并验证。

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

## 三项能力

### 0. 持久终端会话（对照 dsmod 优化）

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

### 1. Docker 沙箱

容器参数：`--network=none --memory=512m --cpus=1 --pids-limit=128`，工作目录只读挂载为 `/w`。

- 需要装包时：`DS_NETWORK=bridge python3 ds_agent_pro.py "..."`（否则 pip 连不上网）
- 换镜像（要 gcc / node 等）：`DS_IMAGE=python:3.12-bookworm`
- 强制本地：`DS_SANDBOX=local`

另有两层兜底，容器内外都生效：命令黑名单（`rm -rf /`、`dd if=`、fork 炸弹等）、路径逃逸检查（拒绝 `../../etc/passwd`）、超时熔断。

### 2. MCP 支持

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

### 3. 文件系统外链（手机 / 外部文件夹）

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

### 4. Git 快照回滚

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

## 文件

| 文件 | 作用 |
|---|---|
| `ds_agent_pro.py` | 主程序：沙箱 + git + MCP + 收敛控制 + 外链挂载 |
| `terminal.py` | 持久终端会话（对 dsmod execOnce 的重写） |
| `COMPARE.md` | 与 dsmod 终端实现的逐项对比与实测数据 |
| `mcp_fs_server.py` | 文件系统 MCP Server（白名单授权，读写可切） |
| `mcp_client.py` | MCP stdio 客户端（JSON-RPC 2.0，纯标准库） |
| `mcp_demo_server.py` | 示例 MCP Server，也是写新 Server 的模板 |
| `ds_agent.py` | 精简版（仅本地执行，无上面四项） |
