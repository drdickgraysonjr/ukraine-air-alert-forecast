"""Figures and the README results block, generated from reports/*.json."""

from __future__ import annotations

import json
import re

import matplotlib
import matplotlib.dates

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import config  # noqa: E402

README = config.ROOT / "README.md"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
CSTART, CEND = "<!-- CHOICES:START -->", "<!-- CHOICES:END -->"
TYPE_ORDER = ("drone", "missile", "ballistic", "kab", "aviation", "all_clear", "alert", "artillery")
CHANNEL_NAMES = {"kpszsu": "Air Force (@kpszsu)", "war_monitor": "@war_monitor", "eradar": "eRadar (@eRadarrua)", "alarm_signal": "@UkraineAlarmSignal"}

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


def fig_zones(daily: pd.DataFrame) -> None:
    """Alert hours per day: each zone against the rest of its oblast (7-day means)."""
    z = daily[(daily["kind"] == "zone") & daily["valid"] & (daily["day"] >= "2025-01-01")]
    regs = [r for r in z["region_id"].value_counts().index if (z["region_id"] == r).sum() >= 60][:4]
    fig, axes = plt.subplots(1, len(regs), figsize=(10, 3.2), facecolor=SURFACE, sharey=True)
    axes = np.atleast_1d(axes)
    for ax, zid in zip(axes, regs):
        _style(ax)
        rid = zid.split("~")[0]
        for sid, color, name in ((rid, SERIES["pre_break"], "rest of the oblast"), (zid, SERIES["post_break"], "zone")):
            x = daily[(daily["region_id"] == sid) & daily["valid"] & (daily["day"] >= "2025-01-01")].set_index("day")["hours"]
            x.index = pd.to_datetime(x.index)
            x = x.asfreq("D")
            ax.plot(x.index, x.rolling(7, min_periods=4).mean().values, color=color, linewidth=1.6, label=name)
        ax.set_title(rid.replace("_", " ").title(), fontsize=10, color=INK, loc="left")
        ax.xaxis.set_major_locator(matplotlib.dates.MonthLocator(bymonth=(1, 7)))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b\n%Y"))
    axes[0].set_ylabel("alert hours per day (7-day mean)", color=INK2, fontsize=9)
    axes[-1].legend(frameon=False, fontsize=8.5, loc="lower right", labelcolor=INK2)
    fig.suptitle("Zones of regular alerts against the rest of their oblast (days when the zone has members)", x=0.01, ha="left", fontsize=10, color=INK)
    fig.tight_layout()
    fig.savefig(config.REPORTS_DIR / "fig_zones.png", dpi=150, facecolor=SURFACE)
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


def _phase1_rows(windows: dict, rows_label: str) -> list[str]:
    out = [f"| Window | Target | {rows_label} | Actual mean | Model MAE | Best naive forecast | Its MAE | Skill [90% CI] |",
           "|---|---|---:|---:|---:|---|---:|---|"]
    for w in ("pre_break", "post_break"):
        x = windows[w]
        for tgt, name in (("count", "alerts"), ("hours", "hours")):
            t = x["targets"][tgt]
            b = t["best_baseline"]
            mb = t["methods"][b]
            n = x["n_days"] if rows_label == "Days" else x["n_rows"]
            out.append(f"| {LABEL[w]} | {name} | {n} | {t['mean_actual']:.2f} | {t['methods']['model']['mae']:.3f} | {BASE_NAMES[b]} | {mb['mae']:.3f} | {mb['model_skill']:+.1%} {_ci(mb['model_skill_ci90'])} |")
    return out


def _phase2_rows(windows: dict) -> list[str]:
    out = ["| Window | h | Forecasts | Base rate | Brier model | Brier climatology | BSS [90% CI] | AUC model | AUC climatology |",
           "|---|---:|---:|---:|---:|---:|---|---:|---:|"]
    for w in ("pre_break", "post_break"):
        for h, v in windows[w]["horizons"].items():
            mm, mc = v["methods"]["model"], v["methods"]["clim"]
            auc = lambda x: "–" if x is None else f"{x:.3f}"  # noqa: E731
            out.append(f"| {LABEL[w]} | {h} | {v['n']} | {v['base_rate']:.3f} | {mm['brier']:.4f} | {mc['brier']:.4f} | {mc['model_bss']:+.1%} {_ci(mc['model_bss_ci90'])} | {auc(mm['auc'])} | {auc(mc['auc'])} |")
    return out


