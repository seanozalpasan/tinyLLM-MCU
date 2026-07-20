"""
Report figures for the anomaly eval -- rendering only; no number is made here.

Three PNGs, written for a mixed technical/non-technical audience:
  score_line   -- every capture as a dot on one score axis, one dashed alarm line;
                  the whole eval readable in one look ("right of the line = flagged")
  blob_sweep   -- hidden-payload size vs score: how small before it slips under
  roc_appendix -- the detection-vs-false-alarm trade-off curve + AUC (appendix)

Identity comes from row labels and marker fill, not hue -- one data color plus
neutral grays, so the figures survive colorblind viewing and grayscale print
without a palette to manage.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")   # file output only; never require a display
import matplotlib.pyplot as plt
import numpy as np
from numpy.typing import NDArray

# Light-surface roles from the shared visualization palette (validated set).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"

plt.rcParams.update({"font.family": "sans-serif",
                     "font.sans-serif": ["Segoe UI", "DejaVu Sans"]})

# One marker recipe per row kind: benign recedes, headline carries the story,
# floor/designed-miss stay hollow so "not a headline number" is visible in print.
_KIND_STYLE: dict[str, dict[str, object]] = {
    "benign": dict(c=MUTED, s=16, alpha=0.55, linewidths=0),
    "headline": dict(c=BLUE, s=36, alpha=0.9, edgecolors=SURFACE, linewidths=0.6),
    "floor": dict(facecolors="none", s=36, edgecolors=BLUE, linewidths=1.4),
    "designed_miss": dict(facecolors="none", s=36, edgecolors=MUTED, linewidths=1.4),
}

_HEADLINE_ORDER = ("stride_break", "correlation_break", "out_of_range_value",
                   "nonmonotonic_ts")


def build_rows(benign: NDArray[np.float64], results: list[dict[str, object]],
               pretty: dict[str, str]) -> list[dict[str, object]]:
    """Score-line rows: benign on top, blobs by size, precision tampers, then floor."""
    def row(label: str, values: list[float], kind: str) -> dict[str, object]:
        return {"label": label, "values": values, "kind": kind}

    def of_key(key: str) -> list[float]:
        return [float(r["d"]) for r in results if r["type_key"] == key]

    rows = [row(f"benign training captures (n={len(benign)})",
                [float(d) for d in benign], "benign")]
    blob_sizes = sorted({int(r["nv_changed_bytes"]) for r in results
                         if r["type"] == "foreign_blob"}, reverse=True)
    for size in blob_sizes:
        vals = of_key(f"foreign_blob_{size}B")
        rows.append(row(f"foreign blob, {size} B (n={len(vals)})", vals, "headline"))
    seen = {f"foreign_blob_{s}B" for s in blob_sizes}
    others = sorted({str(r["type_key"]) for r in results
                     if r["bucket"] == "headline"} - seen,
                    key=lambda k: (_HEADLINE_ORDER.index(k)
                                   if k in _HEADLINE_ORDER else len(_HEADLINE_ORDER), k))
    for key in others:
        vals = of_key(key)
        rows.append(row(f"{pretty.get(key, key.replace('_', ' '))} (n={len(vals)})",
                        vals, "headline"))
    for bucket, suffix in (("floor", "floor measure"), ("designed_miss", "designed miss")):
        for key in sorted({str(r["type_key"]) for r in results if r["bucket"] == bucket}):
            vals = of_key(key)
            rows.append(row(f"{pretty.get(key, key.replace('_', ' '))} "
                            f"(n={len(vals)}, {suffix})", vals, bucket))
    return rows


def _base_axes(fig_w: float, fig_h: float) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelcolor=INK2, labelsize=9)
    ax.set_axisbelow(True)
    return fig, ax


def _save(fig: plt.Figure, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return out


def _alarm_note(thr: float) -> str:
    return f"alarm line {thr:.2f}"


def score_line(rows: list[dict[str, object]], thr: float, out: Path) -> Path:
    """One dot per capture on a single score axis; right of the dashed line = flagged."""
    fig, ax = _base_axes(9.0, 0.46 * len(rows) + 1.7)
    rng = np.random.default_rng(0)   # presentation jitter only; nothing numeric
    top = len(rows)
    all_vals: list[float] = []
    for i, r in enumerate(rows):
        vals = np.asarray(r["values"], dtype=np.float64)
        all_vals.extend(vals.tolist())
        y = (top - i) + rng.uniform(-0.13, 0.13, len(vals))
        ax.scatter(vals, y, **_KIND_STYLE[str(r["kind"])])   # type: ignore[arg-type]
    ax.set_yticks([top - i for i in range(len(rows))],
                  [str(r["label"]) for r in rows])
    ax.set_ylim(0.3, top + 0.7)

    log = max(all_vals) > 4 * thr   # keep benign readable when blobs score far out
    if log:
        ax.set_xscale("log")
    ax.axvline(thr, color=INK, lw=1.2, ls=(0, (4, 3)))
    ax.text(thr, 1.01, _alarm_note(thr), transform=ax.get_xaxis_transform(),
            ha="center", va="bottom", color=INK, fontsize=9)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ax.set_xlabel("anomaly score" + (" (log scale)" if log else ""),
                  color=INK2, fontsize=10)
    ax.set_title("Every capture's anomaly score — dots right of the line get flagged",
                 loc="left", color=INK, fontsize=12, pad=14)
    return _save(fig, out)


def blob_sweep(points: list[tuple[int, float, bool]], thr: float, out: Path) -> Path:
    """Hidden-payload size vs score; the gap under the alarm line is the blind spot."""
    fig, ax = _base_axes(6.4, 4.2)
    sizes = sorted({s for s, _, _ in points})
    for s, d, caught in points:
        style = _KIND_STYLE["headline"] if caught else _KIND_STYLE["floor"]
        ax.scatter([s], [d], **style)   # type: ignore[arg-type]
        if not caught:
            ax.annotate("missed", (s, d), textcoords="offset points", xytext=(0, -14),
                        ha="center", color=INK2, fontsize=8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(sizes, [f"{s}" for s in sizes])
    ax.minorticks_off()
    if max(d for _, d, _ in points) > 4 * thr:
        ax.set_yscale("log")
        ax.set_ylabel("anomaly score (log scale)", color=INK2, fontsize=10)
    else:
        ax.set_ylabel("anomaly score", color=INK2, fontsize=10)
    ax.axhline(thr, color=INK, lw=1.2, ls=(0, (4, 3)))
    ax.text(0.99, thr, _alarm_note(thr), transform=ax.get_yaxis_transform(),
            ha="right", va="bottom", color=INK, fontsize=9)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_xlabel("hidden payload size (bytes)", color=INK2, fontsize=10)
    ax.set_title("How small can a hidden payload get before it goes unseen?",
                 loc="left", color=INK, fontsize=12, pad=14)
    return _save(fig, out)


def roc_appendix(pts: list[tuple[float, float]], auc: float, op: tuple[float, float],
                 thr: float, out: Path) -> Path:
    """The threshold-sliding trade-off curve; the dot is the shipped threshold."""
    fig, ax = _base_axes(5.6, 5.2)
    fpr = [p[0] for p in pts]
    tpr = [p[1] for p in pts]
    ax.plot([0, 1], [0, 1], color=GRID, lw=1.0, ls=(0, (4, 3)))
    ax.step(fpr, tpr, where="post", color=BLUE, lw=2.0)
    ax.plot([op[0]], [op[1]], "o", color=INK, ms=7)
    ax.annotate(f"shipped threshold {thr:.2f}\n{op[1]:.0%} caught at "
                f"{op[0]:.1%} false alarms", (op[0], op[1]),
                textcoords="offset points", xytext=(14, -4), ha="left",
                color=INK2, fontsize=9)
    ax.text(0.97, 0.05, f"AUC = {auc:.3f}\n(0.5 = coin flip, 1.0 = perfect)",
            transform=ax.transAxes, ha="right", color=INK2, fontsize=9)
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.set_aspect("equal")
    ax.grid(color=GRID, lw=0.8)
    ax.set_xlabel("fraction of benign captures wrongly flagged", color=INK2, fontsize=10)
    ax.set_ylabel("fraction of anomalies caught", color=INK2, fontsize=10)
    ax.set_title("Detection vs false alarms as the threshold slides",
                 loc="left", color=INK, fontsize=12, pad=14)
    return _save(fig, out)
