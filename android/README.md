# ds-agent Android 版（Chaquopy 打包）

把 Python 版 ds-agent 嵌进 Android App。**私人研究自用，不要分发。**

## 一句话说明风险

Android 上**没有 Docker，也没有独立 Python 解释器**，所以隔离必然降级。
这不是配置问题，是平台限制。本项目做了能做的最大加固，但**不等于沙箱**——
详见下方「执行能力对比」。若可接受，继续；若必须有真隔离，请用
[Web UI 方案](../README.md#5-web-ui--服务器跑手机访问)（服务器跑 + 手机访问）。

---

## 构建

### 前置

1. **Android Studio**（Ladybug 或更新）+ JDK 17
2. **本机 Python 3.13.x** —— 必须与 `app/build.gradle.kts` 里的
   `chaquopy.version = "3.13"` 主次版本一致。
   它只用于构建期把 .py 编译成 .pyc，不参与运行。
   ```bash
   python3.13 -V        # 必须是 3.13.x
   ```
   若 Gradle 找不到，取消 `build.gradle.kts` 里 `buildPython(...)` 的注释并填绝对路径。
3. Gradle 不要开离线模式（首次需要下载 Chaquopy 的 wheel 和 Python 运行时）

### 步骤

```bash
cd android
# 用 Android Studio 打开这个目录，或：
./gradlew assembleDebug      # 调试版
./gradlew assembleRelease    # 发布版（需签名配置）
```

产物：`app/build/outputs/apk/debug/app-debug.apk`

预计体积 **60–90 MB**（含 CPython 运行时 + openai SDK，arm64 单 ABI 约 35–45 MB）。

### 常见构建失败

| 报错 | 原因 | 处理 |
|---|---|---|
| `buildPython version X is incompatible` | 本机 Python 主次版本与 `version` 不一致 | 换 Python 3.13.x，或改 `buildPython` 指向正确解释器 |
| `Unresolved reference: buildPython` | 把 `buildPython` 写进了 `android{}` 块 | 它属于 `chaquopy{}` 块 |
| `Cannot find chaquopy()` | `chaquopy {}` 写进了 `android {}` 内部 | 与 `android {}` **同级** |
| dlopen failed / 启动即崩 | 用了 Python 3.12 或更低 | Android 15+ 强制 16KB 页对齐，必须 3.13+ |

---

## 使用

1. 安装 APK，打开后先填 **DeepSeek API Key**（存本地 SharedPreferences，不上传）
2. 输入任务，点运行
3. 切后台也不会被杀——跑在前台服务里，通知栏有常驻提示

工作目录：`/data/data/com.dsagent/files/workspace`
（App 私有目录，不需要存储权限）

---

## 执行能力对比

这是本文档最重要的部分。Android 上失去了什么、用什么补偿：

| 能力 | 桌面 / 服务器 | Android (Chaquopy) | 补偿措施 |
|---|---|---|---|
| **独立 Python 进程** | ✅ | ❌ `libpython3.so` 嵌进 App，无法 fork-exec | 改为**进程内执行**（`inprocess.py`） |
| **死循环防护** | ✅ 进程边界 + 超时 kill | ⚠️ 同进程，kill 不掉 | **指令预算**：`sys.settrace` 逐条计数，超 3000 万条抛异常。<br>这是唯一能真正中断死循环的办法——墙钟超时杀不掉正在跑的字节码 |
| **`sys.exit()` / `exit()`** | ✅ 只结束子进程 | ❌ 会带走整个 App | 屏蔽这两个内建函数 |
| **Docker 隔离** | ✅ | ❌ | 强制 `local` 模式 + `hardening.py` 加固 |
| **资源上限（rlimit）** | ✅ | ⚠️ Bionic 上部分可用 | 尽力设置，失败则跳过（不假装成功） |
| **shell (`sh`)** | ✅ | ✅ `/system/bin/sh` 通常可用 | 危险命令拦截 + 写操作审批 |
| **stdio MCP** | ✅ 起子进程 | ❌ 同样需要 spawn Python | 显式跳过并提示 |
| **HTTP MCP** | ✅ | ✅ 纯网络，可用 | **远端 MCP 服务在手机上照样能接** |
| **文件系统外链** | ✅ | ⚠️ 需存储权限 | 走 `DS_FS_ROOTS`，建议先开只读 |

### 关于「进程内执行」必须知道的事

同一进程内**无法阻止**代码：读写任意 App 有权访问的文件、发网络请求、
改全局变量。能守住的是：

- ✅ 死循环不会拖死 App（指令预算）
- ✅ 异常不会冒泡炸掉主流程（统一转成 traceback 字符串）
- ✅ `exit()` 不会让 App 消失
- ✅ 输出被重定向捕获，不会污染 UI

**守不住的**：代码主动读你的其他文件、主动联网外传。
如果这点不能接受，请把代码放服务器跑，手机只做交互。

---

## 目录结构

```
android/
├── settings.gradle.kts          # 含 Chaquopy maven 仓库
├── build.gradle.kts             # 插件版本声明
└── app/
    ├── build.gradle.kts         # chaquopy 配置 + python.srcDirs 指向仓库根
    └── src/main/
        ├── AndroidManifest.xml  # 前台服务 + 网络权限
        ├── java/com/dsagent/
        │   ├── AgentApp.kt      # Application.onCreate 里 Python.start()
        │   ├── MainActivity.kt  # UI + 广播接收输出
        │   └── AgentService.kt  # 前台服务，跑任务不被系统回收
        ├── python/
        │   └── agent_bridge.py  # Kotlin ↔ Python 桥接
        └── res/                 # 布局 / 主题 / 图标
```

### 为什么 `python.srcDirs` 指向 `../../`

```kotlin
python.srcDirs("src/main/python", "../../")
```

第二个路径指向仓库根，这样 `ds_agent_pro.py` / `terminal.py` / `mcp_client.py`
等主模块直接被打包进 APK，**不需要复制一份**，也不会出现两份不同步。
只有 `src/main/python/agent_bridge.py` 是 Android 独有的。

### 为什么 skills 目录要用常量落盘

Chaquopy 下模块从 APK asset 加载，`__file__` 是虚拟路径，
`Path(__file__).parent / "skills"` 在磁盘上**不存在**；
`.md` 也不是 Python 模块，不会进 import 路径。

所以 `android_shim._seed_skills()` 直接用 `skills.py` 里的 `BUILTIN`
字符串常量写到 `filesDir/skills/`——不依赖任何文件系统查找。

---

## 调优环境变量

在 `AgentService.kt` 里 `os.environ` 设置，或改 `android_shim.init()` 的默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DS_IP_BUDGET` | `30000000` | 指令预算。任务重（如训练小模型）要调大 |
| `DS_STREAM` | `1` | 流式输出 |
| `DS_INTERVENE` | `1` | 卡住时暂停（手机上体现为日志提示） |
| `DS_ON_STUCK` | `continue` | 无人应答时的策略 |
| `DS_FS_ROOTS` | 空 | 外链目录，冒号分隔 |
| `DS_FS_READONLY` | `0` | **手机上建议设 `1`** |
| `DS_WALL` | `900` | 单任务墙钟上限（秒） |

---

## 已知限制

- **首次启动慢**：CPython 初始化 + openai 导入，约 2–5 秒
- **内存**：前台服务常驻，低内存设备上可能被 LMK 回收（无法完全避免）
- **Android 15+ 的 16KB 页对齐**：必须 Python 3.13+，已在 `build.gradle.kts` 固定
- **国产 ROM**：需手动加入电池优化白名单 + 允许自启动，否则后台仍可能被杀
- **无 root** 下 `unshare -n` 不可用，网络隔离只能靠不授予权限

---

## 合规

仅供私人研究。不要分发 APK，不要内置他人的 API Key。
仓库 `ds-agent` 本身是 MIT，但打包进去的 CPython / openai SDK / Chaquopy
各有自己的许可证，分发时需分别遵守。
