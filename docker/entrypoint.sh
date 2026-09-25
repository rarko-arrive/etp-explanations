#!/usr/bin/env bash
# Container entrypoint — validate mounts, then start uvicorn via explain_serve.
set -euo pipefail

: "${DQT_DATA_DIR:=/data}"
: "${EXPLAIN_CACHE_DIR:=/cache}"
: "${EXPLAIN_HOST:=0.0.0.0}"
: "${EXPLAIN_PORT:=8765}"

if [[ ! -d "${DQT_DATA_DIR}/etp_lake" ]]; then
  echo "ERROR: missing lake at ${DQT_DATA_DIR}/etp_lake (bind-mount DQT_DATA_DIR)" >&2
  exit 1
fi

mkdir -p "${EXPLAIN_CACHE_DIR}" "${MPLCONFIGDIR:-/tmp/matplotlib}"

if [[ -n "${DQT_USE_LAKE_MIRROR:-}" ]] && [[ "${DQT_USE_LAKE_MIRROR}" =~ ^(1|true|yes|on)$ ]]; then
  mirror="${DQT_LAKE_MIRROR:-}"
  if [[ -z "${mirror}" ]] || [[ ! -d "${mirror}" ]]; then
    echo "WARN: DQT_USE_LAKE_MIRROR set but DQT_LAKE_MIRROR missing or not a directory — using cloudfiles lake" >&2
  else
    echo "lake mirror reads → ${mirror}"
  fi
fi

echo "DQT_DATA_DIR=${DQT_DATA_DIR}"
echo "EXPLAIN_CACHE_DIR=${EXPLAIN_CACHE_DIR}"
echo "listening on ${EXPLAIN_HOST}:${EXPLAIN_PORT}"

exec python scripts/explain_serve.py \
  --host "${EXPLAIN_HOST}" \
  --port "${EXPLAIN_PORT}" \
  --data-dir "${DQT_DATA_DIR}" \
  --cache-dir "${EXPLAIN_CACHE_DIR}"
