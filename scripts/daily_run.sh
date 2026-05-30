#!/usr/bin/env bash
# 每日自动跑 collect + analyze + digest。由 launchd 每天 06:00 调用。
# - 自动启停 colima VM + RSSHub 容器（用于 X/Twitter 桥接）
# - 运行期间阻止 Mac 进入睡眠（caffeinate）
# - 失败自动记录日志，不影响下次运行

# ─── 环境 ───
# launchd 启动时 PATH 很短，需要手动补全常用路径
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

cd "$(dirname "$0")/.."

# 优先用项目内 .venv；否则用 PATH 上的 python3
if [ -f ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3)"
fi

mkdir -p data/logs
TODAY=$(date +%Y-%m-%d)
LOG="data/logs/${TODAY}.log"
STATUS_FILE="data/logs/.last_run_status"

# ─── 防止重复运行 ───
# 如果今天已经成功跑过，跳过（防止开机补跑时重复）
if grep -q "^success ${TODAY}" "$STATUS_FILE" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] Already ran successfully today (${TODAY}), skipping." >> "$LOG"
    exit 0
fi

# ─── 防止并发 ───
LOCK="/tmp/cuttingedgeai_daily.lock"
if [ -f "$LOCK" ]; then
    echo "[$(date '+%H:%M:%S')] Another instance is running (lock: $LOCK), skipping." >> "$LOG"
    exit 0
fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

# 从 .env 加载环境变量
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# ─── 防止运行期间息眠 ───
# caffeinate -i：阻止系统空闲睡眠；-t 10800：最多保持 3 小时（安全上限）
caffeinate -i -t 10800 &
CAFE_PID=$!

# ─── 服务启停 ───
HAS_COLIMA=0
command -v colima >/dev/null 2>&1 && HAS_COLIMA=1

start_services() {
    [ "$HAS_COLIMA" -eq 1 ] || return 0
    if ! colima status >/dev/null 2>&1; then
        echo "[services] Starting colima VM…"
        colima start --cpu 1 --memory 2 --disk 10 >/dev/null 2>&1 || true
    fi
    for _ in 1 2 3 4 5; do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    docker info >/dev/null 2>&1 || { echo "[services] Docker not ready, skipping RSSHub"; return 0; }

    if ! docker ps --format '{{.Names}}' | grep -q '^rsshub$'; then
        docker ps -a --format '{{.Names}}' | grep -q '^rsshub$' && \
            docker rm -f rsshub >/dev/null 2>&1 || true
        docker run -d --name rsshub \
            -p 1200:1200 \
            ${TWITTER_AUTH_TOKEN:+-e TWITTER_AUTH_TOKEN="$TWITTER_AUTH_TOKEN"} \
            diygod/rsshub >/dev/null 2>&1 || true
    fi
    sleep 10  # RSSHub 冷启动预热
    echo "[services] RSSHub ready."
}

stop_services() {
    kill "$CAFE_PID" 2>/dev/null || true  # 释放 caffeinate
    [ "$HAS_COLIMA" -eq 1 ] || return 0
    docker stop rsshub >/dev/null 2>&1 || true
    colima stop >/dev/null 2>&1 || true
    echo "[services] Stopped."
}

trap stop_services EXIT

# ─── 主流程 ───
{
    echo ""
    echo "════════════════════════════════════════"
    echo "  AI Daily Pipeline — $(date '+%Y-%m-%d %H:%M:%S')"
    echo "════════════════════════════════════════"

    if [ "$HAS_COLIMA" -eq 1 ]; then
        start_services
    else
        echo "[info] colima not found, RSSHub (Twitter) skipped."
    fi

    echo "[pipeline] Starting…"
    if "$PY" -m src.main run; then
        echo "[pipeline] ✓ Completed at $(date '+%H:%M:%S')"
        echo "success $(date '+%Y-%m-%d %H:%M:%S')" > "$STATUS_FILE"
    else
        EXIT_CODE=$?
        echo "[pipeline] ✗ Failed (exit $EXIT_CODE) at $(date '+%H:%M:%S')"
        echo "failed $(date '+%Y-%m-%d %H:%M:%S') exit=$EXIT_CODE" > "$STATUS_FILE"
    fi
    echo "════════════════════════════════════════"
} >> "$LOG" 2>&1
