#!/usr/bin/env bash
# One-time / repeat: Azure ML local disk for lake reads + explainer HTML cache.
# Run ON the VM (or: ssh rarko2 'bash -s' < scripts/setup_vm_lake_env.sh)
#
#   ./scripts/setup_vm_lake_env.sh              # bootstrap /mnt/dqt + patch .env
#   ./scripts/setup_vm_lake_env.sh --mirror     # also run etp-lake make mirror-lake LITE=1
#
# Keeps DQT_DATA_DIR on cloudfiles (SoT: cohort, slider, catalog writes).
# Parquet reads go to DQT_LAKE_MIRROR on /mnt; explainer cache on /mnt too.

set -euo pipefail

RUN_MIRROR=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mirror) RUN_MIRROR=1 ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown: $1" >&2; exit 1 ;;
    esac
    shift
done

EXPLAIN_REPO="${EXPLAIN_REPO:-$HOME/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations}"
LAKE_REPO="${LAKE_REPO:-$HOME/cloudfiles/code/Users/rarko/Projects/etp/etp-lake}"
# Same tree as etp-lake .env (case may vary on Azure Files; prefer lowercase rarko)
DATA_DIR="${DQT_DATA_DIR:-$HOME/cloudfiles/code/Users/rarko/Projects/etp/etp-lake/data}"

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
    "DQT_DATA_DIR": "${DATA_DIR}",
    "DQT_USE_LAKE_MIRROR": "1",
    "DQT_LAKE_MIRROR": "/mnt/dqt/etp_lake",
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

echo ""
echo "Effective layout:"
echo "  SoT (cohort, slider, catalog): ${DATA_DIR}"
echo "  Fast parquet reads:            /mnt/dqt/etp_lake  (after mirror-lake)"
echo "  Explainer HTML cache:          /mnt/dqt/etp-explainer-cache"
echo ""

if [[ "$RUN_MIRROR" != "1" ]]; then
    echo "Next: populate mirror (once per wiped /mnt, ~10–60 min):"
    echo "  cd ${LAKE_REPO} && make mirror-lake LITE=1"
    echo "Or re-run: $0 --mirror"
    exit 0
fi

if [[ ! -d "$LAKE_REPO" ]]; then
    echo "skip mirror — no etp-lake at $LAKE_REPO" >&2
    exit 1
fi

echo "→ vm_mirror_lake.sh --lite (background log: /tmp/etp-mirror-lake.log)"
cd "$LAKE_REPO"
export DQT_DATA_DIR="${DATA_DIR}"
export DQT_LAKE_MIRROR=/mnt/dqt/etp_lake
nohup ./scripts/vm_mirror_lake.sh --lite >> /tmp/etp-mirror-lake.log 2>&1 &
echo "  tail -f /tmp/etp-mirror-lake.log"
