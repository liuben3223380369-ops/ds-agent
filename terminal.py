#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持久化终端会话 —— 对照 dsmod 的 TermEnv.execOnce 做了针对性改进。

【dsmod 的做法】(Exp.runShell → TermEnv.execOnce)
    ProcessBuilder("/bin/sh","-c",cmd) + redirectErrorStream(true) + 8KB 读循环
    · 每次调用都新起进程 → cd / export 全部丢失，模型被迫写绝对路径
      （它自己的注释写着：代价：cd / export 不保留，故提示词要求绝对路径）
    · 超时检查写在 read 循环体内 → 无输出的挂死进程永远杀不掉
    · destroy() 之后 waitFor() 没有超时 → 忽略 SIGTERM 的进程永久阻塞
    · 输出超 0x61a80(400000) 静默丢弃，模型不知道被截了
    · stderr 混进 stdout，分不清哪部分是报错

【本模块的做法】
    · 长驻 shell（docker 模式是长驻容器 + docker exec），cwd / env 跨调用保留
    · 超时不看进程脸色：killpg 杀整个进程组 + 重建会话
    · stdout / stderr 分离，exit code 显式回传，cwd 每次回显
    · 输出超限明确标注截断了多少字符，绝不静默丢弃
    · 环境头告诉模型自己在容器里还是裸机（学 dsmod，但信息更全）
    · 会话状态存 .dsenv，崩溃重建后自动 cd + export 恢复
