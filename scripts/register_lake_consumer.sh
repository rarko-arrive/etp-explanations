#!/usr/bin/env bash
# Register the explainer with etp-lake's VM lake sync (run ON the VM).
#
#   scripts/register_lake_consumer.sh                # mode=serve (manage-explainer.sh, nginx :80 → EXPLAIN_PORT)
#   scripts/register_lake_consumer.sh --mode share   # share_explainer.sh (server + cloudflared tunnel)
#   scripts/register_lake_consumer.sh --mode share --port 8799
#   scripts/register_lake_consumer.sh --unregister
#
# Writes ${DQT_CONFIG_DIR:-~/.config/dqt}/lake-consumers.d/etp-explanations.conf. The
# lake sync (etp-lake `make lake-sync`, systemd timer) then:
#   - keeps `requires` paths in the mirror and only publishes the mirror when they are there
#   - runs `on_sync` every run   → start the app if it is down (e.g. after a reboot)
#   - runs `on_update` on change → drop cached HTML, restart the server on the new lake.env
#
# `requires` must match MIRROR_REQUIRED_PATHS in src/dqt/etp_lake.py (tested).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
NAME=etp-explanations
REQUIRES="snapshots mart/feature_snapshots"
MODE=serve
PORT=""
UNREGISTER=0

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:?}"; shift ;;
        --port) PORT="${2:?}"; shift ;;
        --unregister) UNREGISTER=1 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
    shift
done

CONFIG_DIR="${DQT_CONFIG_DIR:-$HOME/.config/dqt}"
CONF="$CONFIG_DIR/lake-consumers.d/$NAME.conf"

if [ "$UNREGISTER" = 1 ]; then
    rm -f "$CONF"
    echo "→ removed $CONF"
    exit 0
fi

# Azure ML: prefer ~/cloudfiles/code/… over the slow /mnt/batch/… alias; never /mnt/mirror (wiped).
case "$REPO_ROOT" in
    /mnt/batch/tasks/shared/LS_root/mounts/clusters/*/code/*)
        alt="$HOME/cloudfiles/code/${REPO_ROOT#/mnt/batch/tasks/shared/LS_root/mounts/clusters/*/code/}"
        [ -d "$alt" ] && REPO_ROOT="$alt" ;;
esac
case "$REPO_ROOT" in
    /mnt/*)
        if [ "${DQT_ALLOW_EPHEMERAL_REPO:-0}" != 1 ]; then  # tests / throwaway checkouts set it
            echo "ERROR: $REPO_ROOT is on /mnt (wiped on VM stop/start) — register from the persistent checkout" >&2
            exit 1
        fi ;;
esac

port_arg=""
[ -n "$PORT" ] && port_arg=" --port $PORT"
case "$MODE" in
    serve)
        on_sync="$REPO_ROOT/manage-explainer.sh ensure"
        on_update="$REPO_ROOT/manage-explainer.sh lake-updated" ;;
    share)
        on_sync="$REPO_ROOT/scripts/share_explainer.sh ensure$port_arg"
        on_update="$REPO_ROOT/scripts/share_explainer.sh lake-updated$port_arg" ;;
    *) echo "unknown --mode $MODE (serve|share)" >&2; exit 1 ;;
esac

mkdir -p "$(dirname "$CONF")"
tmp="$(mktemp "$CONF.XXXXXX")"
cat > "$tmp" <<EOF
# Written by etp-explanations scripts/register_lake_consumer.sh --mode $MODE$port_arg
name=$NAME
requires=$REQUIRES
on_sync=$on_sync
on_update=$on_update
EOF
mv -f "$tmp" "$CONF"
echo "→ registered $NAME ($MODE) in $CONF"
sed 's/^/    /' "$CONF"
if [ ! -f "$CONFIG_DIR/lake.env" ]; then
    echo "→ no $CONFIG_DIR/lake.env yet — on the VM: cd ~/cloudfiles/code/Users/rarko/main/etp-lake && make lake-sync"
fi
