# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**etp-explanations** — ETP shipment lifecycle material-change ledger and HC leadtime-isolated outlier cohort. Answers: *what moved ETP50, targets, clocks, quantile refresh, or display workflow on this load?*

Split from etp-dqt monorepo. Depends on **etp-lake** for `$DQT_DATA_DIR/etp_lake/` partitions and executive cohort exports.

## Commands

```bash
# Setup
make install          # uv venv + Jupyter kernel + pre-commit hooks
make test             # pytest
make pre-commit       # run all hooks on entire repo

# Lint
make lint             # ruff check + format on src/, scripts/, app/, tests/
make sql-lint         # sqlfluff on SQL/

# Explain modes (three variants: CLI stdout, static HTML, interactive dashboard)
make explain LOAD=9199475             # CLI: print ledger to stdout
make explain RANK=3                   # ... or by cohort rank
make explain-ui LOAD=6963033          # static HTML → data/explain-shipments/explain-6963033.html
make explain-serve                    # interactive dashboard at http://127.0.0.1:8765

# Docker (production image requires GitHub SSH for arriveds dep)
make docker-build                     # → etp-explainer:local
make docker-run DQT_DATA=../etp-dqt/data
```

**Note:** `make explain 9199475` does NOT work — use `make explain LOAD=9199475` (Make doesn't parse bare numbers as arguments).

## Architecture

### Three explain modes

1. **CLI** (`make explain`, `scripts/explain_etp_load.py`) — prints material-change ledger to stdout for quick inspection
2. **Static HTML** (`make explain-ui`, `scripts/explain_ui.py`) — renders standalone HTML file with embedded timeline/ledger data
3. **Interactive dashboard** (`make explain-serve`, `scripts/explain_serve.py`) — FastAPI server (`app/explain/`) with load search and cached rendering

All three modes call `src/dqt/etp_lifecycle/explain.py:explain_load()`, which:
- Selects outlier cohort (hc_leadtime_isolated) from `labeled-cohort.parquet`
- Builds material-change ledger via `EtpLake.feature_history` → `consecutive_feature_deltas`
- Adds Lightning cost tracking (Phase 4) via `lightning.py:build_lightning_tracking`
- Generates timeline payload via `etp_timeline.py:build_load_timeline`

### Key modules

| Path | Purpose |
|------|---------|
| `src/dqt/etp_lifecycle/explain.py` | Cohort selection (`select_outlier_cohort`), ledger build, `explain_load()` orchestration |
| `src/dqt/etp_lifecycle/lightning.py` | Lightning cost tracking (Phase 4 events) |
| `src/dqt/etp_timeline.py` | Per-load timeline payload + Altair plot |
| `src/dqt/etp_lake.py` | Read-only API: `history`, `feature_history`, `decompose` (no build deps on etp_funnel/etp_impact) |
| `src/dqt/etp_slider/` | Minimal movement/leadtime/MDE closures extracted for explain (no full slider deps) |
| `app/explain/` | FastAPI server (server.py), service layer, rendering, templates |
| `scripts/explain_etp_load.py` | CLI entrypoint |
| `scripts/explain_ui.py` | Static HTML generator |
| `scripts/explain_serve.py` | Interactive server launcher |

### Data requirements

Set `DQT_DATA_DIR` in `.env` (defaults to `data/`). Required paths:

| Path under `$DQT_DATA_DIR` | Purpose |
|----------------------------|---------|
| `etp_lake/` | Lake partitions: `feature_history`, `decompose` |
| `etp/executive-*/labeled-cohort.parquet` | HC outlier rank index (built by etp-lake) |
| `etp/etp-slider-history.parquet` | Movement/pricing frame |
| `etp/analysis-davis-raw-*.parquet` | Davis path labels |

**Local dev shortcut:** symlink or point `.env` at `etp-dqt/data`:
```bash
ln -s ../etp-dqt/data data
# or in .env:
DQT_DATA_DIR=/Users/rarko/Git/Projects/DQT/etp-dqt/data
```

## Development workflow

- **After copying code from etp-dqt:** rerun `make install` to refresh editable package
- **Environment:** uses `uv` with custom venv path via `UV_PROJECT_ENVIRONMENT` in `.env` (defaults to `.venv`)
- **Testing:** `make test` (all), `uv run pytest tests/test_etp_lifecycle_explain.py` (single module), `uv run pytest tests/test_etp_lifecycle_explain.py::test_name` (single test)
- **Pre-commit:** ruff (lint+format), sqlfluff, trailing-whitespace, detect-private-key — runs on `src/`, `scripts/`, `app/`, `tests/`

## Dependencies

- **arriveds** — private GitHub dep (`ssh://git@github.com/rarko-arrive/arrive-ds.git`). Docker builds require SSH agent with GitHub key.
- **Python 3.12+** — uses match/case, type hints from 3.12

## Docker deployment

See `docker/README.md` for Azure ML Custom Application setup. Build requires `eval "$(ssh-agent -s)" && ssh-add` for arriveds access. Container runs as non-root uid 1000, read-only rootfs, mounts:
- `/data` → `$DQT_DATA_DIR` (read-only)
- `/mirror/etp_lake` → VM lake mirror (read-only, optional fast path)
- `/cache` → rendered HTML cache (read-write)

## VM workflow

```bash
# From Mac (in etp-lake repo)
./scripts/sync_lake_to_vm.sh

# On VM
export DQT_DATA_DIR=~/cloudfiles/code/Users/rarko/dev/etp-dqt/data
jupyter notebook notebooks/etp-shipment-lifecyle.ipynb
```
