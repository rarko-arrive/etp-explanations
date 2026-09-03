"""Matplotlib styling for DQT analysis figures.

Palette and mark conventions follow the validated reference palette
(categorical order fixed, sequential = single blue ramp, diverging =
blue <-> red with neutral gray midpoint, recessive grid/axes).
"""

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

# ---------------------------------------------------------------- palette ---
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

# Fixed categorical order (validated): blue, green, magenta, yellow, aqua, orange
CAT = [
    "#2a78d6",
    "#008300",
    "#e87ba4",
    "#eda100",
    "#1baf7a",
    "#eb6834",
    "#4a3aa7",
    "#e34948",
]

# Semantic assignments used across every notebook — color follows the entity.
C_ETP = CAT[0]  # ETP / DQT-adjusted output  -> blue
C_WM = CAT[1]  # Weatherman raw             -> green
C_LIGHTNING = CAT[2]  # Lightning point estimate   -> magenta
C_OTHER = CAT[3]  # 4th series when needed     -> yellow
DEEMPH = "#c3c2b7"  # context/de-emphasized series

SEQ_BLUES = [
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
]

DIV_LO, DIV_MID, DIV_HI = "#2a78d6", "#f0efec", "#e34948"

cmap_seq = LinearSegmentedColormap.from_list("dqt_seq", SEQ_BLUES)
cmap_div = LinearSegmentedColormap.from_list("dqt_div", [DIV_LO, DIV_MID, DIV_HI])


# ------------------------------------------------------------------ style ---
def apply_style() -> None:
    mpl.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "figure.dpi": 110,
            "savefig.dpi": 150,
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Segoe UI", "Arial"],
            "text.color": INK,
            "axes.edgecolor": BASELINE,
            "axes.labelcolor": INK_2,
            "axes.titlecolor": INK,
            "axes.titlesize": 12,
            "axes.titleweight": "semibold",
            "axes.titlelocation": "left",
            "axes.labelsize": 10,
            "axes.grid": True,
            "axes.axisbelow": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "lines.linewidth": 2.0,
            "lines.solid_capstyle": "round",
            "legend.frameon": False,
            "legend.fontsize": 9,
            "legend.labelcolor": INK_2,
            "axes.prop_cycle": mpl.cycler(color=CAT),
        }
    )


def new_fig(w=9.0, h=4.5, nrows=1, ncols=1, **kw):
    fig, ax = plt.subplots(nrows, ncols, figsize=(w, h), **kw)
    return fig, ax


def escape_mpl_text(text: str) -> str:
    """Escape ``$`` so matplotlib does not parse dollar amounts as mathtext."""
    return text.replace("$", r"\$")


def titles(ax, title: str, subtitle: str | None = None) -> None:
    """Left-aligned title with an optional secondary-ink subtitle."""
    if subtitle:
        ax.set_title(title + "\n", pad=14)
        ax.text(
            0,
            1.02,
            escape_mpl_text(subtitle),
            transform=ax.transAxes,
            fontsize=9.5,
            color=INK_2,
            va="bottom",
        )
    else:
        ax.set_title(title, pad=10)


def target_line(ax, y, label=None, axis="y"):
    f = ax.axhline if axis == "y" else ax.axvline
    ln = f(y, color=MUTED, lw=1.2, ls=(0, (4, 3)), zorder=1)
    if label:
        ax.annotate(
            label,
            xy=(1.0, y),
            xycoords=("axes fraction", "data"),
            xytext=(4, 0),
            textcoords="offset points",
            fontsize=8.5,
            color=MUTED,
            va="center",
        )
    return ln


def end_label(ax, x, y, text, color, dx=5):
    """Direct label at the right end of a line series."""
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(dx, 0),
        textcoords="offset points",
        fontsize=9,
        color=color,
        fontweight="semibold",
        va="center",
    )


def spread_end_labels(ax, x, items, min_gap):
    """Direct-label several series at x, nudging labels apart vertically.

    items: list of (y, text, color). min_gap is in data units.
    """
    items = sorted(items, key=lambda t: t[0])
    ys = [t[0] for t in items]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] < min_gap:
            ys[i] = ys[i - 1] + min_gap
    for (y0, text, color), y in zip(items, ys):
        ax.annotate(
            text,
            xy=(x, y),
            xytext=(5, 0),
            textcoords="offset points",
            fontsize=9,
            color=color,
            fontweight="semibold",
            va="center",
        )


def save_fig(fig, name: str, results_dir: Path, caption: str | None = None):
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"{name}.png"
    fig.savefig(out, bbox_inches="tight")
    print(f"saved {out}")
    return out
