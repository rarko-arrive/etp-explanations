# etp-explanations

**ETP shipment lifecycle explanations** — material change ledger, HC leadtime-isolated outlier cohort, and broker-facing timeline readouts.

Split from the etp-dqt monorepo. Answers: *what moved ETP50, targets, clocks, quantile refresh, or display workflow on this load?*

## Setup

```bash
cd ~/Git/Projects/etp/etp-explanations
cp .env.example .env
make install
make test
make pre-commit   # optional: verify hooks pass on full repo
```

## Prerequisites (data)

Built by **[etp-lake](../etp-lake/)** — not included in this repo:

```bash
# On etp-lake repo (or sync from Mac via scripts/sync_lake_to_vm.sh)
make etp-lake SKIP_PULL=1          # or full pull
uv run python scripts/export_executive_cohort.py \
  --avail-start '2025-01-01' --avail-end '2026-08-28' \
  --tail-mode scaled --tail-pct 0.10 --tail-amt 0
```

Required paths under `$DQT_DATA_DIR`:

| Path | Purpose |
|------|---------|
| `etp_lake/` | `EtpLake.feature_history`, decompose |
| `etp/executive-…/labeled-cohort.parquet` | HC outlier rank index |
| `etp/etp-slider-history.parquet` | Movement / pricing frame |
| `etp/analysis-davis-raw-*.parquet` | Davis path labels |

## Commands

```bash
# Explain one load (rank picker or explicit loadnumber)
make explain LOAD=9199475
make explain RANK=3

uv run python scripts/explain_etp_load.py --rank 3 --cohort hc_leadtime_isolated
uv run python scripts/explain_etp_load.py --load 9199475 --data-dir "$DQT_DATA_DIR"
```

**Note:** `make explain 9199475` does not work — use `make explain LOAD=9199475`.

After copying code from **etp-dqt**, rerun `make install` so the editable package picks up changes.

**Data shortcut (local dev):** point at etp-dqt caches without rebuilding:

```bash
# .env
DQT_DATA_DIR=/Users/rarko/Git/Projects/DQT/etp-dqt/data
```

Or symlink: `ln -s ../etp-dqt/data data`

Run `make test` after setup.

**Notebook:** `notebooks/etp-shipment-lifecyle.ipynb`

## Share a secure URL (basic auth + Cloudflare tunnel)

Works from a MacBook or the Azure VM. Needs `cloudflared` (`brew install cloudflared`)
and a real `AUTH_PASSWORD` in `.env` (see `.env.example` for the bcrypt one-liner).

```bash
make share-check          # preflight with remediation hints
make share                # → prints https://<words>.trycloudflare.com + username
make share-status | make share-stop
scripts/share_explainer.sh start --no-tunnel   # server only (VPN / nginx)
```

The URL is HTTPS at the Cloudflare edge, tunnelled to `127.0.0.1:$EXPLAIN_PORT`; every
page except `/health` requires the basic-auth credentials from `.env`. Quick-tunnel URLs
change on each start; set `SHARE_TUNNEL_TOKEN`/`SHARE_PUBLIC_URL` for a stable hostname.
See `DEPLOYMENT.md` for the VM/nginx deployment.

## Layout

| Path | Purpose |
|------|---------|
| `src/dqt/etp_lifecycle/explain.py` | Cohort selection, ledger, `explain_load()` |
| `src/dqt/etp_lifecycle/lightning.py` | Lightning cost tracking (Phase 4) |
| `src/dqt/etp_timeline.py` | Per-load timeline payload + plot |
| `src/dqt/etp_lake.py` | Read API only (`history`, `feature_history`, `decompose`) — no `etp_funnel` / `etp_impact` build deps |
| `src/dqt/etp_slider/` | Minimal movement/leadtime/MDE closure for explain |
| `scripts/explain_etp_load.py` | CLI |

## Sibling repos

| Repo | Relationship |
|------|--------------|
| **[etp-lake](../etp-lake/)** | Builds `$DQT_DATA_DIR/etp_lake/` and executive cohort |
| **[etp-dqt](../etp-dqt/)** | Not required for core lifecycle explain |

## Azure VM quick start

```bash
# From Mac
./scripts/sync_lake_to_vm.sh   # in etp-lake repo

# On VM
export DQT_DATA_DIR=~/cloudfiles/code/Users/rarko/dev/etp-dqt/data
jupyter notebook notebooks/etp-shipment-lifecyle.ipynb
```

Or set `LOAD_ID=9199475` and `COHORT=manual` to skip cohort parquet if only demoing one load (still needs etp_lake partitions for that load).