def results_markdown(m1: dict, m2: dict) -> str:
    out = []
    out.append("#### Phase 1: alerts and alert hours per region for the coming Kyiv day (forecast at 00:00)\n")
    out.append("Mean absolute error per region-day (the region series: oblast- and raion-level alerts, i.e. the oblast outside its zone of regular alerts). Skill = 1 - MAE(model) / MAE(best naive forecast), 90% interval from a day-level bootstrap.\n")
    out += _phase1_rows(m1["windows"], "Days")
    out.append("")
    out.append("#### Phase 2: probability that an alert starts in the region within the next h hours\n")
    out.append("Scored only at moments with no alert in progress (otherwise the answer is \"alert in progress\"). BSS = Brier skill vs the 28-day climatology of the same region and hour of day, 90% bootstrap interval.\n")
    out += _phase2_rows(m2["windows"])
    out.append("")
    zs = ", ".join(z.split("~")[0] for z in m1["meta"]["zone_series"])
    out.append(f"#### Zones of regular alerts: phase 1 (zone-days; zones: {zs})\n")
    out += _phase1_rows(m1["zones"]["windows"], "Zone-days")
    out.append("")
    out.append("#### Zones of regular alerts: phase 2\n")
    out += _phase2_rows(m2["zones"]["windows"])
    return "\n".join(out)


def _pct(x) -> str:
    return "–" if x is None else f"{x:.1%}"


