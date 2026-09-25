#!/usr/bin/env bash
# One-time / repeat: explainer on an Azure ML VM next to etp-lake's lake sync.
# Run ON the VM (or: ssh rarko2 'bash -s' < scripts/setup_vm_lake_env.sh)
#
#   ./scripts/setup_vm_lake_env.sh              # /mnt/dqt + cache dir, server keys in .env, register consumer
#   ./scripts/setup_vm_lake_env.sh --mirror     # also run one etp-lake `make lake-sync` now (background)
#   ./scripts/setup_vm_lake_env.sh --mode share # register for share_explainer.sh (tunnel) instead of nginx
#
# Lake paths (DQT_DATA_DIR, DQT_LAKE_MIRROR, mirror on/off) are NOT written to
# .env any more: etp-lake publishes them in ~/.config/dqt/lake.env after every
# sync, and the explainer reads that file (src/dqt/lake_env.py). Install the
# schedule once in etp-lake: `make lake-timer-install`.

set -euo pipefail

RUN_MIRROR=0
MODE=serve
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mirror) RUN_MIRROR=1 ;;
        --mode) MODE="${2:?}"; shift ;;
        -h|--help)
            sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown: $1" >&2; exit 1 ;;
    esac
    shift
done

EXPLAIN_REPO="${EXPLAIN_REPO:-$HOME/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations}"
# Pinned team clone (etp-lake AGENTS.md): its data/ is the team lake SoT.
LAKE_REPO="${LAKE_REPO:-$HOME/cloudfiles/code/Users/rarko/main/etp-lake}"

if [[ ! -d "$EXPLAIN_REPO" ]]; then
    echo "missing explainer repo: $EXPLAIN_REPO" >&2
    exit 1
fi

bootstrap_mnt_dqt() {
    if [[ -d /mnt/dqt ]] && touch /mnt/dqt/.write_test 2>/dev/null; then
        rm -f /mnt/dqt/.write_test
        echo "→ /mnt/dqt writable"
        return 0
    fi
    echo "→ creating /mnt/dqt (needs sudo once per VM)"
    sudo mkdir -p /mnt/dqt
    sudo chown "${USER}:${USER}" /mnt/dqt
    touch /mnt/dqt/.write_test && rm -f /mnt/dqt/.write_test
    echo "→ /mnt/dqt ready"
}

bootstrap_mnt_dqt
mkdir -p /mnt/dqt/etp_lake /mnt/dqt/etp-explainer-cache

ENV_FILE="$EXPLAIN_REPO/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$EXPLAIN_REPO/.env.example" "$ENV_FILE"
    echo "→ created .env from .env.example — set AUTH_PASSWORD before sharing"
fi

python3 <<PY
from pathlib import Path
import re

path = Path("${ENV_FILE}")
text = path.read_text()
lines = text.splitlines()

def set_kv(key: str, value: str, lines: list[str]) -> list[str]:
    pat = re.compile(rf"^\s*#?\s*{re.escape(key)}=")
    out: list[str] = []
    replaced = False
    for line in lines:
        if pat.match(line):
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return out

updates = {
    "EXPLAIN_HOST": "0.0.0.0",
    "EXPLAIN_PORT": "8765",
    "EXPLAIN_BEHIND_PROXY": "1",
    "EXPLAIN_CACHE_DIR": "/mnt/dqt/etp-explainer-cache",
}
for k, v in updates.items():
    lines = set_kv(k, v, lines)

path.write_text("\n".join(lines) + "\n")
print(f"→ updated {path}")
PY

"$EXPLAIN_REPO/scripts/register_lake_consumer.sh" --mode "$MODE"

echo ""
echo "Effective layout:"
echo "  Lake paths:            ~/.config/dqt/lake.env  (published by ${LAKE_REPO} make lake-sync)"
echo "  Explainer HTML cache:  /mnt/dqt/etp-explainer-cache"
echo ""

if [[ "$RUN_MIRROR" != "1" ]]; then
    echo "Next (once per VM): cd ${LAKE_REPO} && make lake-timer-install && ./scripts/install_lake_timer.sh run"
    echo "Or run one sync now: $0 --mirror"
    exit 0
fi

if [[ ! -d "$LAKE_REPO" ]]; then
    echo "skip sync — no etp-lake at $LAKE_REPO" >&2
    exit 1
fi

echo "→ etp-lake make lake-sync (background log: /tmp/etp-lake-sync.log)"
cd "$LAKE_REPO"
nohup make lake-sync >> /tmp/etp-lake-sync.log 2>&1 &
echo "  tail -f /tmp/etp-lake-sync.log"