"""

import os
import json
import uuid
import shutil
import signal
import threading
import subprocess
from pathlib import Path


def _preexec(limits):
    """在 fork 出的子进程里施加 rlimit。只能降不能升。"""
    import resource
    RL = {"cpu": resource.RLIMIT_CPU, "as": resource.RLIMIT_AS,
          "nofile": resource.RLIMIT_NOFILE, "nproc": resource.RLIMIT_NPROC,
          "fsize": resource.RLIMIT_FSIZE}

    def _fn():
        for k, v in (limits or {}).items():
            if k not in RL:
                continue
            try:
                soft, hard = resource.getrlimit(RL[k])
                new = min(v, hard) if hard != resource.RLIM_INFINITY else v
                resource.setrlimit(RL[k], (new, hard))
            except (ValueError, OSError):
                pass

    return _fn


class Terminal:
    """跨调用保留 cwd 与环境变量的 shell 会话。"""

    MAX_OUT = 60000
    MAX_ERR = 20000

    def __init__(self, root, mode="auto", mounts=None, image="python:3.12-slim",
                 network="none", memory="512m", cpus="1", limits=None):
        self.root = Path(root).resolve()
        self.mounts = mounts or []
        self.image, self.network = image, network
        self.memory, self.cpus = memory, cpus
        self.mode = mode
        if mode == "auto":
            self.mode = "docker" if self._docker_ok() else "local"

        self.proc = None
        self.container = None
        self.cwd = "/w" if self.mode == "docker" else str(self.root)
        self.env_file = self.root / ".dsenv"       # docker 下经 /w 挂载可见
        self.env_path = "/w/.dsenv" if self.mode == "docker" else str(self.env_file)
        self._lock = threading.Lock()              # 防重入（对应 dsmod 的 PENDING）
        self.note = ""
        self.limits = limits or {}                 # 本地模式资源上限（Docker 不需要）
        self._spawn()

    # ---------------- 会话生命周期 ----------------
    @staticmethod
    def _docker_ok():
        if not shutil.which("docker"):
            return False
        try:
            return subprocess.run("docker info", shell=True, capture_output=True,
                                  timeout=20).returncode == 0
        except Exception:
            return False

    def _mount_flags(self):
        return " ".join(f"-v {json.dumps(str(h))}:{c}:{rw}" for h, c, rw in self.mounts)

    def _spawn(self):
        if self.mode == "docker":
            self.container = f"dsterm_{os.getpid()}_{uuid.uuid4().hex[:6]}"
            cmd = (f"docker run -d --name {self.container} --network={self.network} "
                   f"--memory={self.memory} --cpus={self.cpus} --pids-limit=128 "
                   f"-v {self.root}:/w {self._mount_flags()} -w /w {self.image} sleep infinity")
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if r.returncode == 0:
                self.note = (f"[沙箱] Docker 长驻容器已就绪（{self.image}，"
                             f"网络 {self.network}，内存 {self.memory}）")
            else:
                self.mode = "local"
                self.note = (f"[沙箱] ⚠️ Docker 启动失败，回退本地："
                             f"{r.stderr.strip()[:150]}")
        if self.mode == "local":
            if not self.note:
                self.note = "[沙箱] ⚠️ 未检测到可用 Docker，已回退本地执行（无隔离）"
            # rlimit 施加在常驻 shell 上，它 fork 出的所有命令都会继承。
            # 这是本地模式下唯一绕不过去的一层（命令黑名单可以被拼接绕过）。
            kw = {}
            if getattr(self, "limits", None):
                kw["preexec_fn"] = _preexec(self.limits)
            self.proc = subprocess.Popen(
                ["sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(self.root), text=True,
                bufsize=1, start_new_session=True, **kw
            )
        self._restore()

    def _restore(self):
        if not self.env_file.exists():
            return
        body = self.env_file.read_text(encoding="utf-8", errors="replace")
        try:
            if self.mode == "docker":
                subprocess.run(["docker", "exec", "-w", self.cwd, self.container,
                                "sh", "-c", f". /w/.dsenv 2>/dev/null; true"],
                               capture_output=True, timeout=10)
            else:
                self.proc.stdin.write(f'. "{self.env_file}" 2>/dev/null; true\n')
                self.proc.stdin.flush()
        except Exception:
            pass

    def _save_state(self, pwd, envdump):
        try:
            lines = [f"cd {json.dumps(pwd)} 2>/dev/null || true"]
            lines += [l for l in envdump.splitlines() if l.startswith("export ")]
            self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass
        if pwd:
            self.cwd = pwd

    # ---------------- 包装与解析 ----------------
    @staticmethod
    def _wrap(cmd, marker, errfile=None):
        """命令统一包装：块执行 → 回传 rc / pwd / env → end 哨兵。

        用 EXIT trap 兜底：命令里直接 exit N 会终止会话，
        trap 保证这种情况下仍能把 rc 和状态回传出来（随后自动重建会话）。
        """
        redirect = f" 2>{json.dumps(errfile)}" if errfile else ""
        tail = (f'echo "{marker}|rc:$?"; echo "{marker}|pwd:$(pwd)"; '
                f'echo "{marker}|env:"; export -p 2>/dev/null; echo "{marker}|end"')
        return (f"trap '{tail}' EXIT\n"
                f'{{ {cmd}\n}}{redirect}\n'
                f'echo "{marker}|rc:$?"\n'
                f'echo "{marker}|pwd:$(pwd)"\n'
                f'echo "{marker}|env:"\n'
                f'export -p 2>/dev/null\n'
                f'echo "{marker}|end"\n'
                f"trap - EXIT\n")

    @staticmethod
    def _parse(raw, marker):
        """从原始输出里切出 stdout / rc / pwd / env。"""
        out, meta = [], []
        rc, pwd, section = -1, None, "out"
        for line in raw.splitlines(keepends=True):
            if marker in line and "|" in line:
                tag = line.split("|", 1)[1].split(":", 1)[0].strip()
                if tag == "rc":
                    try:
                        rc = int(line.split(":", 1)[1].strip() or "-1")
                    except ValueError:
                        rc = -1
                elif tag == "pwd":
                    pwd = line.split(":", 1)[1].strip()
                elif tag == "env":
                    section = "env"
                    continue
                elif tag == "end":
                    break
                continue
            (out if section == "out" else meta).append(line)
        return "".join(out), rc, pwd, "".join(meta)

    # ---------------- 执行 ----------------
    def _run_local(self, payload, timeout, _retry=True):
        # 上次执行若以 exit 结束，会话已死，这里重建并从 .dsenv 恢复环境
        if self.proc is None or self.proc.poll() is not None:
            self._restart()
        marker = f"__DS_{uuid.uuid4().hex[:12]}"
        errf = self.root / f".dserr_{uuid.uuid4().hex[:8]}"
        script = self._wrap(payload, marker, str(errf))

        lines, done = [], threading.Event()

        def reader():
            try:
                for line in self.proc.stdout:
                    if f"{marker}|end" in line:
                        break
                    lines.append(line)
            except Exception:
                pass
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            self.proc.stdin.write(script)
            self.proc.stdin.flush()
        except Exception:
            # 上一条命令里的 exit 可能刚好在写入间隙终止了会话，
            # 这里重建会话重试一次（环境从 .dsenv 恢复），避免偶发 BrokenPipe
            done.set()
            t.join(timeout=2)
            self._restart()
            try:
                lines, done = [], threading.Event()
                t = threading.Thread(target=reader, daemon=True)
                t.start()
                self.proc.stdin.write(script)
                self.proc.stdin.flush()
            except Exception as e:
                done.set()
                return -1, "", f"[会话已断开] {type(e).__name__}: {e}", None, ""

        if not done.wait(timeout):
            self._kill_tree()
            self._restart()
            return -1, "", f"[TIMEOUT] 超过 {timeout}s，已杀进程组并重建会话", None, ""

        t.join(timeout=5)
        out, rc, pwd, env = self._parse("".join(lines), marker)
        err = ""
        try:
            if errf.exists():
                err = errf.read_text(encoding="utf-8", errors="replace")
                errf.unlink()
        except Exception:
            pass

        # 上一次的 exit 可能在本次写入后才真正终止会话（竞态）：
        # 特征是三者全空 + rc=-1，说明命令压根没执行，重建后重试一次是安全的
        if _retry and rc == -1 and not out.strip() and not err.strip():
            self._restart()
            return self._run_local(payload, timeout, _retry=False)
        return rc, out, err, pwd, env

    def _run_docker(self, payload, timeout):
        marker = f"__DS_{uuid.uuid4().hex[:12]}"
        script = (f'cd "{self.cwd}" 2>/dev/null; . /w/.dsenv 2>/dev/null; '
                  + self._wrap(payload, marker))
        try:
            p = subprocess.run(
                ["docker", "exec", "-w", "/w", self.container, "sh", "-c", script],
                capture_output=True, text=True, timeout=timeout)
            raw, err = p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            self._restart()
            return -1, "", f"[TIMEOUT] 超过 {timeout}s，容器已重建（残留进程已清理）", None, ""
        out, rc, pwd, env = self._parse(raw, marker)
        return rc, out, err, pwd, env

    def _kill_tree(self):
        if not self.proc:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _restart(self):
        self.close()
        self._spawn()

    # ---------------- 对外接口 ----------------
    def run(self, cmd, timeout=30):
        with self._lock:                       # 防重入，对应 dsmod 的 PENDING
            if self.mode == "docker":
                rc, out, err, pwd, env = self._run_docker(cmd, timeout)
            else:
                rc, out, err, pwd, env = self._run_local(cmd, timeout)
            if pwd:
                self._save_state(pwd, env or "")
            return self.format(rc, out, err)

    @staticmethod
    def _clip(s, n, tag):
        if len(s) <= n:
            return s
        half = n // 2
        return s[:half] + f"\n...[{tag} 截断 {len(s)-n} 字符]...\n" + s[-half:]

    def header(self):
        if self.mode == "docker":
            return (f"[环境] Docker 容器 · {self.image} · 网络 {self.network} · "
                    f"内存 {self.memory} · 容器内 uid=0")
        return f"[环境] 本机 sh（非容器，无隔离）· uid={os.getuid()}"

    def format(self, rc, out, err):
        parts = [self.header()]
        if out and out.strip():
            parts.append(self._clip(out.rstrip(), self.MAX_OUT, "stdout"))
        if err and err.strip():
            parts.append("[stderr]\n" + self._clip(err.rstrip(), self.MAX_ERR, "stderr"))
        parts.append(f"[exit={rc}]")
        parts.append(f"[cwd] {self.cwd}")
        return "\n".join(parts)

    def reset(self):
        self.cwd = "/w" if self.mode == "docker" else str(self.root)
        try:
            self.env_file.unlink()
        except Exception:
            pass
        self._restart()
        return f"终端会话已重置，cwd={self.cwd}"

    def close(self):
        if self.proc:
            try:
                self._kill_tree()
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None
        if self.container:
            subprocess.run(f"docker rm -f {self.container} >/dev/null 2>&1", shell=True)
            self.container = None

    def info(self):
        extra = ""
        if self.mounts:
            rw = "ro" if all(r == "ro" for _, _, r in self.mounts) else "rw"
            extra = "\n[外链] " + ", ".join(f"{h}→{c}" for h, c, _ in self.mounts) + f"（{rw}）"
        return self.note + extra
