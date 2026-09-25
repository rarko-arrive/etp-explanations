#!/usr/bin/env bash
# share_explainer.sh — run the ETP explainer locally and expose it on a public
# HTTPS URL (Cloudflare quick tunnel) protected by the app's HTTP basic auth.
#
# Works on macOS (bash 3.2) and Linux. Everything is driven from .env in the
# repo root; nothing is hard-coded to a machine.
#
#   scripts/share_explainer.sh check              # preflight only, no changes
#   scripts/share_explainer.sh start              # server + tunnel, prints share URL
#   scripts/share_explainer.sh start --no-tunnel  # server only (LAN / VPN / nginx)
#   scripts/share_explainer.sh start --port 8799  # override EXPLAIN_PORT
#   scripts/share_explainer.sh status | url | logs [server|tunnel] | stop | restart
#   scripts/share_explainer.sh start --show-password   # echo AUTH_PASSWORD in summary
#
# Optional stable hostname (instead of a random *.trycloudflare.com URL):
#   set SHARE_TUNNEL_TOKEN and SHARE_PUBLIC_URL in .env (Cloudflare Zero Trust
#   → Networks → Tunnels → create tunnel → copy token; public hostname must
#   route to http://127.0.0.1:<EXPLAIN_PORT>).
#
# Runtime state lives in .run/ (gitignored): pids, logs, share.url.

set -euo pipefail

# ----------------------------------------------------------------------------
# locate repo, runtime dir
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

RUN_DIR="$REPO_ROOT/.run"
SERVER_PID_FILE="$RUN_DIR/server.pid"
TUNNEL_PID_FILE="$RUN_DIR/tunnel.pid"
SERVER_LOG="$RUN_DIR/server.log"
TUNNEL_LOG="$RUN_DIR/tunnel.log"
URL_FILE="$RUN_DIR/share.url"
mkdir -p "$RUN_DIR"

OS="$(uname -s)"

# ----------------------------------------------------------------------------
# output helpers
# ----------------------------------------------------------------------------
if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_FAIL=""; C_DIM=""; C_BOLD=""; C_OFF=""
fi
ts() { date '+%H:%M:%S'; }
ok()   { printf '%s %s[ ok ]%s %s\n'   "$(ts)" "$C_OK"   "$C_OFF" "$*"; }
info() { printf '%s %s[info]%s %s\n'   "$(ts)" "$C_DIM"  "$C_OFF" "$*"; }
warn() { printf '%s %s[warn]%s %s\n'   "$(ts)" "$C_WARN" "$C_OFF" "$*" >&2; }
fail() { printf '%s %s[FAIL]%s %s\n'   "$(ts)" "$C_FAIL" "$C_OFF" "$*" >&2; }
# remediation block: fail <headline> then fix <line> [<line> ...]
fix() {
    printf '%s        %sfix:%s %s\n' "$(ts)" "$C_BOLD" "$C_OFF" "$1" >&2
    shift
    for line in "$@"; do printf '%s             %s\n' "$(ts)" "$line" >&2; done
}
die() { fail "$@"; exit 1; }

tail_log() {  # tail_log <file> <n>
    if [ -s "$1" ]; then
        printf '%s--- last %s lines of %s ---%s\n' "$C_DIM" "$2" "$1" "$C_OFF" >&2
        tail -n "$2" "$1" >&2
        printf '%s--- end ---%s\n' "$C_DIM" "$C_OFF" >&2
    fi
}

