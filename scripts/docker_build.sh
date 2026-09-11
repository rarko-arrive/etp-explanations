#!/usr/bin/env bash
# Build the explainer production image (requires GitHub SSH for arriveds).
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${1:-etp-explainer:local}"

_github_ssh_ok() {
  # GitHub returns exit 1 even on successful auth — check message, not exit code.
  local out
  out="$(ssh -o BatchMode=yes -T git@github.com 2>&1)" || true
  grep -qi 'successfully authenticated\|Hi ' <<< "${out}"
}

if ! _github_ssh_ok; then
  echo "ERROR: GitHub SSH auth required for private arriveds dependency." >&2
  echo "  ssh -T git@github.com   # should say Hi <user>!" >&2
  echo "  eval \"\$(ssh-agent -s)\"" >&2
  echo "  ssh-add --apple-use-keychain ~/.ssh/id_ed25519_github   # or your GitHub key" >&2
  echo "  DOCKER_BUILDKIT=1 docker build --ssh default -t ${IMAGE} ." >&2
  exit 1
fi

export DOCKER_BUILDKIT=1
docker build --ssh default -t "${IMAGE}" .
