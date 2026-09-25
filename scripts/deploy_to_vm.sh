#!/usr/bin/env bash
# Deploy (or bootstrap) the ETP explainer on an Azure ML compute VM from your Mac.
#
# Prerequisites on the Mac:
#   - SSH config alias to the VM (e.g. `ssh rarko2` works; often via `az ml compute connect-ssh`)
#   - Current branch pushed to origin if the VM will `git pull` it
#
# Usage (from repo root):
#   VM_HOST=rarko2 ./scripts/deploy_to_vm.sh check
#   VM_HOST=rarko2 ./scripts/deploy_to_vm.sh bootstrap   # first time on a new VM
#   VM_HOST=rarko2 ./scripts/deploy_to_vm.sh deploy      # git pull + install + restart
#   VM_HOST=rarko2 GIT_REF=main ./scripts/deploy_to_vm.sh deploy
#   VM_HOST=rarko2 ./scripts/deploy_to_vm.sh deploy --skip-install
#   VM_HOST=rarko2 SHARE=1 ./scripts/deploy_to_vm.sh deploy   # run via share_explainer.sh (tunnel) instead of nginx
#
# Deploy = reset the VM checkout to ${GIT_REMOTE}/${GIT_REF} (refuses if the VM has
# local commits or edits — FORCE=1 to discard), make install, register with the
# etp-lake lake sync (~/.config/dqt/lake-consumers.d/), restart.
#
# The VM must already have .env (paths, AUTH_PASSWORD, DQT_DATA_DIR). This script
# never copies .env from the Mac — Mac and VM settings differ.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VM_HOST="${VM_HOST:-rarko2}"
VM_REPO="${VM_REPO:-~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_REF="${GIT_REF:-}"
SKIP_INSTALL=0
SHARE="${SHARE:-0}"
FORCE="${FORCE:-0}"
REPO_URL="${REPO_URL:-git@github.com:rarko-arrive/etp-explanations.git}"

CMD="${1:-deploy}"
shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-install) SKIP_INSTALL=1 ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
    shift
done

remote() {
    ssh -o BatchMode=yes "$VM_HOST" "$@"
}

die() { echo "deploy_to_vm: $*" >&2; exit 1; }

preflight_ssh() {
    remote "echo ok" >/dev/null 2>&1 || die "cannot SSH to $VM_HOST (BatchMode). Fix ~/.ssh/config or VPN, then: ssh $VM_HOST"
}

repo_dir_on_vm() {
    remote "bash -lc 'cd $VM_REPO && pwd'"
}

cmd_check() {
    preflight_ssh
    echo "→ SSH $VM_HOST"
    if remote "test -d $VM_REPO/.git"; then
        echo "→ repo: $(repo_dir_on_vm)"
        remote "bash -lc 'cd $VM_REPO && git rev-parse --short HEAD && git status -sb | head -3'"
    else
        echo "→ repo missing at $VM_REPO — run: VM_HOST=$VM_HOST $0 bootstrap"
        exit 1
    fi
    if remote "test -f $VM_REPO/.env"; then
        echo "→ .env present on VM"
    else
        echo "→ WARN: no .env on VM — copy from .env.example and set DQT_DATA_DIR, AUTH_PASSWORD"
    fi
    if remote "test -x $VM_REPO/manage-explainer.sh"; then
        echo "→ manage-explainer.sh executable"
    else
        echo "→ WARN: chmod +x manage-explainer.sh on VM"
    fi
    remote "bash -lc 'cd $VM_REPO && ./manage-explainer.sh status'" || true
    code="$(remote "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health; exit 0" 2>/dev/null | tr -d '[:space:]')"
    code="${code:-000}"
    echo "→ localhost:8765/health → ${code:-000}"
    if [ "$code" != "200" ]; then
        echo "→ server not up yet — start with: VM_HOST=$VM_HOST $0 deploy"
        echo "  or: ssh $VM_HOST 'cd $VM_REPO && ./manage-explainer.sh start && tail -30 /tmp/etp-explainer.log'"
    else
        echo "→ try in browser (VNet): http://${VM_HOST}/  (needs nginx → :8765; see DEPLOYMENT.md)"
    fi
}

git_ref_for_deploy() {
    if [ -n "$GIT_REF" ]; then
        echo "$GIT_REF"
        return
    fi
    local branch
    branch="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ -n "$branch" ] && [ "$branch" != HEAD ]; then
        echo "$branch"
    else
        echo "main"
    fi
}

