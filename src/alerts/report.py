"""Figures and the README results block, generated from reports/*.json."""

from __future__ import annotations

import json
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import config  # noqa: E402

README = config.ROOT / "README.md"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = {"pre_break": "#2a78d6", "post_break": "#eb6834"}
LABEL = {"pre_break": "Before the break (Jun 1 - Aug 22)", "post_break": "After the break (Sep 14 - last day)"}
BASE_NAMES = {"yesterday": "yesterday", "lastweek": "same weekday last week", "mean7": "mean of last 7 days", "mean28": "mean of last 28 days"}


def _load(name: str) -> dict:
    return json.loads((config.REPORTS_DIR / name).read_text())


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def fig_daily_hours(daily: pd.DataFrame) -> None:
    d = daily[daily["day"] >= "2025-01-01"]
    s = d.groupby("day").apply(lambda x: x["hours"].where(x["valid"]).mean())
    s.index = pd.to_datetime(s.index)
    fig, ax = plt.subplots(figsize=(10, 3.6), facecolor=SURFACE)
    _style(ax)
    ax.plot(s.index, s.values, color="#9ec3ee", linewidth=1)
    ax.plot(s.index, s.rolling(7, min_periods=4).mean().values, color=SERIES["pre_break"], linewidth=2)
    for d0, d1 in (("2026-08-29", "2026-09-03"), ("2026-09-06", "2026-09-08")):
        ax.axvspan(pd.Timestamp(d0), pd.Timestamp(d1), color=GRID, alpha=0.9, linewidth=0)
    ax.axvline(pd.Timestamp("2026-09-07"), color=INK2, linewidth=1, linestyle="--")
    ax.annotate("source switch + two-level alerts\n(Sep 6-7, 2026)", xy=(pd.Timestamp("2026-09-07"), np.nanmax(s.values) * 0.92),
                xytext=(-10, 0), textcoords="offset points", ha="right", va="top", fontsize=8.5, color=INK2)
    ax.set_ylabel("hours under alert per region-day", color=INK2, fontsize=9)
    ax.set_title("Average alert hours per region and day (thin: daily, thick: 7-day mean; grey: excluded days)", loc="left", fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(config.REPORTS_DIR / "fig_daily_hours.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def fig_reliability(m2: dict) -> None:
    hs = ("1", "3", "6")
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.6), facecolor=SURFACE, sharey=True)
    for ax, h in zip(axes, hs):
        _style(ax)
        ax.plot([0, 1], [0, 1], color=INK2, linewidth=1, linestyle=":")
        for w in ("pre_break", "post_break"):
            rel = [b for b in m2["windows"][w]["horizons"][h]["reliability"] if b["n"] >= 30]
            ax.plot([b["mean_forecast"] for b in rel], [b["observed"] for b in rel], color=SERIES[w], linewidth=2, marker="o", markersize=5, label=LABEL[w])
        ax.set_title(f"alert starts within {h} h", fontsize=10, color=INK, loc="left")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel("forecast probability", color=INK2, fontsize=9)
    axes[0].set_ylabel("observed frequency", color=INK2, fontsize=9)
    axes[0].legend(frameon=False, fontsize=8.5, loc="upper left", labelcolor=INK2)
    fig.suptitle("Phase 2 calibration (bins with >= 30 forecasts; dotted = perfect)", x=0.01, ha="left", fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(config.REPORTS_DIR / "fig_reliability.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def _ci(v) -> str:
    return f"[{v[0]:+.1%}, {v[1]:+.1%}]"


def results_markdown(m1: dict, m2: dict) -> str:
    out = []
    out.append("#### Phase 1: alerts and alert hours per region for the coming Kyiv day (forecast at 00:00)\n")
    out.append("Mean absolute error per region-day. Skill = 1 - MAE(model) / MAE(best naive forecast), 90% interval from a day-level bootstrap.\n")
    out.append("| Window | Target | Days | Actual mean | Model MAE | Best naive forecast | Its MAE | Skill [90% CI] |")
    out.append("|---|---|---:|---:|---:|---|---:|---|")
    for w in ("pre_break", "post_break"):
        x = m1["windows"][w]
        for tgt, name in (("count", "alerts"), ("hours", "hours")):
            t = x["targets"][tgt]
            b = t["best_baseline"]
            mb = t["methods"][b]
            out.append(f"| {LABEL[w]} | {name} | {x['n_days']} | {t['mean_actual']:.2f} | {t['methods']['model']['mae']:.3f} | {BASE_NAMES[b]} | {mb['mae']:.3f} | {mb['model_skill']:+.1%} {_ci(mb['model_skill_ci90'])} |")
    out.append("")
    out.append("#### Phase 2: probability that an alert starts in the region within the next h hours\n")
    out.append("Scored only at moments with no alert in progress (otherwise the answer is \"alert in progress\"). BSS = Brier skill vs the 28-day climatology of the same region and hour of day, 90% bootstrap interval.\n")
    out.append("| Window | h | Forecasts | Base rate | Brier model | Brier climatology | BSS [90% CI] | AUC model | AUC climatology |")
    out.append("|---|---:|---:|---:|---:|---:|---|---:|---:|")
    for w in ("pre_break", "post_break"):
        for h, v in m2["windows"][w]["horizons"].items():
            mm, mc = v["methods"]["model"], v["methods"]["clim"]
            out.append(f"| {LABEL[w]} | {h} | {v['n']} | {v['base_rate']:.3f} | {mm['brier']:.4f} | {mc['brier']:.4f} | {mc['model_bss']:+.1%} {_ci(mc['model_bss_ci90'])} | {mm['auc']:.3f} | {mc['auc']:.3f} |")
    return "\n".join(out)


def update_readme(block: str) -> None:
    text = README.read_text()
    pat = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if not pat.search(text):
        raise RuntimeError("README has no results markers")
    README.write_text(pat.sub(START + "\n" + block + "\n" + END, text))


def run() -> None:
    from . import datasets, stitch

    m1, m2 = _load("metrics_phase1.json"), _load("metrics_phase2.json")
    D = datasets.build(stitch.load_stitched(), config.DATA_END_UTC)
    fig_daily_hours(D.daily)
    fig_reliability(m2)
    update_readme(results_markdown(m1, m2))
    print("figures and README results updated")
