# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**etp-explanations** — ETP shipment lifecycle material-change ledger and HC leadtime-isolated outlier cohort. Answers: *what moved ETP50, targets, clocks, quantile refresh, or display workflow on this load?*

Split from the etp-dqt monorepo. Depends on **etp-lake** (sibling repo) for `$DQT_DATA_DIR/etp_lake/` partitions and the executive cohort export. Nothing in this repo builds lake data; it only reads it.

## Commands

```bash
# Setup
make install          # creates .env from .env.example, uv sync --all-groups, Jupyter kernel, pre-commit hooks, direnv allow
make test             # pytest tests/ -q
make pre-commit       # run all hooks on entire repo

# Lint
make lint             # ruff check --fix --unsafe-fixes + ruff format on src/ scripts/ app/ tests/
make sql-lint         # sqlfluff on SQL/

# Explain modes (three variants: CLI stdout, static HTML, interactive dashboard)
make explain LOAD=9199475             # CLI: print ledger to stdout
make explain 9199475                  # same (Makefile swallows bare loadnumber goals)
make explain RANK=3                   # ... or by HC cohort rank
make explain-ui LOAD=6963033          # static HTML → data/explain-shipments/explain-6963033.html, opens browser
make explain-serve                    # FastAPI dashboard at http://127.0.0.1:8765  [PORT=...]

# Share a public HTTPS URL (basic auth) — server + cloudflared quick tunnel, Mac or VM
make share-check                      # preflight with remediation hints
make share [PORT=8799]                # prints https://<words>.trycloudflare.com; state in .run/
make share-status / share-stop / share-logs

# Docker (production image requires GitHub SSH agent for the private arriveds dep)
make docker-build                     # → etp-explainer:local
make docker-run DQT_DATA=../etp-dqt/data
```

Single test module / test:

```bash
uv run pytest tests/test_etp_lifecycle_explain.py
uv run pytest tests/test_etp_lifecycle_explain.py::test_explain_load_9199475
uv run pytest -m "not integration"    # skip tests that need real lake data (fast, ~seconds vs ~2 min)
```

The Makefile reads `UV_PROJECT_ENVIRONMENT` and `DQT_DATA_DIR` out of `.env` itself (with `~` expansion via `scripts/expand_user_path.sh`), so `make` targets work without direnv. Always invoke Python through `uv run`; the venv may live outside the repo (`.venv` is then a symlink).

## Architecture

### Three explain modes, one pipeline

1. **CLI** (`scripts/explain_etp_load.py`) — prints the ledger; `--json-out` dumps the full explanation.
2. **Static HTML** (`scripts/explain_ui.py`) — one self-contained HTML file with embedded timeline PNG and ledger JSON.
3. **Interactive dashboard** (`scripts/explain_serve.py` → `app/explain/`) — FastAPI with load search, basic auth, and on-disk HTML cache.

All three call `explain_load()` in `src/dqt/etp_lifecycle/explain.py`, which:
- resolves the HC outlier cohort (`select_outlier_cohort`, cohort `hc_leadtime_isolated`) from `labeled-cohort.parquet` when a rank rather than a loadnumber is given;
- builds the material-change ledger from `EtpLake.feature_history` → consecutive feature deltas (`build_material_change_ledger`);
- adds Lightning cost tracking (Phase 4 events) via `lightning.py:build_lightning_tracking`;
- pairs with `etp_timeline.py:build_load_timeline` / `plot_etp_timeline` for the per-load timeline payload and matplotlib figure.

Modes 2 and 3 share `app/explain/service.py:render_explanation_for_load()` → `view_model.build_view_model()` → `render.render_explain_html()`. The HTML templates in `app/explain/templates/` are plain string templates (no Jinja); data is injected as JSON in a `<script>` block. `ValueError`s containing "No ETP history" / "Insufficient timeline data" are translated into `LoadNotFoundError` (404).

