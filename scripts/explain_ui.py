"""Render a self-contained HTML explainer for one ETP load.

Example
-------
    uv run python scripts/explain_ui.py --load 6963033 --open
    uv run python scripts/explain_ui.py --rank 2 --out data/explain-shipments/explain.html
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from loguru import logger

load_dotenv(find_dotenv())

from app.explain import (
    DEFAULT_EXPLAIN_OUTPUT_DIR,
    build_view_model,
    render_explain_html,
)
from dqt import resolve_data_dir
from dqt.etp_lake import EtpLake
from dqt.etp_lifecycle import COHORT_HC, explain_load, resolve_load_id
from dqt.etp_timeline import plot_etp_timeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--load", type=int, default=None, help="explicit loadnumber")
    parser.add_argument("--cohort", default=COHORT_HC)
    parser.add_argument("--rank", type=int, default=1, help="HC outlier rank when --load omitted")
    parser.add_argument("--davis-cache", default=None)
    parser.add_argument("--mde-cache", default=None)
    parser.add_argument(
        "--query-mde",
        action="store_true",
        help="pull MDE from Snowflake (slow; default uses cache only)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"output HTML path (default: {DEFAULT_EXPLAIN_OUTPUT_DIR}/explain-{{loadnumber}}.html)",
    )
    parser.add_argument("--open", action="store_true", help="open HTML in default browser")
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    lake = EtpLake(data_dir)
    davis = Path(args.davis_cache) if args.davis_cache else None
    mde = Path(args.mde_cache) if args.mde_cache else None

    loadnumber = resolve_load_id(
        cohort=args.cohort,  # type: ignore[arg-type]
        rank=args.rank,
        load_id=args.load,
        data_dir=data_dir,
        davis_cache=davis,
    )
    logger.info("rendering explainer for load {}", loadnumber)

    result = explain_load(
        lake,
        loadnumber,
        mde_cache=mde,
        query_mde=args.query_mde,
        davis_cache=davis,
        data_dir=data_dir,
    )
    timeline = plot_etp_timeline(
        lake,
        loadnumber,
        show=False,
        mde_cache=mde,
        query_mde=args.query_mde,
        data_dir=data_dir,
    )
    view_model = build_view_model(
        result,
        timeline_figure=timeline["figure"],
        cohort=args.cohort,
        rank=None if args.load else args.rank,
    )
    html = render_explain_html(view_model)

    out_path = (
        Path(args.out)
        if args.out
        else DEFAULT_EXPLAIN_OUTPUT_DIR / f"explain-{loadnumber}.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    logger.info("wrote {}", out_path.resolve())

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
