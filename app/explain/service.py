"""Shared explainer pipeline and disk cache helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.explain.render import render_explain_html
from app.explain.view_model import build_view_model
from dqt import resolve_data_dir
from dqt.etp_lake import EtpLake
from dqt.etp_lifecycle import explain_load
from dqt.etp_timeline import plot_etp_timeline

DEFAULT_EXPLAIN_OUTPUT_DIR = Path("data/explain-shipments")


def resolve_explain_cache_dir(explicit: Path | str | None = None) -> Path | None:
    """Resolve writable HTML cache directory for explain pages."""
    if explicit is not None:
        return Path(explicit)
    raw = (os.environ.get("EXPLAIN_CACHE_DIR") or "").strip()
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return None


if TYPE_CHECKING:
    from dqt.etp_lake import EtpLake as EtpLakeType


class LoadNotFoundError(Exception):
    """Raised when etp-lake has no history for the requested load."""


@dataclass
class ExplainOptions:
    data_dir: Path | str | None = None
    mde_cache: Path | None = None
    davis_cache: Path | None = None
    query_mde: bool = False
    cohort: str | None = None
    rank: int | None = None
    lake: EtpLakeType | None = None


def cache_path_for_load(loadnumber: int, *, output_dir: Path | None = None) -> Path:
    root = output_dir or DEFAULT_EXPLAIN_OUTPUT_DIR
    return root / f"explain-{loadnumber}.html"


def read_cached_html(loadnumber: int, *, output_dir: Path | None = None) -> str | None:
    path = cache_path_for_load(loadnumber, output_dir=output_dir)
    if not path.is_file():
        return None
    return path.read_text()


def write_cached_html(
    loadnumber: int,
    html: str,
    *,
    output_dir: Path | None = None,
) -> Path:
    path = cache_path_for_load(loadnumber, output_dir=output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)
    return path


def render_explanation_for_load(loadnumber: int, opts: ExplainOptions) -> str:
    """Run explain_load + plot_etp_timeline and return rendered HTML."""
    import matplotlib

    matplotlib.use("Agg", force=True)

    data_dir = resolve_data_dir(opts.data_dir)
    lake = opts.lake if opts.lake is not None else EtpLake(data_dir)

    try:
        result = explain_load(
            lake,
            loadnumber,
            mde_cache=opts.mde_cache,
            query_mde=opts.query_mde,
            davis_cache=opts.davis_cache,
            data_dir=data_dir,
        )
        timeline = plot_etp_timeline(
            lake,
            loadnumber,
            show=False,
            mde_cache=opts.mde_cache,
            query_mde=opts.query_mde,
            data_dir=data_dir,
        )
    except ValueError as exc:
        msg = str(exc)
        if "No ETP history" in msg or "Insufficient timeline data" in msg:
            raise LoadNotFoundError(f"No ETP history for load {loadnumber}") from exc
        raise

    view_model = build_view_model(
        result,
        timeline_figure=timeline["figure"],
        cohort=opts.cohort,
        rank=opts.rank,
    )
    return render_explain_html(view_model)