def choices_markdown(v: dict, parsers: dict, ablation: dict, cross: dict) -> str:
    out = ["#### Choices on the validation window (2026-03-02 .. 05-31)\n"]
    z = v["zones"]
    best = max(z["trials"], key=lambda t: (round(t["f1"], 12), t["lookback_days"], t["min_share"]))
    f1s = [t["f1"] for t in z["trials"]]
    out.append(f"Zones: {len(z['trials'])} settings of (lookback, share) scored by the F1 of \"member on day d\" against \"alerts on at least 5 of the next 7 days\" "
               f"over {z['n_unit_days']} hromada-days. All settings land between {min(f1s):.3f} and {max(f1s):.3f}; the best is a {best['lookback_days']}-day lookback "
               f"with a share of {best['min_share']} (precision {best['precision']:.3f}, recall {best['recall']:.3f}).\n")
    out.append("Score for the other choices: mean of the phase 1 ratio model MAE / best naive MAE (both targets) and the phase 2 ratio model log loss / climatology log loss (1, 3, 6 h), regions only. Lower is better.\n")
    out.append("| Decision | Option | Phase 1 MAE ratio (alerts / hours) | Phase 2 log-loss ratio (1 / 3 / 6 h) | Score | Chosen |")
    out.append("|---|---|---|---|---:|---|")
    for step, title in (("history", "History start"), ("text", "Text channels"), ("edited", "Edited posts (not a choice)")):
        if step not in v:
            continue
        sel = v[step].get("selected")
        for t in v[step]["trials"]:
            p1 = t["phase1_rel_mae"]
            p2 = t["phase2_rel_logloss"]
            out.append(f"| {title} | {t['option']} | {p1['count']:.3f} / {p1['hours']:.3f} | {p2['1']:.3f} / {p2['3']:.3f} / {p2['6']:.3f} | {t['score']:.4f} | {'yes' if t['option'] == sel else ''} |")
    out.append("")
    out.append("#### Parser accuracy on the reviewed gold posts\n")
    dev = parsers.get("dev_v2")
    if dev:
        n = dev["channels"]["all"]["v1"]["n_posts"]
        out.append(f"The {n} gold posts Yehor Hunia reviewed (all {dev['channels']['kpszsu']['v1']['n_posts']} of the Air Force sample and the first "
                   f"{dev['channels']['war_monitor']['v1']['n_posts']} of @war_monitor), labelled under the rules v2. Parser v2 was written on these posts, "
                   "so its numbers here are a development figure, not a held-out one; the other 550 gold posts were not reviewed and no held-out "
                   "accuracy was measured. Oblasts: post x oblast pairs. Types: post x type pairs. Summary: the post is marked as a summary.\n")
        out.append("| Posts | Parser | Oblasts P / R | Types P / R | Summaries P / R | Post fully right |")
        out.append("|---|---|---|---|---|---:|")
        names = {**CHANNEL_NAMES, "all": "all reviewed"}
        f = lambda x: "–" if x is None else f"{x:.2f}"  # noqa: E731
        for c, v in dev["channels"].items():
            for ver in ("v1", "v2"):
                m = v[ver]
                o, t, s_ = m["oblasts"], m["types_micro"], m["summary"]
                out.append(f"| {names[c]} ({m['n_posts']}) | {ver} | {f(o['precision'])} / {f(o['recall'])} | {f(t['precision'])} / {f(t['recall'])} | "
                           f"{f(s_['precision'])} / {f(s_['recall'])} | {m['exact_post_match']:.0%} |")
        out.append("")
    v1 = config.REPORTS_DIR / "parser_v1"
    if (v1 / "metrics_phase1.json").exists():
        from .ablation import headline

        old = headline(_load("parser_v1/metrics_phase1.json"), _load("parser_v1/metrics_phase2.json"))
        new = headline(_load("metrics_phase1.json"), _load("metrics_phase2.json"))
        vold = _load("parser_v1/validation.json").get("text", {})
        out.append("#### Parser v1 against parser v2 on the test windows\n")
        out.append(f"The whole chain rerun with parser v2: text channels chosen again on the validation window "
                   f"(v1: {', '.join(CHANNEL_NAMES[c] for c in vold.get('selected_channels', []))}; "
                   f"v2: {', '.join(CHANNEL_NAMES[c] for c in v.get('text', {}).get('selected_channels', []))}), hyper-parameters tuned again, then the backtest. "
                   "Phase 1: skill vs the best naive forecast (alerts / hours). Phase 2: BSS vs climatology (1 h / 3 h / 6 h).\n")
        out.append("| Series / window | Parser v1: P1 | Parser v2: P1 | Parser v1: P2 | Parser v2: P2 |")
        out.append("|---|---|---|---|---|")
        for k in sorted(new):
            c1 = [f"{x['phase1_skill_vs_best_naive']['count']:+.1%} / {x['phase1_skill_vs_best_naive']['hours']:+.1%}" for x in (old[k], new[k])]
            c2 = [f"{x['phase2_bss_vs_climatology']['1']:+.1%} / {x['phase2_bss_vs_climatology']['3']:+.1%} / {x['phase2_bss_vs_climatology']['6']:+.1%}" for x in (old[k], new[k])]
            out.append(f"| {k.replace('/', ', ').replace('pre_break', 'before the break').replace('post_break', 'after the break')} | {c1[0]} | {c1[1]} | {c2[0]} | {c2[1]} |")
        out.append("")
    out.append("#### Edited posts and text features on the test windows\n")
    out.append("The same backtest with edited posts dropped, and with no text features at all; everything else fixed. Phase 1: skill vs the best naive forecast (hours). Phase 2: BSS vs climatology at 1 h.\n")
    out.append("| Series / window | Final model: P1 hours / P2 1 h | Without edited posts | Without text features |")
    out.append("|---|---|---|---|")
    for k in sorted(ablation["with_edited"]):
        cells = []
        for src in (ablation["with_edited"], ablation["without_edited"], ablation["without_text"]):
            x = src[k]
            cells.append(f"{x['phase1_skill_vs_best_naive']['hours']:+.1%} / {x['phase2_bss_vs_climatology']['1']:+.1%}")
        out.append(f"| {k.replace('/', ', ').replace('pre_break', 'before the break').replace('post_break', 'after the break')} | " + " | ".join(cells) + " |")
    out.append("")
    out.append(f"Posts dropped: " + ", ".join(f"{CHANNEL_NAMES[c]} {n}" for c, n in ablation["dropped_posts"].items()) + ".\n")
    out.append("#### Cross-check of the series with @UkraineAlarmSignal\n")
    out.append(f"Share of alert-start posts with a start of the same region's series within {cross['tolerance_min']} min, and the reverse per alert level.\n")
    out.append("| Window | Alert posts | Posts matched by the series | Series starts matched by a post, per level |")
    out.append("|---|---:|---:|---|")
    for name, w in cross["windows"].items():
        lv = "; ".join(f"{k}: {_pct(x['series_to_posts'])} of {x['series_starts']}" for k, x in w["levels"].items())
        out.append(f"| {name.replace('_', ' ')} ({w['first_day']} .. {w['last_day']}) | {w['alert_posts']} | {_pct(w['posts_to_series'])} | {lv} |")
    return "\n".join(out)


def _replace(text: str, a: str, b: str, block: str) -> str:
    pat = re.compile(re.escape(a) + r".*?" + re.escape(b), re.S)
    if not pat.search(text):
        raise RuntimeError(f"README has no {a} markers")
    return pat.sub(lambda _m: a + "\n" + block + "\n" + b, text)


def update_readme(block: str, choices: str | None = None) -> None:
    text = _replace(README.read_text(), START, END, block)
    if choices is not None:
        text = _replace(text, CSTART, CEND, choices)
    README.write_text(text)


def run() -> None:
    from . import datasets, stitch

    m1, m2 = _load("metrics_phase1.json"), _load("metrics_phase2.json")
    D = datasets.build(stitch.load_all(), config.DATA_END_UTC)
    fig_daily_hours(D.daily[D.daily["kind"] == "region"])
    fig_reliability(m2)
    fig_zones(D.daily)
    update_readme(results_markdown(m1, m2), choices_markdown(_load("validation.json"), _load("parsers.json"), _load("edited_ablation.json"), _load("crosscheck.json")))
    print("figures and README results updated")