# ----------------------------------------------------------------------------
# .env loading  (~ expansion, strip quotes; env from the caller does NOT win —
# .env is the single source of truth, --port is the only CLI override)
# ----------------------------------------------------------------------------
load_env() {
    if [ ! -f .env ]; then
        fail ".env not found in $REPO_ROOT"
        fix "create it from the template, then set DQT_DATA_DIR and AUTH_PASSWORD:" \
            "cp .env.example .env" \
            "\$EDITOR .env"
        exit 1
    fi
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
    DQT_DATA_DIR="$(bash scripts/expand_user_path.sh "${DQT_DATA_DIR:-data}")"
    [ -n "${UV_PROJECT_ENVIRONMENT:-}" ] && UV_PROJECT_ENVIRONMENT="$(bash scripts/expand_user_path.sh "$UV_PROJECT_ENVIRONMENT")"
    export DQT_DATA_DIR UV_PROJECT_ENVIRONMENT
    case "$DQT_DATA_DIR" in /*) ;; *) DQT_DATA_DIR="$REPO_ROOT/$DQT_DATA_DIR" ;; esac
    unset VIRTUAL_ENV   # avoid uv "mismatched VIRTUAL_ENV" warning
    EXPLAIN_HOST="${EXPLAIN_HOST:-127.0.0.1}"
    EXPLAIN_PORT="${PORT_OVERRIDE:-${EXPLAIN_PORT:-8765}}"
    AUTH_ENABLED="${AUTH_ENABLED:-1}"
    AUTH_USERNAME="${AUTH_USERNAME:-etp}"
    AUTH_PASSWORD="${AUTH_PASSWORD:-}"
    export EXPLAIN_HOST EXPLAIN_PORT AUTH_ENABLED AUTH_USERNAME AUTH_PASSWORD
    LOCAL_URL="http://127.0.0.1:$EXPLAIN_PORT"
}

# ----------------------------------------------------------------------------
# process helpers (portable: no setsid / timeout / pgrep -F)
# ----------------------------------------------------------------------------
pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
read_pid()  { [ -f "$1" ] && cat "$1" 2>/dev/null || true; }

port_listener_pid() {  # prints pid listening on TCP port, or nothing
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n1 || true
    elif command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print $NF}' | sed -nE 's/.*pid=([0-9]+).*/\1/p' | head -n1 || true
    fi
}

stop_pid() {  # stop_pid <name> <pidfile>
    local name="$1" file="$2" pid
    pid="$(read_pid "$file")"
    if pid_alive "$pid"; then
        kill "$pid" 2>/dev/null || true
        local i=0
        while pid_alive "$pid" && [ $i -lt 25 ]; do sleep 0.2; i=$((i+1)); done
        if pid_alive "$pid"; then
            warn "$name (pid $pid) ignored SIGTERM; sending SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        fi
        ok "$name stopped (pid $pid)"
    else
        info "$name not running"
    fi
    rm -f "$file"
}

http_code() {  # http_code <url> [curl args...]  → prints 3-digit status code (000 on connection failure)
    local url="$1" code; shift
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@" "$url" 2>/dev/null || true)"
    printf '%s' "${code:-000}"
}

# basic-auth GET without leaking the password into `ps` (config via stdin)
http_code_auth() {  # http_code_auth <url>
    local code
    code="$(printf 'user = "%s:%s"\n' "$AUTH_USERNAME" "$AUTH_PASSWORD" \
        | curl -s -o /dev/null -w '%{http_code}' --max-time 120 -K - "$1" 2>/dev/null || true)"
    printf '%s' "${code:-000}"
}

# public edge can flap for a few seconds after start — retry before judging
http_code_retry() {  # http_code_retry <url> <attempts> <sleep>
    local code="000" i=0
    while [ $i -lt "$2" ]; do
        code="$(http_code "$1")"
        [ "$code" = "200" ] || [ "$code" = "401" ] && break
        sleep "$3"; i=$((i+1))
    done
    printf '%s' "$code"
}

# ----------------------------------------------------------------------------
# preflight
# ----------------------------------------------------------------------------
cloudflared_install_hint() {
    case "$OS" in
        Darwin) fix "install cloudflared:" "brew install cloudflared" ;;
        Linux)
            fix "install cloudflared (single static binary):" \
                "curl -fsSL -o /tmp/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" \
                "chmod +x /tmp/cloudflared && mkdir -p ~/bin && mv /tmp/cloudflared ~/bin/cloudflared" \
                "export PATH=\"\$HOME/bin:\$PATH\"   # add to ~/.bashrc" \
                "(or: scripts/share_explainer.sh start --no-tunnel  and share over VPN / nginx instead)" ;;
        *) fix "install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" ;;
    esac
}

