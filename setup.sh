#!/bin/sh
# ds-agent 一键部署脚本
# 适用于 Termux(Android) / Linux / macOS / WSL
#
# 用法：
#   sh setup.sh
# 或一步到位（非交互）：
#   curl -fsSL https://raw.githubusercontent.com/liuben3223380369-ops/ds-agent/main/setup.sh | sh
#
# 幂等：重复执行不会破坏已有配置。

set -e

REPO="https://github.com/liuben3223380369-ops/ds-agent.git"
DIR="${DS_DIR:-$HOME/ds-agent}"
PY=python3

echo "== ds-agent 部署 =="

# ---------- 1. 获取代码 ----------
if [ -d "$DIR/.git" ]; then
    echo "[1/5] 已存在仓库，跳过克隆"
elif [ -f "ds_agent_pro.py" ]; then
    DIR="$(pwd)"; echo "[1/5] 已在项目目录中"
else
    echo "[1/5] 克隆到 $DIR"
    command -v git >/dev/null 2>&1 || { echo "❌ 未找到 git，请先安装"; exit 1; }
    git clone "$REPO" "$DIR"
fi
cd "$DIR"

# ---------- 2. 检查 Python ----------
echo "[2/5] 检查 Python"
if ! command -v $PY >/dev/null 2>&1; then
    echo "未找到 python3，尝试安装..."
    if command -v pkg >/dev/null 2>&1; then          # Termux
        pkg install -y python git
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y python3 python3-pip git
    elif command -v brew >/dev/null 2>&1; then
        brew install python3 git
    else
        echo "❌ 请手动安装 Python 3.8+ 与 git"; exit 1
    fi
fi
$PY -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' \
    || { echo "❌ 需要 Python 3.8+，当前：$($PY --version)"; exit 1; }
echo "  $($PY --version)"

# ---------- 3. 安装依赖 ----------
echo "[3/5] 安装依赖"
# Termux / 部分发行版禁止直接 pip 装到系统，优先尝试，失败则建虚拟环境
if ! $PY -m pip install -q -r requirements.txt 2>/dev/null; then
    echo "  系统 pip 受限，改用虚拟环境 .venv"
    $PY -m venv .venv || { $PY -m pip install -q virtualenv && $PY -m virtualenv .venv; }
    . .venv/bin/activate
    $PY -m pip install -q -r requirements.txt
    echo "  已创建 .venv，后续请先执行： source $DIR/.venv/bin/activate"
fi
echo "  openai SDK 就绪"

# ---------- 4. 手机存储授权 ----------
echo "[4/5] 环境检查"
if [ -d /data/data/com.termux ] && [ ! -d /sdcard ]; then
    echo "  ⚠️ 检测到 Termux 但 /sdcard 不可见"
    echo "     请执行： termux-setup-storage   然后重新运行本脚本"
fi

# ---------- 5. 生成配置 ----------
echo "[5/5] 生成配置"
# 把 example 里的相对路径换成绝对路径，避免在其他目录下运行时找不到 server
if [ ! -f mcp.json ]; then
    $PY - "$DIR" <<'PYEOF'
import json, sys, os
d = sys.argv[1]
cfg = json.load(open("mcp.json.example"))
for spec in cfg.get("mcpServers", {}).values():
    args = spec.get("args") or []
    if args and not os.path.isabs(args[0]):
        spec["args"] = [os.path.join(d, args[0])] + args[1:]
json.dump(cfg, open("mcp.json", "w"), indent=2, ensure_ascii=False)
PYEOF
    echo "  已生成 mcp.json（server 路径已改写为绝对路径）"
else
    echo "  mcp.json 已存在，保留"
fi
mkdir -p workspace

cat > .env.example <<'EOF'
# 复制为 .env 后填入真实 key，或直接在 shell 里 export
export DEEPSEEK_API_KEY="sk-你的key"
# 可选：省钱用 flash
# export DS_MODEL="deepseek-v4-flash"
# 可选：手机上建议只读
# export DS_FS_READONLY=1
# export DS_FS_ROOTS="/sdcard/Download"
EOF

echo
echo "============ 部署完成 ============"
echo "项目目录: $DIR"
echo
echo "下一步："
echo "  1) 设置 API Key（二选一）"
echo "     export DEEPSEEK_API_KEY='sk-xxx'"
echo "     或: cp .env.example .env && 编辑 .env && . ./.env"
echo
echo "  2) 试跑（不消耗 token，只测 MCP 通道）"
echo "     python3 ds_agent_pro.py --mcp-only"
echo
echo "  3) 正式使用"
echo "     python3 ds_agent_pro.py '写一个快速排序并验证正确性'"
echo
echo "  4) 常用维护"
echo "     python3 ds_agent_pro.py --snapshots      # 看快照"
echo "     python3 ds_agent_pro.py --rollback 3     # 回退 3 步"
echo
if [ -d /data/data/com.termux ]; then
    echo "手机提示：Termux 无 Docker，会回退本地执行，建议开 DS_FS_READONLY=1"
fi
