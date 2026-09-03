"""ETP analytics mart: feature catalog, deltas, and category decomposition.

Raw mart stores prediction + input snapshots only. Day-1 HSA anchors are
report parameters (see :func:`nearest_anchor`), not required mart columns.
Category taxonomy: ``.ai/plans/etp-slider-leadtime/etp-changing.md``.
Category price-impact (OLS attribution): ``.ai/plans/etp-slider/feature-impact.md``.

Log keys verified against ``events_raw.etp.etp_model_logs.lightning_data.features_used``
(sample inventory 2026-08). ``is_hyperlocal`` is a first-class table column.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import polars as pl
from loguru import logger

from dqt.score.constants import ID_COL

# Report-time defaults (not mart schema)
DEFAULT_ANCHOR_HSA = 24
DEFAULT_ANCHOR_TOLERANCE_H = 12

FEATURE_HISTORY_CACHE = "etp-feature-history.parquet"

# Numeric / bool inputs used for Δ / decomp when present on feature_snapshots
CURATED_INPUT_COLS = (
    "hours_before_pickup",
    "hours_since_available",
    "total_charges",
    "hard_ft_cnt",
    "hard_ft_cnt_improved",
    "hard_ft_cnt_reg",
    "charge_less_dat",
    "dat_rate",
    "mean_pred_mkt_rt",
    "fuel_cost",
    "fsc",
    "lag7_cpm_x_miles",
    "ra7_cpm_x_miles",
    "book_2_pkup",
    "book_2_pkup_days",
    "avail_2_book",
    "avail_2_book_days",
    "clhp_ma",
    "clhp_pred_line",
    "clhp_pred_tot_cost",
    "knn_50",
    "knn_avg_distance",
    "loaded_miles",
    "miles_feat",
)

# Default catalog: every etp-changing.md feature → verified log_key
FEATURE_CATALOG: list[dict[str, Any]] = [
    {
        "doc_name": "Book 2 pickup",
        "mart_column": "book_2_pkup",
        "log_key": "book_2_pkup",
        "log_path": "lightning_data.features_used.book_2_pkup",
        "movement_category": 3,
        "movement_category_name": "Deterministic changes",
        "priority": "P0",
        "status": "mapped",
        "notes": "Also book_2_pkup_days; hours_before_pickup is derived clock",
    },
    {
        "doc_name": "Avail 2 book",
        "mart_column": "avail_2_book",
        "log_key": "avail_2_book",
        "log_path": "lightning_data.features_used.avail_2_book",
        "movement_category": 3,
        "movement_category_name": "Deterministic changes",
        "priority": "P0",
        "status": "mapped",
        "notes": "Also avail_2_book_days; hours_since_available is derived clock",
    },
    {
        "doc_name": "Load type (equipment)",
        "mart_column": "load_type",
        "log_key": "EquipmentType",
        "log_path": "lightning_data.features_used.EquipmentType",
        "movement_category": 2,
        "movement_category_name": "Changes in load features correlated with time",
        "priority": "P0",
        "status": "mapped",
        "notes": (
            "Equipment passed to each ETP call; codes V/R/PO/… "
            "(not core.loads.load_type DRY/REEFER labels)"
        ),
    },
    {
        "doc_name": "Total Charges",
        "mart_column": "total_charges",
        "log_key": "TotalCharges",
        "log_path": "lightning_data.features_used.TotalCharges",
        "movement_category": 2,
        "movement_category_name": "Changes in load features correlated with time",
        "priority": "P1",
        "status": "mapped",
    },
    {
        "doc_name": "Hard Ft cnt",
        "mart_column": "hard_ft_cnt",
        "log_key": "hard_ft_cnt",
        "log_path": "lightning_data.features_used.hard_ft_cnt",
        "movement_category": 2,
        "movement_category_name": "Changes in load features correlated with time",
        "priority": "P1",
        "status": "mapped",
        "notes": "Also hard_ft_cnt_improved, hard_ft_cnt_reg",
    },
    {
        "doc_name": "Charge less DAT",
        "mart_column": "charge_less_dat",
        "log_key": "charge_less_mean_pred_mkt_rt",
        "log_path": "lightning_data.features_used.charge_less_mean_pred_mkt_rt",
        "movement_category": 2,
        "movement_category_name": "Changes in load features correlated with time",
        "priority": "P1",
        "status": "mapped",
    },
    {
        "doc_name": "DAT rate",
        "mart_column": "dat_rate",
        "log_key": "DAT",
        "log_path": "lightning_data.features_used.DAT",
        "movement_category": 1,
        "movement_category_name": "Random changes in staged data",
        "priority": "P2",
        "status": "mapped",
        "notes": "Also mean_pred_mkt_rt market proxy",
    },
    {
        "doc_name": "Happy Path all in",
        "mart_column": "clhp_pred_tot_cost",
        "log_key": "clhp_pred_tot_cost",
        "log_path": "lightning_data.features_used.clhp_pred_tot_cost",
        "movement_category": 2,
        "movement_category_name": "Changes in load features correlated with time",
        "priority": "P1",
        "status": "mapped",
    },
    {
        "doc_name": "happy path linehaul",
        "mart_column": "clhp_pred_line",
        "log_key": "clhp_pred_line",
        "log_path": "lightning_data.features_used.clhp_pred_line",
        "movement_category": 2,
        "movement_category_name": "Changes in load features correlated with time",
        "priority": "P1",
        "status": "mapped",
    },
    {
        "doc_name": "Lag 7 cpm x miles",
        "mart_column": "lag7_cpm_x_miles",
        "log_key": "lag7_cpm_x_mi",
        "log_path": "lightning_data.features_used.lag7_cpm_x_mi",
        "movement_category": 1,
        "movement_category_name": "Random changes in staged data",
        "priority": "P2",
        "status": "mapped",
    },
    {
        "doc_name": "ra 7 cpm x miles",
        "mart_column": "ra7_cpm_x_miles",
        "log_key": "ra_cpm_7_x_mi",
        "log_path": "lightning_data.features_used.ra_cpm_7_x_mi",
        "movement_category": 1,
        "movement_category_name": "Random changes in staged data",
        "priority": "P2",
        "status": "mapped",
    },
    {
        "doc_name": "CLHP cust",
        "mart_column": "clhp_ma",
        "log_key": "clhp_MA",
        "log_path": "lightning_data.features_used.clhp_MA",
        "movement_category": 1,
        "movement_category_name": "Random changes in staged data",
        "priority": "P2",
        "status": "mapped",
    },
    {
        "doc_name": "Fuel",
        "mart_column": "fuel_cost",
        "log_key": "fuel_cost",
        "log_path": "lightning_data.features_used.fuel_cost",
        "movement_category": 1,
        "movement_category_name": "Random changes in staged data",
        "priority": "P2",
        "status": "mapped",
        "notes": "Also fsc",
    },
    {
        "doc_name": "Miles",
        "mart_column": "loaded_miles",
        "log_key": "Miles",
        "log_path": "core.loads.loaded_miles / features_used.Miles",
        "movement_category": 0,
        "movement_category_name": "Unclear",
        "priority": "P3",
        "status": "mapped",
        "notes": "Stratum; features_used.Miles mirrored as miles_feat",
    },
    {
        "doc_name": "Hyperlocal",
        "mart_column": "is_hyperlocal",
        "log_key": None,
        "log_path": "etp_model_logs.is_hyperlocal",
        "movement_category": 0,
        "movement_category_name": "Model path / stratum",
        "priority": "P0",
        "status": "mapped",
        "notes": "First-class bool on model logs; also prediction.is_hyperlocal",
    },
]

CHARGE_PATH_COLS = ("path_taken", "multiplier", "override_flag")

MODEL_CALL_COLS = (
    "load_type",
    "is_hyperlocal",
    "knn_avg_distance",
    "weatherman_version",
    "etp90",
    "knn_50",
    "is_happy_path",
)


def feature_catalog_payload() -> dict[str, Any]:
    return {
        "version": 2,
        "source": (
            "etp-changing.md + etp_model_logs.features_used inventory "
            "(2026-08 sample, 104 keys)"
        ),
        "related_playbooks": [
            ".ai/plans/etp-slider-leadtime/analytics-mart.md",
            ".ai/plans/etp-slider/feature-impact.md",
        ],
        "features": FEATURE_CATALOG,
        "charge_path_columns": list(CHARGE_PATH_COLS),
        "model_call_columns": list(MODEL_CALL_COLS),
        "curated_input_columns": list(CURATED_INPUT_COLS),
        "default_anchor_hsa": DEFAULT_ANCHOR_HSA,
        "default_anchor_tolerance_h": DEFAULT_ANCHOR_TOLERANCE_H,
    }


def write_feature_catalog(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(feature_catalog_payload(), indent=2) + "\n")
    logger.info("wrote feature catalog {}", path)
    return path


def load_feature_catalog(path: Path | None = None) -> dict[str, Any]:
    if path is not None and Path(path).exists():
        return json.loads(Path(path).read_text())
    return feature_catalog_payload()


def mapped_mart_columns(catalog: dict[str, Any] | None = None) -> list[str]:
    cat = catalog or feature_catalog_payload()
    return [
        f["mart_column"]
        for f in cat["features"]
        if f.get("status") == "mapped" and f.get("mart_column")
    ]


def nearest_anchor(
    df: pl.DataFrame,
    target_hsa: int = DEFAULT_ANCHOR_HSA,
    *,
    etp_col: str = "etp50",
) -> pl.DataFrame:
    """Per load: ETP at snapshot nearest ``target_hsa`` hours_since_available.

    Report-time helper — do not bake into raw mart schema.
    ``target_hsa=0`` uses the earliest snap (min hours_since_available).
    """
    if target_hsa <= 0:
        return (
            df.sort([ID_COL, "hours_since_available", "snapshot_utc"])
            .group_by(ID_COL)
            .agg(
                pl.col(etp_col).first().alias("p50_anchor"),
                pl.col("hours_since_available").first().alias("hsa_anchor"),
                pl.col("hours_before_pickup").first().alias("hbp_anchor"),
                pl.lit(0.0).alias("anchor_dist_h"),
                pl.col("snapshot_utc").first().alias("anchor_snapshot_utc"),
            )
        )
    return (
        df.with_columns(
            (pl.col("hours_since_available") - target_hsa).abs().alias("_dist")
        )
        .sort([ID_COL, "_dist", "snapshot_utc"])
        .group_by(ID_COL)
        .agg(
            pl.col(etp_col).first().alias("p50_anchor"),
            pl.col("hours_since_available").first().alias("hsa_anchor"),
            pl.col("hours_before_pickup").first().alias("hbp_anchor"),
            pl.col("_dist").first().alias("anchor_dist_h"),
            pl.col("snapshot_utc").first().alias("anchor_snapshot_utc"),
        )
    )


def consecutive_feature_deltas(
    snaps: pl.DataFrame,
    *,
    input_cols: list[str] | None = None,
) -> pl.DataFrame:
    """Within-load consecutive snapshot deltas for etp50 and curated inputs."""
    cols = input_cols or [c for c in CURATED_INPUT_COLS if c in snaps.columns]
    if snaps.is_empty():
        return pl.DataFrame()

    frame = snaps.sort([ID_COL, "snapshot_utc"])
    exprs: list[pl.Expr] = [
        pl.col("snapshot_utc").alias("snapshot_utc_t1"),
        pl.col("snapshot_utc").shift(1).over(ID_COL).alias("snapshot_utc_t0"),
        (pl.col("etp50") - pl.col("etp50").shift(1).over(ID_COL)).alias("delta_etp50"),
        (pl.col("etp10") - pl.col("etp10").shift(1).over(ID_COL)).alias("delta_etp10"),
        (pl.col("hours_since_available") // 24).cast(pl.Int64).alias("prebook_day"),
        (pl.col("hours_before_pickup") // 24).cast(pl.Int64).alias("lead_day"),
    ]
    for c in cols:
        if c in frame.columns and frame.schema[c].is_numeric():
            exprs.append(
                (pl.col(c) - pl.col(c).shift(1).over(ID_COL)).alias(f"delta_{c}")
            )

    out = frame.with_columns(exprs).filter(pl.col("snapshot_utc_t0").is_not_null())
    keep = [
        ID_COL,
        "snapshot_utc_t0",
        "snapshot_utc_t1",
        "delta_etp50",
        "delta_etp10",
        "prebook_day",
        "lead_day",
        *[f"delta_{c}" for c in cols if f"delta_{c}" in out.columns],
    ]
    for meta in ("ship_month", "is_hyperlocal", "loaded_miles"):
        if meta in out.columns:
            keep.append(meta)
    return out.select([c for c in keep if c in out.columns])


def _category_for_column(mart_column: str, catalog: dict[str, Any]) -> int | None:
    for f in catalog.get("features", []):
        if f.get("mart_column") == mart_column:
            return f.get("movement_category")
    return None


def decomp_by_day(
    deltas: pl.DataFrame,
    *,
    by: Literal["prebook_day", "lead_day"] = "prebook_day",
    catalog: dict[str, Any] | None = None,
    min_rows: int = 30,
    stratum_col: str | None = None,
) -> pl.DataFrame:
    """Roll Δetp50 vs Δinputs by day × movement category (simple corr + means)."""
    cat = catalog or feature_catalog_payload()
    if deltas.is_empty() or by not in deltas.columns:
        return pl.DataFrame()

    delta_cols = [
        c
        for c in deltas.columns
        if c.startswith("delta_") and c not in ("delta_etp50", "delta_etp10")
    ]
    rows: list[dict[str, Any]] = []

    strata: list[Any]
    if stratum_col and stratum_col in deltas.columns:
        strata = list(deltas.get_column(stratum_col).unique().to_list())
    else:
        strata = [None]
        stratum_col = None

    for stratum in strata:
        base_df = (
            deltas.filter(pl.col(stratum_col) == stratum)
            if stratum_col is not None
            else deltas
        )
        for day in sorted(base_df.get_column(by).drop_nulls().unique().to_list()):
            sub = base_df.filter(pl.col(by) == day)
            if sub.height < min_rows:
                continue
            base = {
                "day_type": by,
                "day": int(day),
                "n_pairs": sub.height,
                "delta_etp50_mean": sub["delta_etp50"].mean(),
                "delta_etp50_median": sub["delta_etp50"].median(),
            }
            if stratum_col is not None:
                base[stratum_col] = stratum
            for dc in delta_cols:
                feat = dc.removeprefix("delta_")
                mov = _category_for_column(feat, cat)
                valid = sub.select("delta_etp50", dc).drop_nulls()
                if valid.height < min_rows:
                    continue
                corr = valid.select(pl.corr("delta_etp50", dc)).item()
                rows.append(
                    {
                        **base,
                        "feature": feat,
                        "delta_col": dc,
                        "movement_category": mov,
                        "delta_feat_mean": valid[dc].mean(),
                        "delta_feat_abs_mean": valid[dc].abs().mean(),
                        "corr_delta_etp50": corr,
                    }
                )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def decompose_frame(
    snaps: pl.DataFrame,
    *,
    anchor_hsa: int = DEFAULT_ANCHOR_HSA,
    tolerance_h: int = DEFAULT_ANCHOR_TOLERANCE_H,
    by: Literal["prebook_day", "lead_day"] = "prebook_day",
    catalog: dict[str, Any] | None = None,
    is_hyperlocal: bool | None = None,
) -> dict[str, Any]:
    """Report-time decomposition for a feature_snapshots frame (one or many loads)."""
    cat = catalog or feature_catalog_payload()
    model = snaps.filter(pl.col("etp50").is_not_null())
    if is_hyperlocal is not None and "is_hyperlocal" in model.columns:
        model = model.filter(pl.col("is_hyperlocal") == is_hyperlocal)
    if model.is_empty():
        return {"n_snaps": 0, "deltas": pl.DataFrame(), "decomp": pl.DataFrame()}

    anchor = nearest_anchor(model, anchor_hsa)
    eligible = anchor.filter(pl.col("anchor_dist_h") <= tolerance_h)
    deltas = consecutive_feature_deltas(model)
    min_rows = 1 if model.height < 50 else 30
    decomp = decomp_by_day(deltas, by=by, catalog=cat, min_rows=min_rows)
    decomp_hl = pl.DataFrame()
    if "is_hyperlocal" in deltas.columns:
        decomp_hl = decomp_by_day(
            deltas,
            by=by,
            catalog=cat,
            min_rows=min_rows,
            stratum_col="is_hyperlocal",
        )
    return {
        "n_snaps": model.height,
        "n_loads_anchor": eligible.height,
        "anchor_hsa": anchor_hsa,
        "tolerance_h": tolerance_h,
        "is_hyperlocal_filter": is_hyperlocal,
        "anchors": eligible,
        "deltas": deltas,
        "decomp": decomp,
        "decomp_by_hyperlocal": decomp_hl,
        "catalog_version": cat.get("version"),
    }


__all__ = [
    "CHARGE_PATH_COLS",
    "CURATED_INPUT_COLS",
    "DEFAULT_ANCHOR_HSA",
    "DEFAULT_ANCHOR_TOLERANCE_H",
    "FEATURE_CATALOG",
    "FEATURE_HISTORY_CACHE",
    "MODEL_CALL_COLS",
    "consecutive_feature_deltas",
    "decomp_by_day",
    "decompose_frame",
    "feature_catalog_payload",
    "load_feature_catalog",
    "mapped_mart_columns",
    "nearest_anchor",
    "write_feature_catalog",
]