cmd_bootstrap() {
    preflight_ssh
    echo "→ bootstrap on $VM_HOST at $VM_REPO"
    remote "mkdir -p $(dirname "$VM_REPO")"
    if ! remote "test -d $VM_REPO/.git"; then
        echo "→ cloning $REPO_URL"
        remote "git clone $REPO_URL $VM_REPO"
    fi
    remote "bash -lc 'cd $VM_REPO && git config --local core.fsmonitor false && git config --local core.untrackedCache true && git config --local feature.manyFiles true || true'"
    remote "chmod +x $VM_REPO/manage-explainer.sh $VM_REPO/scripts/deploy_to_vm.sh 2>/dev/null || chmod +x $VM_REPO/manage-explainer.sh"
    remote "mkdir -p /mnt/dqt/etp-explainer-cache 2>/dev/null && chown \"\$(whoami):\$(whoami)\" /mnt/dqt/etp-explainer-cache 2>/dev/null || true"
    if ! remote "test -f $VM_REPO/.env"; then
        remote "bash -lc 'cd $VM_REPO && cp -n .env.example .env || true'"
        echo "→ created $VM_REPO/.env from example — edit on VM: DQT_DATA_DIR, AUTH_PASSWORD, EXPLAIN_HOST=0.0.0.0, EXPLAIN_CACHE_DIR"
    fi
    echo "→ bootstrap done. Edit .env on the VM, then: VM_HOST=$VM_HOST $0 deploy"
    echo "→ nginx (one-time): see DEPLOYMENT.md — server_name $VM_HOST and proxy_pass :8765"
}

cmd_deploy() {
    preflight_ssh
    ref="$(git_ref_for_deploy)"
    echo "→ deploy to $VM_HOST ($VM_REPO) @ ${GIT_REMOTE}/${ref}"

    remote "test -d $VM_REPO/.git" || die "repo not on VM — run bootstrap first"

    # Azure Files misreports stat data, so `git checkout` / `pull` fail with
    # "not uptodate" on files nobody touched. Refresh the index; if it still
    # disagrees, rebuild it (the index is only a cache) and reset.
    remote "bash -s" <<EOF || die "git sync failed on VM (see above); FORCE=1 discards VM-local commits/edits"
set -euo pipefail
cd $VM_REPO
git fetch -q $GIT_REMOTE $ref
git update-index -q --refresh >/dev/null 2>&1 || true
if [ "$FORCE" != 1 ]; then
    if [ -n "\$(git status --porcelain --untracked-files=no)" ]; then
        echo "VM checkout has uncommitted edits:"; git status --short --untracked-files=no | head -20; exit 1
    fi
    if git rev-parse -q --verify "refs/heads/$ref" >/dev/null && \
       [ "\$(git rev-list --count $GIT_REMOTE/$ref..$ref)" != 0 ]; then
        echo "VM branch $ref has commits not on $GIT_REMOTE/$ref:"; git log --oneline $GIT_REMOTE/$ref..$ref | head; exit 1
    fi
fi
if ! git checkout -q -B $ref $GIT_REMOTE/$ref 2>/dev/null; then
    echo "checkout hit stale index (Azure Files) — rebuilding index"
    rm -f .git/index
    git reset -q --hard
    git checkout -q -B $ref $GIT_REMOTE/$ref
fi
git reset -q --hard $GIT_REMOTE/$ref
echo "VM at \$(git rev-parse --short HEAD) (\$(git rev-parse --abbrev-ref HEAD))"
EOF
    if [ "$SKIP_INSTALL" = 0 ]; then
        echo "→ make install (uv sync; VM needs GitHub SSH for arriveds)"
        remote "bash -lc 'cd $VM_REPO && make install'" || die "make install failed — on VM: eval \"\$(ssh-agent -s)\" && ssh-add, then retry"
    fi
    if [ "$SHARE" = 1 ]; then
        echo "→ register with lake sync (share mode) + restart via share_explainer.sh"
        remote "bash -lc 'cd $VM_REPO && scripts/register_lake_consumer.sh --mode share && scripts/share_explainer.sh lake-updated'"
        echo "→ deployed — share URL: $(remote "cat $VM_REPO/.run/share.url 2>/dev/null" || echo '?')"
        return
    fi
    echo "→ register with lake sync (serve mode) + restart explainer"
    remote "bash -lc 'cd $VM_REPO && scripts/register_lake_consumer.sh --mode serve && ./manage-explainer.sh restart'"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        code="$(remote "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/health; exit 0" 2>/dev/null | tr -d '[:space:]')"
        [ "$code" = "200" ] && break
        sleep 2
    done
    [ "$code" = "200" ] || die "health check failed (HTTP $code) — ssh $VM_HOST 'tail -50 /tmp/etp-explainer.log'"
    echo "→ deployed — http://${VM_HOST}/ (VNet) · health OK on :8765"
}

case "$CMD" in
    check) cmd_check ;;
    bootstrap) cmd_bootstrap ;;
    deploy) cmd_deploy ;;
    help|-h|--help)
        sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) die "unknown command: $CMD (check|bootstrap|deploy|help)" ;;
esac