preflight() {
    local errors=0

    # uv
    if command -v uv >/dev/null 2>&1; then
        ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
    else
        fail "uv not on PATH"
        fix "install uv:" "curl -LsSf https://astral.sh/uv/install.sh | sh    # or: brew install uv"
        errors=$((errors+1))
    fi

    # python env
    if command -v uv >/dev/null 2>&1; then
        if uv run --no-sync python -c "import fastapi, uvicorn, bcrypt, app.explain.server" >/dev/null 2>"$RUN_DIR/preflight-import.err"; then
            ok "python env importable (${UV_PROJECT_ENVIRONMENT:-.venv})"
        else
            fail "python env missing or stale (cannot import fastapi/uvicorn/bcrypt/app.explain.server)"
            tail_log "$RUN_DIR/preflight-import.err" 8
            fix "rebuild the environment (needs GitHub SSH for the private arriveds dep):" \
                "eval \"\$(ssh-agent -s)\" && ssh-add     # macOS: ssh-add --apple-use-keychain ~/.ssh/id_ed25519_github" \
                "make install"
            errors=$((errors+1))
        fi
    fi

    # data
    if [ -d "$DQT_DATA_DIR/etp_lake" ] && [ -f "$DQT_DATA_DIR/etp/etp-slider-history.parquet" ]; then
        ok "data dir $DQT_DATA_DIR (etp_lake/ + etp/etp-slider-history.parquet present)"
        [ -d "$DQT_DATA_DIR/etp_lake/mart/feature_deltas" ] || warn "etp_lake/mart/feature_deltas missing — material-change ledgers will be empty; rebuild the lake in etp-lake"
        ls "$DQT_DATA_DIR"/etp/executive-*/labeled-cohort.parquet >/dev/null 2>&1 \
            || warn "no etp/executive-*/labeled-cohort.parquet — rank-based lookup unavailable, loadnumber search still works"
    else
        fail "DQT_DATA_DIR=$DQT_DATA_DIR is missing etp_lake/ and/or etp/etp-slider-history.parquet"
        fix "point .env at a built lake (etp-lake or etp-dqt data dir), e.g.:" \
            "DQT_DATA_DIR=~/Git/Projects/etp/etp-lake/data          # in .env" \
            "or build it:  cd ../etp-lake && make etp-lake SKIP_PULL=1" \
            "or sync:      ln -s ../etp-dqt/data data"
        errors=$((errors+1))
    fi

    # auth
    if [ "$AUTH_ENABLED" != "1" ] && [ "$AUTH_ENABLED" != "true" ]; then
        if [ "$NO_TUNNEL" = 1 ]; then
            warn "AUTH_ENABLED=$AUTH_ENABLED — server will be open to anyone who can reach it"
        else
            fail "AUTH_ENABLED=$AUTH_ENABLED but a public tunnel was requested"
            fix "set in .env:" "AUTH_ENABLED=1"
            errors=$((errors+1))
        fi
    elif [ -z "$AUTH_PASSWORD" ] || [ "$AUTH_PASSWORD" = "changeme" ]; then
        fail "AUTH_PASSWORD is ${AUTH_PASSWORD:-empty} — refusing to share with a default/empty password"
        fix "set a real password in .env (bcrypt hash recommended, keep the \$ in single quotes):" \
            "uv run python -c \"from app.explain.auth import hash_password; print(hash_password('YOUR-PASSWORD'))\"" \
            "AUTH_PASSWORD='\$2b\$12\$...'"
        errors=$((errors+1))
    else
        case "$AUTH_PASSWORD" in
            '$2a$'*|'$2b$'*|'$2y$'*) ok "basic auth enabled for user '$AUTH_USERNAME' (bcrypt hash)" ;;
            *) ok "basic auth enabled for user '$AUTH_USERNAME' (plaintext password in .env)"
               info "tip: store a bcrypt hash instead — see .env.example" ;;
        esac
    fi

    # bind host
    if [ "$NO_TUNNEL" = 0 ] && [ "$EXPLAIN_HOST" != "127.0.0.1" ] && [ "$EXPLAIN_HOST" != "localhost" ]; then
        warn "EXPLAIN_HOST=$EXPLAIN_HOST — the tunnel only needs 127.0.0.1; binding wider also exposes the port on your LAN"
    fi

    # port
    local lp; lp="$(port_listener_pid "$EXPLAIN_PORT")"
    local ours; ours="$(read_pid "$SERVER_PID_FILE")"
    if [ -n "$lp" ] && ! pid_alive "$ours"; then
        fail "port $EXPLAIN_PORT already in use by pid $lp (not started by this script)"
        fix "stop it, or pick another port:" \
            "kill $lp" \
            "scripts/share_explainer.sh start --port 8799" \
            "(on the VM the nginx-managed instance is controlled by ./manage-explainer.sh stop)"
        errors=$((errors+1))
    elif [ -n "$lp" ]; then
        info "port $EXPLAIN_PORT held by our own server (pid $ours)"
    else
        ok "port $EXPLAIN_PORT free"
    fi

    # cloudflared
    if [ "$NO_TUNNEL" = 0 ]; then
        if command -v cloudflared >/dev/null 2>&1; then
            ok "cloudflared $(cloudflared --version 2>/dev/null | awk '{print $3}')"
        else
            fail "cloudflared not on PATH"
            cloudflared_install_hint
            errors=$((errors+1))
        fi
        if [ -n "${SHARE_TUNNEL_TOKEN:-}" ] && [ -z "${SHARE_PUBLIC_URL:-}" ]; then
            fail "SHARE_TUNNEL_TOKEN set but SHARE_PUBLIC_URL missing"
            fix "add the tunnel's public hostname to .env:" "SHARE_PUBLIC_URL=https://etp.example.com"
            errors=$((errors+1))
        fi
    fi

    if [ $errors -gt 0 ]; then
        die "preflight failed with $errors error(s) — fix the items above and re-run"
    fi
    ok "preflight passed"
}

