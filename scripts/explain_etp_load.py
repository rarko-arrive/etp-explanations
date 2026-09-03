"""Explain ETP lifecycle material changes for a single load.

Example
-------
    uv run python scripts/explain_etp_load.py --load 9199475
    uv run python scripts/explain_etp_load.py --rank 2 --json-out /tmp/9199475.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from loguru import logger

load_dotenv(find_dotenv())

import polars as pl

from dqt import resolve_data_dir
from dqt.etp_lake import EtpLake
from dqt.etp_lifecycle import (
    COHORT_HC,
    explain_load,
    format_pricing_accuracy,
    ledger_to_json,
    resolve_load_id,
    select_outlier_cohort,
)


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
    parser.add_argument("--refresh-cohort", action="store_true")
    parser.add_argument("--json-out", default=None, help="write full explanation JSON")
    parser.add_argument("--top-drivers", type=int, default=5)
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    lake = EtpLake(data_dir)
    davis = Path(args.davis_cache) if args.davis_cache else None
    mde = Path(args.mde_cache) if args.mde_cache else None

    if args.refresh_cohort:
        hc = select_outlier_cohort(
            cohort=args.cohort,  # type: ignore[arg-type]
            data_dir=data_dir,
            davis_cache=davis,
            refresh=True,
        )
        logger.info("refreshed HC index: {:,} loads", hc.height)

    loadnumber = resolve_load_id(
        cohort=args.cohort,  # type: ignore[arg-type]
        rank=args.rank,
        load_id=args.load,
        data_dir=data_dir,
        davis_cache=davis,
    )
    logger.info("explaining load {}", loadnumber)

    result = explain_load(
        lake,
        loadnumber,
        mde_cache=mde,
        query_mde=args.query_mde,
        davis_cache=davis,
        data_dir=data_dir,
    )
    summary = result["summary"]
    print(summary["one_liner"])
    print()
    print("Attribution card:")
    for i, row in enumerate(summary["attribution_card"][: args.top_drivers], start=1):
        print(
            f"  {i}. [{row['category']}] {row['driver_text']} "
            f"(${row['delta_usd']:+,.0f}) @ {row['hours_before_pickup']}h before pickup"
        )

    ledger = result["ledger"]
    if ledger.is_empty():
        pricing_n = display_n = 0
    else:
        pricing_n = ledger.filter(pl.col("is_pricing_driver") == True).height
        display_n = ledger.filter(pl.col("is_pricing_driver") == False).height
    print()
    print(f"Ledger: {ledger.height} rows ({pricing_n} pricing, {display_n} display)")

    lightning = result.get("lightning") or {}
    endpoint = lightning.get("endpoint_summary") or {}
    if endpoint:
        print()
        print("Lightning endpoint:")
        for k in ("clhp_pred_tot_cost_delta", "clhp_pred_tot_cost_rel_pct", "clhp_change_ind"):
            if k in endpoint and endpoint[k] is not None:
                print(f"  {k}: {endpoint[k]}")
    lt_n = (
        ledger.filter(pl.col("driver_text").str.contains("Lightning")).height
        if not ledger.is_empty()
        else 0
    )
    if lt_n:
        print(f"  ledger Lightning rows: {lt_n}")

    pricing_lines = format_pricing_accuracy(result.get("pricing_accuracy"))
    if pricing_lines:
        print()
        print("\n".join(pricing_lines))

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        export = {
            "loadnumber": loadnumber,
            "summary": summary,
            "attribution_card": summary["attribution_card"],
            "ledger": ledger_to_json(ledger),
            "endpoint_shifts": summary["endpoint_shifts"],
            "movement": result.get("movement"),
            "lightning": result.get("lightning"),
            "pricing_accuracy": result.get("pricing_accuracy"),
        }
        out_path.write_text(json.dumps(export, indent=2, default=str))
        logger.info("wrote {}", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