### Dashboard (`app/explain/`) layering

| Module | Role |
|--------|------|
| `config.py` | `ExplainSettings` (pydantic-settings, reads `.env`); singleton via `get_settings()`, `reset_settings()` for tests |
| `dependencies.py` | FastAPI `Depends` aliases: `Settings`, `Lake` (process-wide `EtpLake` singleton), `CacheDir` |
| `auth.py` | HTTP Basic auth via the `bcrypt` package (not passlib — 1.7.4 breaks with bcrypt≥4.1). `AUTH_PASSWORD` may be plaintext (dev) or a `$2b$` hash; `AUTH_ENABLED=0` disables |
| `router.py` | `GET /health` (no auth), `GET /` (search page), `GET /explain/{loadnumber}?refresh=true&query_mde=true` |
| `service.py` | `ExplainOptions`, cache read/write (`explain-{load}.html` under cache dir), `render_explanation_for_load` |
| `middleware.py` | Request logging with 8-char request id (`X-Request-ID` header) |
| `server.py` | `create_app()` (no args; everything comes from settings) + `run_server()` (uvicorn, proxy headers) |

Server settings and their env vars: `EXPLAIN_HOST`, `EXPLAIN_PORT`, `EXPLAIN_BEHIND_PROXY`, `EXPLAIN_CACHE_DIR` (default `data/explain-shipments`), `DQT_DATA_DIR`, `AUTH_ENABLED`, `AUTH_USERNAME`, `AUTH_PASSWORD`, `DQT_LOG_LEVEL`. See `.env.example`.

### Library modules (`src/dqt/`)

| Path | Purpose |
|------|---------|
| `etp_lifecycle/explain.py` | Cohort selection, ledger build, `explain_load()` orchestration, `ledger_to_json` |
| `etp_lifecycle/lightning.py` | Lightning cost tracking (Phase 4 events) |
| `etp_timeline.py` | Per-load timeline payload + matplotlib/Altair plot |
| `etp_lake.py` | **Read-only** lake API (`history`, `feature_history`, `decompose`) and `LakePaths`. Build paths (`etp_funnel`, `etp_impact`) are guarded imports that raise if called — don't add build deps here |
| `etp_slider/` | Minimal movement / leadtime / MDE closures copied from etp-dqt for explain; not the full slider |
| `etp_mart.py`, `score/`, `holidays.py`, `viz.py` | Supporting constants (`ID_COL`), metrics, plotting helpers |

**Lake mirror:** `DQT_USE_LAKE_MIRROR=1` (+ optional `DQT_LAKE_MIRROR`, default `~/dqt/etp_lake`) makes `LakePaths` read parquet from a local-disk mirror while `duckdb_path` and writes stay on `DQT_DATA_DIR`. If the mirror dir is missing it logs a warning and falls back silently.

### Data requirements

`DQT_DATA_DIR` (from `.env`, default `data/`). Required under it:

| Path | Purpose |
|------|---------|
| `etp_lake/` | Lake partitions: `snapshots/`, `feature_history`, `decompose` |
| `etp/executive-*/labeled-cohort.parquet` | HC outlier rank index (built by etp-lake `export_executive_cohort.py`) |
| `etp/etp-slider-history.parquet` | Movement/pricing frame (`HISTORY_CACHE`) |
| `etp/analysis-davis-raw-*.parquet` | Davis path labels |

Local dev shortcut: `ln -s ../etp-dqt/data data`, or set `DQT_DATA_DIR` to the etp-dqt/etp-lake data dir in `.env`.

## Testing notes