# ----------------------------------------------------------------------------
# server
# ----------------------------------------------------------------------------
start_server() {
    local pid; pid="$(read_pid "$SERVER_PID_FILE")"
    if pid_alive "$pid"; then
        info "server already running (pid $pid)"; return 0
    fi
    : > "$SERVER_LOG"
    info "starting server on $LOCAL_URL (log: $SERVER_LOG)"
    nohup uv run --no-sync python scripts/explain_serve.py --host 127.0.0.1 --port "$EXPLAIN_PORT" \
        >>"$SERVER_LOG" 2>&1 &
    pid=$!
    echo "$pid" > "$SERVER_PID_FILE"

    # first import of polars/arriveds can take a while on a cold cache
    local i=0 code=000
    while [ $i -lt 90 ]; do
        code="$(http_code "$LOCAL_URL/health")"
        [ "$code" = "200" ] && break
        if ! pid_alive "$pid"; then
            fail "server process exited before becoming healthy"
            tail_log "$SERVER_LOG" 40
            fix "read the traceback above; common causes:" \
                "ModuleNotFoundError        → make install" \
                "DQT_DATA_DIR / lake errors → check DQT_DATA_DIR in .env, run: scripts/share_explainer.sh check" \
                "address already in use     → scripts/share_explainer.sh start --port 8799"
            rm -f "$SERVER_PID_FILE"; exit 1
        fi
        sleep 1; i=$((i+1))
    done
    if [ "$code" != "200" ]; then
        fail "server did not answer /health within 90s (pid $pid still running)"
        tail_log "$SERVER_LOG" 40
        fix "watch it come up, or stop and retry:" "scripts/share_explainer.sh logs server" "scripts/share_explainer.sh stop"
        exit 1
    fi
    ok "server healthy: $LOCAL_URL/health (pid $pid, ${i}s)"

    # auth smoke tests against the real process
    if [ "$AUTH_ENABLED" = "1" ] || [ "$AUTH_ENABLED" = "true" ]; then
        code="$(http_code "$LOCAL_URL/")"
        if [ "$code" != "401" ]; then
            fail "expected 401 for unauthenticated GET / but got $code — auth is NOT protecting the app"
            tail_log "$SERVER_LOG" 20
            fix "check AUTH_ENABLED / AUTH_PASSWORD in .env are the values the server loaded (see log header above)"
            exit 1
        fi
        code="$(http_code_auth "$LOCAL_URL/")"
        if [ "$code" != "200" ]; then
            fail "credentials from .env were rejected by the server (GET / → $code)"
            fix "if AUTH_PASSWORD is a bcrypt hash make sure it is single-quoted in .env so the \$ signs survive;" \
                "regenerate: uv run python -c \"from app.explain.auth import hash_password; print(hash_password('...'))\""
            exit 1
        fi
        ok "basic auth verified (anonymous → 401, $AUTH_USERNAME → 200)"
    fi
}

