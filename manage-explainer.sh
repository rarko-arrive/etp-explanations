#!/bin/bash
# ETP Explainer Management Script (bare-metal VM: uvicorn on EXPLAIN_PORT behind nginx)
#
#   ./manage-explainer.sh {start|stop|restart|status|logs|ensure|lake-updated}
#
# ensure        start only if /health is not answering (etp-lake lake-sync on_sync hook)
# lake-updated  drop cached HTML + restart on the new lake.env (lake-sync on_update hook)
#
# Only touches the server it started (pid file / EXPLAIN_PORT) — never the
# `make share` instance.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${EXPLAIN_LOG_FILE:-/tmp/etp-explainer.log}"
PID_FILE="${LOG_FILE}.pid"

# Non-login SSH / nohup / systemd often omit ~/.local/bin (where uv lives on Azure ML).
export PATH="${HOME}/.local/bin:${PATH}"

load_app_env() {
    cd "$APP_DIR" || exit 1
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        . ./.env
        set +a
    fi
    # Expand a leading ~ only. (The old `== ~/*` test was itself tilde-expanded,
    # so absolute paths matched and lost two characters → /home/azureuser/ome/….)
    if [ -n "${UV_PROJECT_ENVIRONMENT:-}" ]; then
        UV_PROJECT_ENVIRONMENT="$(bash scripts/expand_user_path.sh "$UV_PROJECT_ENVIRONMENT")"
        export UV_PROJECT_ENVIRONMENT
    fi
    PORT="${EXPLAIN_PORT:-8765}"
    CACHE_DIR="$(bash scripts/expand_user_path.sh "${EXPLAIN_CACHE_DIR:-data/explain-shipments}")"
}

python_cmd() {
    local venv_py="${UV_PROJECT_ENVIRONMENT:-$APP_DIR/.venv}/bin/python"
    if [ -x "$venv_py" ]; then
        echo "$venv_py"
    elif command -v uv >/dev/null 2>&1; then
        echo "uv run python"
    else
        echo ""
    fi
}

healthy() {
    curl -s -m 5 -o /dev/null "http://127.0.0.1:${PORT}/health"
}

server_pid() {  # pid from our pid file if alive, else the explain_serve listener on PORT
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"; return
    fi
    pid="$(ss -ltnpH "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
    if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q explain_serve; then
        echo "$pid"
    fi
}

ensure_cache_dir() {  # /mnt is wiped on stop/start and recreated root-owned
    mkdir -p "$CACHE_DIR" 2>/dev/null || sudo -n mkdir -p "$CACHE_DIR" 2>/dev/null
    if [ ! -w "$CACHE_DIR" ]; then
        sudo -n chown "$(id -un):$(id -gn)" "$CACHE_DIR" 2>/dev/null \
            || echo "⚠ cache dir not writable: $CACHE_DIR (sudo chown $(id -un) $CACHE_DIR)"
    fi
}

do_start() {
    echo "Starting ETP Explainer on :${PORT}..."
    if healthy; then
        echo "✓ already running on :${PORT} (pid $(server_pid))"
        return 0
    fi
    run="$(python_cmd)"
    if [ -z "$run" ]; then
        echo "✗ No interpreter — run: make install  (needs uv in PATH or UV_PROJECT_ENVIRONMENT venv)"
        return 1
    fi
    ensure_cache_dir
    # shellcheck disable=SC2086
    nohup $run scripts/explain_serve.py --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    echo "Waiting for server to start..."
    for _ in $(seq 1 60); do
        sleep 1
        if healthy; then
            echo "✓ Server started successfully (pid $(cat "$PID_FILE"))"
            echo "  Local: http://127.0.0.1:${PORT}/"
            echo "  Logs: tail -f $LOG_FILE"
            return 0
        fi
    done

    echo "✗ Server failed to start - check logs: tail $LOG_FILE"
    tail -20 "$LOG_FILE" 2>/dev/null || true
    return 1
}

do_stop() {
    echo "Stopping ETP Explainer on :${PORT}..."
    local pid
    pid="$(server_pid)"
    if [ -z "$pid" ]; then
        echo "✓ not running"
        rm -f "$PID_FILE"
        return 0
    fi
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "✗ Server still running, force killing pid $pid..."
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    echo "✓ Server stopped"
}

load_app_env

case "$1" in
    start) do_start || exit 1 ;;
    stop) do_stop ;;
    restart)
        echo "Restarting ETP Explainer..."
        do_stop
        do_start || exit 1
        ;;
    ensure)
        if healthy; then
            echo "✓ ETP Explainer healthy on :${PORT}"
        else
            do_start || exit 1
        fi
        ;;
    lake-updated)
        echo "Lake updated (version ${LAKE_VERSION:-?}, status ${LAKE_STATUS:-?}) — clearing $CACHE_DIR/explain-*.html"
        rm -f "$CACHE_DIR"/explain-*.html
        do_stop
        do_start || exit 1
        ;;
    status)
        pid="$(server_pid)"
        if [ -n "$pid" ]; then
            echo "✓ ETP Explainer is running (pid $pid, :${PORT}, health $(healthy && echo ok || echo FAIL))"
        else
            echo "✗ ETP Explainer is not running"
        fi
        ;;
    logs)
        tail -f "$LOG_FILE"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|ensure|lake-updated}"
        echo ""
        echo "Commands:"
        echo "  start         - Start the server"
        echo "  stop          - Stop the server"
        echo "  restart       - Restart the server"
        echo "  status        - Check if server is running"
        echo "  logs          - View server logs (Ctrl+C to exit)"
        echo "  ensure        - Start only if not healthy (lake-sync on_sync hook)"
        echo "  lake-updated  - Clear HTML cache + restart (lake-sync on_update hook)"
        exit 1
        ;;
esac

exit 0
