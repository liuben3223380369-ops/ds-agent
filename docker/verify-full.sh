#!/bin/sh
# 全量镜像自检 —— 对齐 dsmod-full-stack 的 verify.sh
#
# 用法：
#   docker run --rm ds-agent-full:3.20 verify-full
# 或容器内直接：
#   verify-full
#
# 输出 OK / MISSING，最后给汇总。

PASS=0
MISS=0
MISSING=""

chk() {
    # $1 = 命令名, $2 = 显示名（可选）
    name="${2:-$1}"
    # 用 env -i 验证：不依赖 profile 也能找到（wrapper 落地方式的关键检验）
    if command -v "$1" >/dev/null 2>&1 || env -i sh -c "command -v $1" >/dev/null 2>&1; then
        printf '  OK       %s\n' "$name"
        PASS=$((PASS+1))
    else
        printf '  MISSING  %s\n' "$name"
        MISS=$((MISS+1))
        MISSING="$MISSING $name"
    fi
}

echo "== 基础 / Shell =="
for c in bash rg fd jq vim tmux git curl wget unzip zip less file tree; do chk "$c"; done

echo
echo "== 语言运行时 =="
for c in python3 node npm go rustc cargo clang gcc g++ make; do chk "$c"; done
for c in java javac kotlinc groovy scala scalac clojure php ruby perl lua5.4 luajit; do chk "$c"; done
for c in elixir erl R dotnet ghc gradle mvn; do chk "$c"; done

echo
echo "== 数据库 / 服务 =="
for c in sqlite3 psql mysql redis-cli nginx; do chk "$c"; done

echo
echo "== 逆向工具链 =="
for c in jadx apktool aapt2 zipalign dexdump r8 d8 uber-apk-signer apkid; do chk "$c"; done

echo
echo "== ds-agent 自身 =="
if python3 -c 'import openai' 2>/dev/null; then
    printf '  OK       openai SDK\n'; PASS=$((PASS+1))
else
    printf '  MISSING  openai SDK\n'; MISS=$((MISS+1)); MISSING="$MISSING openai"
fi

echo
echo "================ 汇总 ================"
echo "  OK: $PASS    MISSING: $MISS"
if [ "$MISS" -gt 0 ]; then
    echo "  缺失项：$MISSING"
    echo
    echo "  说明："
    echo "   · 逆向工具（apktool/aapt2/r8 等）依赖构建时的网络下载，失败会 skip"
    echo "   · 重新构建并把下载失败的那一步日志保留下来即可定位"
    exit 1
fi
echo "  全部就绪"