# ----------------------------------------------------------------------------
# tunnel
# ----------------------------------------------------------------------------
start_tunnel() {
    local pid; pid="$(read_pid "$TUNNEL_PID_FILE")"
    if pid_alive "$pid" && [ -s "$URL_FILE" ]; then
        info "tunnel already running (pid $pid) → $(cat "$URL_FILE")"; return 0
    fi
    : > "$TUNNEL_LOG"; rm -f "$URL_FILE"

    if [ -n "${SHARE_TUNNEL_TOKEN:-}" ]; then
        info "starting named cloudflare tunnel (log: $TUNNEL_LOG)"
        nohup cloudflared tunnel --no-autoupdate run --token "$SHARE_TUNNEL_TOKEN" >>"$TUNNEL_LOG" 2>&1 &
        pid=$!; echo "$pid" > "$TUNNEL_PID_FILE"
        local i=0
        while [ $i -lt 45 ]; do
            grep -q "Registered tunnel connection" "$TUNNEL_LOG" 2>/dev/null && break
            pid_alive "$pid" || break
            sleep 1; i=$((i+1))
        done
        if ! grep -q "Registered tunnel connection" "$TUNNEL_LOG" 2>/dev/null; then
            fail "named tunnel did not register within 45s"
            tail_log "$TUNNEL_LOG" 30
            fix "check the token and that the tunnel's public hostname routes to http://127.0.0.1:$EXPLAIN_PORT;" \
                "fall back to a quick tunnel by unsetting SHARE_TUNNEL_TOKEN in .env"
            exit 1
        fi
        printf '%s\n' "${SHARE_PUBLIC_URL%/}" > "$URL_FILE"
    else
        info "starting cloudflare quick tunnel → $LOCAL_URL (log: $TUNNEL_LOG)"
        nohup cloudflared tunnel --no-autoupdate --url "$LOCAL_URL" >>"$TUNNEL_LOG" 2>&1 &
        pid=$!; echo "$pid" > "$TUNNEL_PID_FILE"
        local i=0 url=""
        while [ $i -lt 60 ]; do
            url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -n1 || true)"
            [ -n "$url" ] && break
            if ! pid_alive "$pid"; then break; fi
            sleep 1; i=$((i+1))
        done
        if [ -z "$url" ]; then
            fail "cloudflared did not print a trycloudflare.com URL within 60s"
            tail_log "$TUNNEL_LOG" 30
            fix "usual causes: outbound 443/7844 to Cloudflare blocked, or cloudflared too old." \
                "try by hand:  cloudflared tunnel --url $LOCAL_URL" \
                "update:       brew upgrade cloudflared   (macOS)" \
                "no-tunnel:    scripts/share_explainer.sh start --no-tunnel   and share via VPN / nginx (see DEPLOYMENT.md)"
            stop_pid "tunnel" "$TUNNEL_PID_FILE"; exit 1
        fi
        printf '%s\n' "$url" > "$URL_FILE"
    fi
    ok "tunnel up (pid $pid): $(cat "$URL_FILE")"

    # end-to-end: /health through the public edge (DNS can lag a few seconds)
    local i=0 code=000
    while [ $i -lt 15 ]; do
        code="$(http_code "$(cat "$URL_FILE")/health")"
        [ "$code" = "200" ] && break
        sleep 2; i=$((i+1))
    done
    if [ "$code" = "200" ]; then
        ok "public /health → 200 through the tunnel"
    else
        warn "public /health returned $code — edge may still be propagating; retry in ~30s: scripts/share_explainer.sh status"
    fi
    code="$(http_code "$(cat "$URL_FILE")/")"
    [ "$code" = "401" ] && ok "public GET / → 401 without credentials (auth enforced at the edge)" \
                        || warn "public GET / returned $code (expected 401)"
}

# ----------------------------------------------------------------------------
# summary / status
# ----------------------------------------------------------------------------
print_summary() {
    local url; url="$( [ -s "$URL_FILE" ] && cat "$URL_FILE" || printf '%s' "$LOCAL_URL" )"
    local pw="(hidden — rerun with --show-password, or read AUTH_PASSWORD in .env)"
    [ "$SHOW_PASSWORD" = 1 ] && pw="$AUTH_PASSWORD"
    case "$AUTH_PASSWORD" in '$2a$'*|'$2b$'*|'$2y$'*) [ "$SHOW_PASSWORD" = 1 ] && pw="(bcrypt hash in .env — share the original password, not the hash)";; esac
    printf '\n%s================ ETP Explainer is live ================%s\n' "$C_BOLD" "$C_OFF"
    printf '  URL       : %s%s%s\n' "$C_BOLD" "$url" "$C_OFF"
    printf '  Username  : %s\n' "$AUTH_USERNAME"
    printf '  Password  : %s\n' "$pw"
    printf '  Try a load: %s/explain/9199475\n' "$url"
    printf '  Local     : %s\n' "$LOCAL_URL"
    printf '%s-------------------------------------------------------%s\n' "$C_DIM" "$C_OFF"
    if [ -s "$URL_FILE" ] && [ -z "${SHARE_TUNNEL_TOKEN:-}" ]; then
        printf '  quick-tunnel URL changes every start; keep this machine awake and online.\n'
    fi
    printf '  status: scripts/share_explainer.sh status    stop: scripts/share_explainer.sh stop\n'
    printf '  logs  : scripts/share_explainer.sh logs [server|tunnel]\n'
    printf '%s=======================================================%s\n\n' "$C_BOLD" "$C_OFF"
}

