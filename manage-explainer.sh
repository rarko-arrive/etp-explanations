#!/bin/bash
# ETP Explainer Management Script

APP_DIR="$HOME/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations"
LOG_FILE="/tmp/etp-explainer.log"

# Non-login SSH / nohup often omit ~/.local/bin (where uv lives on Azure ML).
export PATH="${HOME}/.local/bin:${PATH}"

load_app_env() {
    cd "$APP_DIR" || exit 1
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        . ./.env
        set +a
    fi
    if [[ "${UV_PROJECT_ENVIRONMENT:-}" == ~/* ]]; then
        UV_PROJECT_ENVIRONMENT="${HOME}/${UV_PROJECT_ENVIRONMENT:2}"
    fi
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

case "$1" in
    start)
        echo "Starting ETP Explainer..."
        load_app_env
        run="$(python_cmd)"
        if [ -z "$run" ]; then
            echo "✗ No interpreter — run: make install  (needs uv in PATH or UV_PROJECT_ENVIRONMENT venv)"
            exit 1
        fi
        # shellcheck disable=SC2086
        nohup $run scripts/explain_serve.py >>"$LOG_FILE" 2>&1 &
        echo $! > "${LOG_FILE}.pid"

        echo "Waiting for server to start..."
        for i in {1..30}; do
            sleep 1
            if curl -s http://localhost:8765/health > /dev/null 2>&1; then
                echo "✓ Server started successfully"
                echo "  Local: http://127.0.0.1:8765/"
                echo "  Logs: tail -f $LOG_FILE"
                exit 0
            fi
        done

        echo "✗ Server failed to start - check logs: tail $LOG_FILE"
        tail -20 "$LOG_FILE" 2>/dev/null || true
        exit 1
        ;;

    stop)
        echo "Stopping ETP Explainer..."
        pkill -f explain_serve
        sleep 2
        if pgrep -f explain_serve > /dev/null; then
            echo "✗ Server still running, force killing..."
            pkill -9 -f explain_serve
        else
            echo "✓ Server stopped"
        fi
        ;;

    restart)
        echo "Restarting ETP Explainer..."
        $0 stop
        sleep 2
        $0 start
        ;;

    status)
        if pgrep -f explain_serve > /dev/null; then
            echo "✓ ETP Explainer is running"
            ps aux | grep '[p]ython.*explain_serve' | head -1
        else
            echo "✗ ETP Explainer is not running"
        fi
        ;;

    logs)
        tail -f "$LOG_FILE"
        ;;

    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        echo ""
        echo "Commands:"
        echo "  start   - Start the server"
        echo "  stop    - Stop the server"
        echo "  restart - Restart the server"
        echo "  status  - Check if server is running"
        echo "  logs    - View server logs (Ctrl+C to exit)"
        exit 1
        ;;
esac

exit 0
