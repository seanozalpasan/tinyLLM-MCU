"""
Poster asset generator: print-grade figures for the 4 ft x 3 ft poster.

Renders into docs/demo_assets/ (git-ignored) at 300 dpi, sized so a panel
prints ~12 inches wide with text readable from a few feet. Rendering only --
every number comes from the banked eval (offdevice/eval/results/
eval_results.json), the deployed model metadata, or the banked chain capture;
nothing is recomputed and nothing under offdevice/eval/results/ is touched.

Each figure ships in two skins: _dark (the demo's black/green/red terminal
identity, matches the live meter screenshots) and _light (the committed eval
figures' blue/gray language, for a white poster). The console receipt is
dark-only -- it depicts a terminal. receipt_chain.txt is the plain-text twin
for on-screen use in PowerPoint.

    python -m offdevice.demo.make_poster_assets
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")   # file output only; never require a display
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_JSON = REPO_ROOT / "offdevice" / "eval" / "results" / "eval_results.json"
OUT_DIR = REPO_ROOT / "docs" / "demo_assets"
DPI = 300

# Measured live benign ceiling + the soak that measured it. These two update
# TOGETHER after any longer verification soak (e.g. the overnight run) -- edit
# here, re-run this script, done. Everything else is derived from the eval
# results file, so a collaborator redelivery re-renders with no code change.
LIVE_CEILING = 12.742
SOAK_LABEL = "120-minute verification soak"

# ---- skins ------------------------------------------------------------------
DARK = dict(bg="#000000", ink="#9be8b3", ink2="#5d8a6c", grid="#123420",
            good="#00FF41", alarm="#FF2B2B", note="#FFB000", suffix="dark")
LIGHT = dict(bg="#fcfcfb", ink="#0b0b0b", ink2="#52514e", grid="#e1e0d9",
             good="#2a78d6", alarm="#b3261e", note="#52514e", suffix="light")


def _axes(skin: dict[str, str], w: float, h: float) -> tuple[plt.Figure, plt.Axes]:
    plt.rcParams["font.family"] = "monospace" if skin is DARK else "sans-serif"
    fig, ax = plt.subplots(figsize=(w, h), facecolor=skin["bg"])
    ax.set_facecolor(skin["bg"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(skin["ink2"])
    ax.tick_params(colors=skin["ink2"], labelcolor=skin["ink"], labelsize=12)
    ax.set_axisbelow(True)
    return fig, ax


def _save(fig: plt.Figure, skin: dict[str, str], stem: str) -> Path:
    out = OUT_DIR / f"{stem}_{skin['suffix']}.png"
    fig.savefig(out, dpi=DPI, facecolor=skin["bg"], bbox_inches="tight")
    plt.close(fig)
    return out


# ---- figure 1: the corridor ---------------------------------------------------
def corridor(skin: dict[str, str], thr: float, base_lo: float,
             floor_d: float) -> Path:
    """One horizontal score axis: benign territory, the alarm line, the
    weakest caught attack, and the demo implant pointing off-scale."""
    # Annotation rows get exclusive vertical lanes (0.93 / 0.74-0.55 / 0.45 /
    # 0.30) so no two labels can collide at print size.
    fig, ax = _axes(skin, 12.0, 3.8)
    ax.set_ylim(0, 1)
    ax.set_xlim(0, 18.0)
    ax.get_yaxis().set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.grid(axis="x", color=skin["grid"], lw=0.8)

    ax.axvspan(base_lo, LIVE_CEILING, color=skin["good"], alpha=0.16)
    ax.axvline(LIVE_CEILING, color=skin["good"], lw=2.0)
    mid = (base_lo + LIVE_CEILING) / 2
    ax.text(mid, 0.74, "benign territory", ha="center",
            color=skin["good"], fontsize=17, fontweight="bold")
    ax.text(mid, 0.62, "every score the running device produced\nin its "
            f"{SOAK_LABEL}", ha="center", va="top",
            color=skin["ink"], fontsize=11.5)
    ax.text(LIVE_CEILING - 0.15, 0.30,
            f"highest benign ever seen live: {LIVE_CEILING}",
            ha="right", va="center", color=skin["good"], fontsize=12)

    ax.axvline(thr, color=skin["alarm"], lw=3.0)
    ax.text(thr + 0.1, 0.93, f"ALARM LINE {thr:.3f}", ha="left", va="center",
            color=skin["alarm"], fontsize=16, fontweight="bold")

    ax.plot([floor_d], [0.45], "o", ms=13, color=skin["alarm"])
    ax.text(floor_d + 0.25, 0.45, f"weakest caught: {floor_d:.3f}\n"
            "(512 B, fullest ring)", ha="left", va="center",
            color=skin["ink"], fontsize=11.5)

    ax.set_xlabel("anomaly score", color=skin["ink"], fontsize=13)
    ax.set_title("The corridor: benign life vs the alarm line",
                 loc="left", color=skin["ink"], fontsize=18, pad=14)
    return _save(fig, skin, "corridor")


# ---- figure 2: payload-size sweep ---------------------------------------------
def blob_sweep(skin: dict[str, str], thr: float, rows: list[dict[str, object]],
               base_lo: float, base_hi: float) -> Path:
    """Bytes changed vs score for all 16 collaborator attacks; the region
    under the alarm line is the deliberate micro-tamper blind spot."""
    fig, ax = _axes(skin, 9.5, 6.4)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.grid(color=skin["grid"], lw=0.8)

    ax.axhspan(base_lo, base_hi, color=skin["good"], alpha=0.16)
    ax.text(0.985, base_hi * 0.96, "untampered captures score in this band",
            transform=ax.get_yaxis_transform(), ha="right", va="top",
            color=skin["good"], fontsize=11.5)
    ax.axhline(thr, color=skin["alarm"], lw=2.5)
    ax.text(0.985, thr * 1.04, f"ALARM LINE {thr:.3f}",
            transform=ax.get_yaxis_transform(), ha="right", va="bottom",
            color=skin["alarm"], fontsize=13, fontweight="bold")

    sizes = sorted({int(r["nv_changed_bytes"]) for r in rows})
    for r in rows:
        x, d = int(r["nv_changed_bytes"]), float(r["d"])
        if bool(r["flagged"]):
            ax.plot([x], [d], "o", ms=11, color=skin["alarm"])
        else:
            ax.plot([x], [d], "o", ms=11, markerfacecolor="none",
                    markeredgecolor=skin["ink2"], markeredgewidth=1.8)
    ax.set_xticks(sizes, [str(s) for s in sizes])
    ax.minorticks_off()
    ax.set_xlabel("bytes the attacker changed (of 4096)", color=skin["ink"],
                  fontsize=13)
    ax.set_ylabel("anomaly score (log scale)", color=skin["ink"], fontsize=13)
    # Behavior legend lives in the subtitle, where no dot can collide with it;
    # two stacked lines so the canvas never grows wider than the plot. Counts
    # are derived, so a collaborator redelivery re-renders without edits.
    n_caught = sum(bool(r["flagged"]) for r in rows)
    miss_max = max((int(r["nv_changed_bytes"]) for r in rows
                    if not bool(r["flagged"])), default=0)
    ax.set_title(f"All {len(rows)} real attacks: how big before the model "
                 "sees it?", loc="left", color=skin["ink"], fontsize=18, pad=52)
    ax.text(0, 1.02, f"solid = caught (n={n_caught})\nhollow = missed by design "
            f"(micro-tampers <= {miss_max} B belong to firmware rule-checks)",
            transform=ax.transAxes, color=skin["ink"], fontsize=11.5,
            va="bottom")
    return _save(fig, skin, "blob_sweep")


# ---- figure 3: the console receipt --------------------------------------------
# Verbatim lines from docs/soaks/chain_capture.txt (+05:59 .. +09:34), sha256
# shortened for print; amber ">>>" rows are narration, styled so they can never
# be mistaken for console output.
RECEIPT: list[tuple[str, str]] = [
    ("con",   "[+05:59] [IDS] scan #3 score=8.690 slot=27/122 seq=4 benign"),
    ("con",   "[+06:23] [IDS] scan #4 score=8.635 slot=29/122 seq=4 benign"),
    ("note",  ">>> attacker plants 1 KB of foreign payload in the data region"),
    ("con",   "[+06:43] [NVLOG] FAULT: flash program failed; logging stopped"),
    ("alarm", "[+06:49] [IDS] scan #5 score=99.573 rescan=99.573 slot=72/122 seq=20"),
    ("alarm", "         ANOMALY -- withholding watchdog kick, reset imminent"),
    ("note",  ">>> the starved watchdog hard-resets the board"),
    ("con",   "[+06:53] [HASH] static region OK: sha256=a22895d5..."),
    ("con",   "[+06:53] [NVLOG] init: seq=20 boot=16 op=2390 slot=72/122 units=F,inHg"),
    ("note",  ">>> note the ring identity: seq/boot/units are the FOREIGN image's"),
    ("alarm", "[+07:18] [IDS] scan #1 score=99.573 rescan=99.573 slot=72/122 seq=20"),
    ("alarm", "         ANOMALY -- withholding watchdog kick, reset imminent"),
    ("note",  ">>> the payload persists in flash: five detect->reset chains in all"),
    ("note",  ">>> defender erases the data region; the board starts a fresh ring"),
    ("con",   "[+08:53] [NVLOG] init: seq=0 boot=1 op=0 slot=0/122 units=C,hPa"),
    ("con",   "[+09:34] [IDS] scan #1 score=9.459 slot=2/122 seq=1 benign"),
]


def receipt() -> tuple[Path, Path]:
    skin = DARK
    color = {"con": skin["good"], "alarm": skin["alarm"], "note": skin["note"]}
    n = len(RECEIPT)
    width_chars = max(len(t) for _, t in RECEIPT)
    fig_w = 12.0
    font = (fig_w * 72 * 0.94) / (width_chars * 0.62)   # fit longest line
    line_h = font * 1.55 / 72
    fig_h = n * line_h + 1.3
    fig = plt.figure(figsize=(fig_w, fig_h), facecolor=skin["bg"])
    fig.text(0.03, 1 - 0.45 / fig_h,
             "The moment of detection -- board console, recorded live",
             color=skin["ink"], fontsize=17, family="monospace",
             fontweight="bold", va="center")
    for i, (kind, text) in enumerate(RECEIPT):
        y = 1 - (1.0 + (i + 0.5) * line_h) / fig_h
        fig.text(0.03, y, text, color=color[kind], fontsize=font,
                 family="monospace", va="center",
                 fontstyle="italic" if kind == "note" else "normal")
    out_png = OUT_DIR / "receipt_chain_dark.png"
    fig.savefig(out_png, dpi=DPI, facecolor=skin["bg"])
    plt.close(fig)

    out_txt = OUT_DIR / "receipt_chain.txt"
    out_txt.write_text("\n".join(t for _, t in RECEIPT) + "\n", encoding="utf-8")
    return out_png, out_txt


def main() -> int:
    data = json.loads(EVAL_JSON.read_text())
    thr = float(data["threshold"])
    rows = [r for r in data["per_file"]]
    bases = [float(v) for v in data["base_scores"].values()]
    base_lo, base_hi = min(bases), max(bases)
    floor_d = min(float(r["d"]) for r in rows
                  if r["type"] == "foreign_blob" and bool(r["flagged"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for skin in (DARK, LIGHT):
        written.append(corridor(skin, thr, base_lo, floor_d))
        written.append(blob_sweep(skin, thr, rows, base_lo, base_hi))
    written.extend(receipt())
    for p in written:
        print(f"[assets] wrote {p.relative_to(REPO_ROOT)}")
    print(f"[assets] {len(written)} files; corridor floor derived = "
          f"{floor_d:.3f} (banked: 14.284 -- these must agree)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
