#!/bin/bash
# ETP Explainer Management Script

APP_DIR="$HOME/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations"
LOG_FILE="/tmp/etp-explainer.log"

case "$1" in
    start)
        echo "Starting ETP Explainer..."
        cd "$APP_DIR"
        uv run python scripts/explain_serve.py > "$LOG_FILE" 2>&1 &

        # Wait for server to start (retry health check)
        echo "Waiting for server to start..."
        for i in {1..20}; do
            sleep 1
            if curl -s http://localhost:8765/health > /dev/null 2>&1; then
                echo "✓ Server started successfully"
                echo "  URL: http://rarko1/ or http://10.0.0.4/"
                echo "  Logs: tail -f $LOG_FILE"
                exit 0
            fi
        done

        # If we get here, it failed
        echo "✗ Server failed to start - check logs: tail $LOG_FILE"
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
