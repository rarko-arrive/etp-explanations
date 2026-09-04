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

from app.explain import DEFAULT_EXPLAIN_OUTPUT_DIR
from app.explain.service import (
    ExplainOptions,
    render_explanation_for_load,
    write_cached_html,
)
from dqt import resolve_data_dir
from dqt.etp_lifecycle import COHORT_HC, resolve_load_id


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

    opts = ExplainOptions(
        data_dir=data_dir,
        mde_cache=mde,
        davis_cache=davis,
        query_mde=args.query_mde,
        cohort=args.cohort,
        rank=None if args.load else args.rank,
    )
    html = render_explanation_for_load(loadnumber, opts)

    out_path = Path(args.out) if args.out else write_cached_html(loadnumber, html)
    if args.out:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html)

    logger.info("wrote {}", out_path.resolve())

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