cmd_status() {
    local spid tpid code
    spid="$(read_pid "$SERVER_PID_FILE")"; tpid="$(read_pid "$TUNNEL_PID_FILE")"
    if pid_alive "$spid"; then
        code="$(http_code "$LOCAL_URL/health")"
        [ "$code" = "200" ] && ok "server running (pid $spid) — $LOCAL_URL/health → 200" \
                            || warn "server process alive (pid $spid) but /health → $code"
    else
        info "server not running"
    fi
    if pid_alive "$tpid"; then
        if [ -s "$URL_FILE" ]; then
            code="$(http_code_retry "$(cat "$URL_FILE")/health" 3 2)"
            [ "$code" = "200" ] && ok "tunnel running (pid $tpid) — $(cat "$URL_FILE") → 200" \
                                || warn "tunnel alive (pid $tpid) but $(cat "$URL_FILE")/health → $code"
        else
            warn "tunnel alive (pid $tpid) but no URL recorded"
        fi
    else
        info "tunnel not running"
    fi
    if pid_alive "$spid"; then print_summary; fi
}

cmd_logs() {
    case "${1:-both}" in
        server) tail -n 50 -f "$SERVER_LOG" ;;
        tunnel) tail -n 50 -f "$TUNNEL_LOG" ;;
        *)      tail -n 30 -f "$SERVER_LOG" "$TUNNEL_LOG" ;;
    esac
}

# ----------------------------------------------------------------------------
# arg parsing
# ----------------------------------------------------------------------------
CMD="${1:-help}"; shift || true
NO_TUNNEL=0; SHOW_PASSWORD=0; PORT_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-tunnel)      NO_TUNNEL=1 ;;
        --show-password)  SHOW_PASSWORD=1 ;;
        --port)           shift; PORT_OVERRIDE="${1:-}"; [ -n "$PORT_OVERRIDE" ] || die "--port needs a value" ;;
        --port=*)         PORT_OVERRIDE="${1#--port=}" ;;
        server|tunnel)    LOG_WHICH="$1" ;;
        -h|--help|help)   CMD=help ;;
        *) die "unknown argument: $1 (see: scripts/share_explainer.sh help)" ;;
    esac
    shift
done

case "$CMD" in
    help|-h|--help)
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    check)
        load_env; preflight ;;
    start)
        load_env; preflight; start_server
        [ "$NO_TUNNEL" = 0 ] && start_tunnel || info "tunnel skipped (--no-tunnel); reachable at $LOCAL_URL only"
        print_summary ;;
    stop)
        load_env
        stop_pid "tunnel" "$TUNNEL_PID_FILE"
        stop_pid "server" "$SERVER_PID_FILE"
        rm -f "$URL_FILE" ;;
    restart)
        load_env
        stop_pid "tunnel" "$TUNNEL_PID_FILE"; stop_pid "server" "$SERVER_PID_FILE"; rm -f "$URL_FILE"
        preflight; start_server
        [ "$NO_TUNNEL" = 0 ] && start_tunnel || info "tunnel skipped (--no-tunnel)"
        print_summary ;;
    status)
        load_env; cmd_status ;;
    url)
        [ -s "$URL_FILE" ] && cat "$URL_FILE" || { load_env; die "no tunnel URL recorded — is it running? (scripts/share_explainer.sh status)"; } ;;
    logs)
        cmd_logs "${LOG_WHICH:-both}" ;;
    *)
        die "unknown command: $CMD (check|start|stop|restart|status|url|logs|help)" ;;
esac