- `tests/conftest.py:timeline_lake` builds a synthetic single-load lake (load `9394640`) in `tmp_path`; unit tests should use it rather than real data.
- Tests marked `@pytest.mark.integration` gate on `etp/etp-slider-history.parquet` existing under `DQT_DATA_DIR` and skip otherwise. The mark is not registered in `pyproject.toml`, so pytest emits `PytestUnknownMarkWarning`. When real data is present these tests take ~2 min.
- Server tests use FastAPI `TestClient` against `create_app()` with `app.dependency_overrides` for `get_settings`, `get_etp_lake`, `get_cache_dir`; requests pass `auth=(user, pw)`. The `timeline_lake` fixture unsets `DQT_USE_LAKE_MIRROR`/`EXPLAIN_CACHE_DIR` so `.env` on the VM cannot leak into tests.

## Development workflow

- **After copying code from etp-dqt:** rerun `make install` so the editable install picks up new modules.
- **Environment:** `uv` with `UV_PROJECT_ENVIRONMENT` from `.env`. On Azure ML set it to a local-disk path (e.g. `/home/azureuser/uv-venvs/etp-explanations`); `install.sh` symlinks `.venv` to it.
- **Lint config:** ruff line-length 120, target py312, double quotes; `E402` allowed in `scripts/` and `etp_slider/drift/mde_timeline.py`. Pre-commit also runs sqlfluff on `SQL/`, trailing-whitespace/EOF fixers (excluding `notebooks/`), and `detect-private-key`.
- **`.ai/`** holds gitignored planning notes and results (plans for the explainer app live in `.ai/plans/app/`).

## Dependencies

- **arriveds** — private GitHub dep pinned by git rev in `pyproject.toml` (`ssh://git@github.com/rarko-arrive/arrive-ds.git`). `uv sync` and Docker builds need an SSH agent with the GitHub key loaded (`eval "$(ssh-agent -s)" && ssh-add`).
- **Python 3.12+** — uses match/case and 3.12 typing.
- Dependency groups: `serve` (fastapi, uvicorn, bcrypt, python-multipart) and `dev` (pytest, ruff, httpx, pre-commit, sqlfluff, includes `serve`).

## Deployment

Three deployment paths exist; see the linked docs rather than duplicating them here.

- **Share from any machine** (`scripts/share_explainer.sh`, `make share*`): starts uvicorn on `127.0.0.1` plus a `cloudflared` quick tunnel, verifies auth locally and through the edge, prints the URL. Bash 3.2 compatible (macOS); all config from `.env`; `--port` is the only CLI override; refuses empty/`changeme` passwords. Runtime state in `.run/`.

- **Bare-metal on the rarko1 VM** (`DEPLOYMENT.md`, `manage-explainer.sh {start|stop|restart|status|logs}`): uvicorn on `0.0.0.0:8765` behind nginx on port 80, logs to `/tmp/etp-explainer.log`, cache at `/mnt/dqt/etp-explainer-cache`, basic auth enabled. `manage-explainer.sh` stops the server with `pkill -f explain_serve`.
- **Docker / Azure ML Custom Application** (`docker/README.md`): non-root uid 1000, read-only rootfs, mounts `/data` (ro), `/mirror/etp_lake` (ro, optional), `/cache` (rw). Auth is expected to come from Azure AD in front, so `EXPLAIN_BEHIND_PROXY=1`.

## VM workflow

On the Azure VM the repo is reachable at two paths that point to the same files. **Always use `~/cloudfiles/code/Users/rarko/Projects/etp/etp-explanations`.** The `/mnt/batch/tasks/shared/LS_root/mounts/clusters/rarko1/...` path goes through extra network layers and is 50–100x slower for git and file operations. `DEPLOYMENT.md` lists the `git config --local` tweaks (`core.fsmonitor false`, `core.untrackedCache true`, `feature.manyFiles true`) that make git tolerable on Azure Files.

```bash
# From Mac (in etp-lake repo)
./scripts/sync_lake_to_vm.sh

# On VM
export DQT_DATA_DIR=~/cloudfiles/code/Users/rarko/Projects/etp/etp-lake/data
jupyter notebook notebooks/etp-shipment-lifecyle.ipynb
```
