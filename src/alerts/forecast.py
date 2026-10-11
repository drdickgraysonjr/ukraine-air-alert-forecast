"""Forecast for a given moment, using only what was known at that moment:
alerts as of that moment and Telegram posts published by then."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import backtest, config, datasets, features, models
from . import timeline as T
from .regions import REGIONS

STATE_NAMES = {T.NONE: "none", T.PART: "part of region", T.WHOLE: "whole region"}
ZONE_STATE = {T.NONE: "none", T.PART: "zone under alert", T.WHOLE: "zone under alert"}


def run(alerts: pd.DataFrame, as_of: pd.Timestamp, posts=None) -> dict:
    t0 = as_of.tz_convert("UTC").floor("h")
    if t0 > config.DATA_END_UTC:
        raise ValueError(f"as_of {t0} is after the data end {config.DATA_END_UTC}")
    known = T.as_of(alerts, t0)
    D = datasets.build(known, t0, posts=posts.as_of(int(T.to_sec(t0))) if posts is not None else None)
    g = D.grid
    col = len(g.hours) - 1
    assert g.hours[col] == T.to_sec(t0)
    p1_params, p1_days, p2_params, p2_days = backtest.selected_params()

    # Phase 1: the forecast for today was issued at today's Kyiv midnight.
    day = t0.tz_convert(config.KYIV_TZ).normalize()
    m0 = int(T.to_sec(day))
    m1 = int(T.to_sec((day.tz_localize(None) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)))
    mcol = int(np.searchsorted(g.hours, m0))
    out = {"regions": [], "zones": []}
    known_until = {}
    for kind in ("region", "zone"):
        rows = np.nonzero((g.is_zone == (kind == "zone")) & g.exists[:, col])[0]
        if len(rows) == 0:
            continue
        d = D.daily[D.daily["valid"] & (D.daily["kind"] == kind)]
        train1 = d[d["known_at"] <= m0]
        if p1_days:
            train1 = train1[train1["m0"] >= m0 - p1_days * 86400]
        row1 = features.to_frame(g, D.feats, [mcol], rows)
        row1["day_hours"] = (m1 - m0) / 3600
        p1 = {}
        for tgt in ("count", "hours"):
            m = models.fit_phase1(train1, D.feature_cols, tgt, p1_params)
            p1[tgt] = models.predict_phase1(m, row1, D.feature_cols, tgt)
        known_until[kind] = int(train1["known_at"].max())

        # Phase 2: probability that an alert starts within h hours, unless one is on.
        row2 = features.to_frame(g, D.feats, [col], rows)
        h = D.hourly[D.hourly["kind"] == kind]
        for hz in config.HORIZONS:
            y = f"y{hz}"
            train2 = h[(h["t"] + hz * 3600 <= g.hours[col]) & h[y].notna()]
            if p2_days:
                train2 = train2[train2["t"] >= g.hours[col] - p2_days * 86400]
            m = models.fit_phase2(train2, D.feature_cols, y, p2_params)
            row2[f"p_model_{hz}"] = models.predict_phase2(m, row2, D.feature_cols)
        models.make_monotone(row2, "model", config.HORIZONS)

        now = np.array([g.hours[col]])
        for j, k in enumerate(rows):
            sid = g.series[k]
            set_idx = None
            if g.is_zone[k]:
                set_idx = int(g.zones[sid].day_set[g.col_day[col]])
            s, e = g.series_intervals(k, set_idx)
            n_started = T.count_in(s, np.array([m0]), now + 1)[0]  # starts in [midnight, t0]
            hours = (T.covered_before(s, e, now) - T.covered_before(s, e, np.array([m0])))[0] / 3600
            region = g.regions[g.parent[k]]
            state = int(g.state[k, col])
            entry = {
                "region": region,
                "name": REGIONS[region][2] + (" zone" if g.is_zone[k] else ""),
                "state_now": (ZONE_STATE if g.is_zone[k] else STATE_NAMES)[state],
                "today": {
                    "forecast_count": round(float(p1["count"][j]), 2),
                    "forecast_hours": round(float(p1["hours"][j]), 2),
                    "observed_count_so_far": int(n_started),
                    "observed_hours_so_far": round(float(hours), 2),
                },
            }
            if g.is_zone[k]:
                entry["members"] = g.zones[sid].members[set_idx]
            if g.active[k, col]:
                entry["next_hours"] = "alert in progress"
                entry["alert_elapsed_h"] = round(float(D.feats["elapsed_active_h"][k, col]), 2)
            else:
                entry["next_hours"] = {f"{hz}h": round(float(row2[f"p_model_{hz}"].iloc[j]), 3) for hz in config.HORIZONS}
            out["regions" if kind == "region" else "zones"].append(entry)
    return {
        "as_of_utc": str(t0),
        "as_of_kyiv": str(t0.tz_convert(config.KYIV_TZ)),
        "today_kyiv": str(day.date()),
        "phase1_issued_at_kyiv": str(day),
        "data_end_utc": str(config.DATA_END_UTC),
        "text_channels": sorted(posts.frames) if posts is not None else [],
        "train": {"phase1_known_until": max(known_until.values()), "phase2_known_until": int(g.hours[col])},
        **out,
    }


def render(res: dict) -> str:
    lines = [f"Forecast as of {res['as_of_kyiv']} (Kyiv); data until {res['data_end_utc']} UTC", ""]
    lines.append(f"Today ({res['today_kyiv']}): forecast issued at 00:00 Kyiv, observed so far in brackets.")
    lines.append(f"{'series':22} {'now':17} {'alerts today':>14} {'hours today':>13}   next 1h..6h: P(alert starts)")
    for e in res["regions"] + res["zones"]:
        t = e["today"]
        nxt = e["next_hours"] if isinstance(e["next_hours"], str) else "  ".join(f"{v:.2f}" for v in e["next_hours"].values())
        alerts = f"{t['forecast_count']:.1f} ({t['observed_count_so_far']})"
        hours = f"{t['forecast_hours']:.1f} ({t['observed_hours_so_far']:.1f})"
        lines.append(f"{e['name']:22} {e['state_now']:17} {alerts:>14} {hours:>13}   {nxt}")
    if res["zones"]:
        lines.append("")
        lines.append("Zones of regular alerts today: " + "; ".join(f"{e['name']}: {', '.join(e['members'])}" for e in res["zones"]))
    return "\n".join(lines)
