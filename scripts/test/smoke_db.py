from pathlib import Path

import polars as pl
from arriveds.dataframe import frame_shape
from loguru import logger

PATH_MART = Path("data/etp_lake/mart")
sorted(PATH_MART.glob("*.parquet"))

cat = pl.read_parquet(PATH_MART / "feature_impact/category_by_day.parquet")
cat.filter(pl.col("day").is_between(2, 4)).sort("day", "movement_category")

betas = pl.read_parquet("data/etp_lake/mart/feature_impact/betas_by_prebook_day.parquet")
betas.filter(pl.col("day") == 3).sort("movement_category", "feature")

path_hyperlocal = PATH_MART / "decomp_by_day_hyperlocal.parquet"
dfh = pl.scan_parquet(path_hyperlocal)
logger.info(f"hyperlocal decomposition with {frame_shape(dfh)} shape at {path_hyperlocal}")


import polars as pl

hyp = pl.read_parquet("data/etp_lake/mart/feature_impact/hypothesis_summary.parquet")

# Cat-2 event vs non-event on prebook days 2–4
tests = pl.read_parquet("data/etp_lake/mart/feature_impact/hypothesis_tests.parquet")
tests.filter(
    pl.col("test_name") == "cat2_event_gt_non_event",
    pl.col("day").is_between(2, 4),
).sort("day")

events = pl.read_parquet("data/etp_lake/mart/feature_impact/cat2_events.parquet")
events.group_by("event_feature").len().sort("len", descending=True)


## Impact Analysis

# ```bash
# uv run python scripts/build_etp_lake.py --stages impact,catalog --force
# uv run python scripts/build_etp_lake.py --stages impact,catalog --ship-month 2026-07 --skip-step-contrib
# ```

from dqt.etp_lake import EtpLake

lake = EtpLake(data_dir="data/etp_lake", cache_dir="data/etp_lake/cache")
impact = lake.feature_impact(ship_month="2026-07", anchor_hsa=24, tolerance_h=12)
print(impact)
