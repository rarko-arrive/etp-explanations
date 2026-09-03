# etp-explanations — agent guide

Shipment lifecycle material-change ledger. Requires etp-lake data on disk.

## Commands

```bash
make install && make test
make explain LOAD=9199475
# notebook: notebooks/etp-shipment-lifecyle.ipynb
```

## Prerequisites

`$DQT_DATA_DIR/etp_lake/` + `data/etp/executive-*/labeled-cohort.parquet` from etp-lake exports.

## Key modules

- `src/dqt/etp_lifecycle/explain.py` — `select_outlier_cohort`, `explain_load`
- `src/dqt/etp_timeline.py` — timeline payload
- `scripts/explain_etp_load.py` — CLI

## VM sync

Use `../etp-lake/scripts/sync_lake_to_vm.sh` from Mac; set `DQT_DATA_DIR` on VM to match.
