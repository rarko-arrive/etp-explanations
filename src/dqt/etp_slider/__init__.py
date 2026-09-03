"""ETP slider analysis — SQL pulls, movement taxonomy, problem loads, drift reports.

Subpackages
-----------
sql
    Snowflake SQL rendering and parquet cache names.
movement
    Davis order enrichment and movement-category taxonomy.
problem_loads
    Tail cohort flags, LeadtimeChange deep-dive.
drift
    HTML drift reports (mart + survival).

Legacy flat imports (``dqt.etp_tail``, ``dqt.etp_drift_report``, …) remain
valid via thin re-export shims at the old paths.
"""

from __future__ import annotations

from dqt.etp_slider.sql import (
    COMPOSITE_NAMES,
    COMPOSITE_TAILS,
    CTE_FILE,
    HISTORY_CACHE,
    PER_LOAD_CACHE,
    SHIP_DATE_END,
    SHIP_DATE_START,
    SQL_DIR,
    SURVIVAL_CTE_FILE,
    SURVIVAL_HISTORY_CACHE,
    SURVIVAL_PER_LOAD_CACHE,
    SURVIVAL_TAIL_FILES,
    TAIL_FILES,
    render_etp_slider_sql,
    shift_aggregate,
    slider_sql,
    survival_slider_sql,
    write_composite_sql_files,
)

__all__ = [
    "COMPOSITE_NAMES",
    "COMPOSITE_TAILS",
    "CTE_FILE",
    "HISTORY_CACHE",
    "PER_LOAD_CACHE",
    "SHIP_DATE_END",
    "SHIP_DATE_START",
    "SQL_DIR",
    "SURVIVAL_CTE_FILE",
    "SURVIVAL_HISTORY_CACHE",
    "SURVIVAL_PER_LOAD_CACHE",
    "SURVIVAL_TAIL_FILES",
    "TAIL_FILES",
    "render_etp_slider_sql",
    "shift_aggregate",
    "slider_sql",
    "survival_slider_sql",
    "write_composite_sql_files",
]
