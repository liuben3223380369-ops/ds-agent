# 终端实现对比：dsmod vs 本实现

对照 GitHub 项目 `liuben3223380369-ops/dsmod-*` 的终端部分
（`Exp.runShell` → `TermEnv.execOnce`，见 `dsmod-autocoder-analysis/docs/key-methods/`），
逐项拆解、指出缺陷，并给出本实现的做法与实测数据。

## 一、dsmod 的终端是怎么工作的

从 smali 还原出的调用链：

```
Exp.runShell (378行)
  └─ TermEnv.ensure     解包 alpine.tar.gz + libproot.so + 工具链
  └─ TermEnv.cmd        拼 proot 参数：-r rootfs / -b /sdcard,/storage,/dev,/proc,/sys / -w /root
  └─ TermEnv.applyEnv   PROOT_TMP_DIR / LD_LIBRARY_PATH / HOME，PATH 追加 :/opt/dt :/skills :/tmp
  └─ TermEnv.execOnce   ProcessBuilder("/bin/sh","-c",cmd) → redirectErrorStream(true) → 8KB 读循环
```

`execOnce` 的核心逻辑（`TermEnv.execOnce.timeout.smali`，110 行）：

```
deadline = now + timeout
buf = new byte[0x2000]
while ((n = in.read(buf)) > 0) {
    if (sb.length() < 0x61a80) sb.append(...)      // 上限 400000
    if (System.currentTimeMillis() > deadline) { p.destroy(); break; }
}
p.waitFor()
return sb.toString()
```

收敛控制常量（来自 `Exp.smali`）：`MAX_EXEC=0x3c(60)`、`MAX_TEXT=0x30d40(200000)`、
`IDLE_MS=0x5dc(1500)`、`WATCHDOG=0x3a98(15000)`，配合 `EXECED / PENDING / ROUNDS / WATCHDOG`
四个 ConcurrentHashMap 做去重、防重入、轮次、看门狗。

## 二、逐项对比

| 维度 | dsmod (execOnce) | 本实现 (terminal.py) |
|---|---|---|
| 会话模型 | **每次调用新起进程** | 长驻 shell（docker 模式是长驻容器 + `docker exec`） |
| cwd | 丢失，只能回 `/root` | **跨调用保留**，每次回显 `[cwd]` |
| 环境变量 | 丢失 | **跨调用保留**，写入 `.dsenv`，崩溃重建后自动 source 恢复 |
| 超时机制 | `read()` 循环内检查 deadline | `killpg` 杀整个进程组，不看进程脸色 |
| 无输出挂死 | **杀不掉**（read 阻塞，永远进不了检查） | 4.0s 精确熔断并重建会话 |
| `destroy()` 后 | `waitFor()` 无超时，可永久阻塞 | 无此路径，直接 kill |
| stderr | `redirectErrorStream(true)` 混入 stdout | 分离，输出到 `[stderr]` 段 |
| 输出上限 | 400000，**超限时静默丢弃** | 60000，明确打印「截断 N 字符」 |
| exit code | `exit=N` 追加在输出尾部 | `[exit=N]` 独立成行 |
| 环境头 | `[环境] Alpine 容器 · PRoot 沙箱 · 容器内 uid=0` | 同思路，信息更全（镜像/网络/内存/uid/cwd） |
| 降级透明 | `[环境] 系统 sh（非容器，非 root…）` | 同，并在启动时打印警告 |
| 防重入 | `PENDING` ConcurrentHashMap | `threading.Lock` |
| 看门狗 | `WATCHDOG` 15s + `IDLE_MS` 1.5s | 墙钟看门狗 `DS_WALL`（默认 900s）+ 轮次上限 |
| `exit N` 处理 | 无（进程结束即结束） | EXIT trap 兜底回传 rc，随后自动重建并恢复环境 |

## 三、实测数据

```
会话状态保留
  cd /tmp && pwd       → /tmp
  下一条 pwd            → /tmp          （dsmod 这里会回到 /root）
  export MYVAR=abc123   → 下一条 echo $MYVAR → abc123
  X=42 → echo $((X+1)) → 43

exit 码正确性
  exit 0 / 1 / 2 / 42  → [exit=0] [exit=1] [exit=2] [exit=42]  全部正确

可靠性压测
  120 次混合命令（40 次含 exit、含 stderr、普通）  → 失败 0/120
  exit 后环境变量恢复，重复 5 轮                  → 5/5 成功

超时熔断
  sleep 30, timeout=4  → 4.0s 熔断，会话自动重建，后续命令正常
  while true; do sleep 1; done（完全无输出）→ 5.0s 熔断  ← dsmod 在此场景会永久挂住

输出截断
  输出 200000 字符 → 返回 60079 字符，含「截断 140000+ 字符」标记
```

## 四、过程中发现并修掉的两个真实竞态

1. **BrokenPipeError**：上一条命令的 `exit` 可能在下一次写入的间隙才真正终止会话，
   导致 `stdin.write` 抛异常。修法：捕获后重建会话重试一次。
   修复前后：30 次混合测试失败 2 次 → 60 次失败 1 次。

2. **空输出竞态**：写入成功但 shell 随即退出，返回 `rc=-1` 且三者全空。
   修法：判定「命令压根没执行」→ 重建后重试一次（此时重试是安全的，因为无副作用发生）。
   修复后：120 次压测 **0 失败**。

## 五、dsmod 值得学、已吸收的三点

1. **环境头**：每条命令输出前先告诉模型自己在什么环境（容器 / 裸机 / uid），
   避免模型误以为有 root 而写出跑不了的命令。本实现继承并扩展了这一设计。
2. **降级透明**：容器不可用时明确回退系统 sh 并告知，而不是静默降级。
3. **proot 绑定参数**：`-w` 指定工作目录、PATH 追加工具链目录、绑定 `/sdcard /storage`——
   对应本实现的 `mounts` 外链挂载。

## 六、诚实的边界

- **性能**：本机执行简单命令时，持久会话与每次新进程差异很小（实测 1.2x）。
  真正的收益不在速度，而在 **状态保留**；在 Docker 场景下另有一次数量级提升
  （省掉每次 `docker run` 的容器启动开销，改为复用长驻容器）。
- **未移植**：`IDLE_MS` 流式空闲判定（用于流式 delta 到达时判断代码块是否闭合），
  这属于 UI 流式渲染范畴，本实现走 API 结构化 `tool_calls`，不需要。
- **手机上**：Termux / PRoot 无 Docker，必然回退本地执行；此时 `export -p` 的
  格式依赖 shell 实现，恢复失败会静默跳过（`2>/dev/null || true`），不影响主流程。
