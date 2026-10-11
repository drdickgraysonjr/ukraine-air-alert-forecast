"""Forecast for a given moment, using only what was known at that moment."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import backtest, config, datasets, features, models
from . import timeline as T
from .regions import REGIONS

STATE_NAMES = {T.NONE: "none", T.PART: "part of region", T.WHOLE: "whole region"}


def run(alerts: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    t0 = as_of.tz_convert("UTC").floor("h")
    if t0 > config.DATA_END_UTC:
        raise ValueError(f"as_of {t0} is after the data end {config.DATA_END_UTC}")
    known = T.as_of(alerts, t0)
    D = datasets.build(known, t0)
    g = D.grid
    col = len(g.hours) - 1
    assert g.hours[col] == T.to_sec(t0)
    p1_params, p1_days, p2_params, p2_days = backtest.selected_params()

    # Phase 1: the forecast for today was issued at today's Kyiv midnight.
    day = t0.tz_convert(config.KYIV_TZ).normalize()
    m0 = int(T.to_sec(day))
    d = D.daily[D.daily["valid"]]
    train1 = d[d["known_at"] <= m0]
    if p1_days:
        train1 = train1[train1["m0"] >= m0 - p1_days * 86400]
    mcol = int(np.searchsorted(g.hours, m0))
    row1 = features.to_frame(g, D.feats, [mcol])
    m1 = int(T.to_sec((day.tz_localize(None) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)))
    row1["day_hours"] = (m1 - m0) / 3600
    p1 = {}
    for tgt in ("count", "hours"):
        m = models.fit_phase1(train1, D.feature_cols, tgt, p1_params)
        p1[tgt] = models.predict_phase1(m, row1, D.feature_cols, tgt)

    # Observed so far today (known at t0).
    so_far = {}
    now = np.array([g.hours[col]])
    for r in g.regions:
        tl = g.timelines[r]
        n_started = T.count_in(tl.ep_s, np.array([m0]), now + 1)[0]  # starts in [midnight, t0]
        hours = (T.covered_before(tl.s, tl.e, now) - T.covered_before(tl.s, tl.e, np.array([m0])))[0] / 3600
        so_far[r] = (int(n_started), float(hours))

    # Phase 2: probability that an alert starts within h hours, unless one is on.
    row2 = features.to_frame(g, D.feats, [col])
    h = D.hourly
    probs = {}
    for hz in config.HORIZONS:
        y = f"y{hz}"
        train2 = h[(h["t"] + hz * 3600 <= g.hours[col]) & h[y].notna()]
        if p2_days:
            train2 = train2[train2["t"] >= g.hours[col] - p2_days * 86400]
        m = models.fit_phase2(train2, D.feature_cols, y, p2_params)
        row2[f"p_model_{hz}"] = models.predict_phase2(m, row2, D.feature_cols)
    models.make_monotone(row2, "model", config.HORIZONS)

    regions = []
    for i, r in enumerate(g.regions):
        state = int(g.state[i, col])
        entry = {
            "region": r,
            "name": REGIONS[r][2],
            "state_now": STATE_NAMES[state],
            "today": {
                "forecast_count": round(float(p1["count"][i]), 2),
                "forecast_hours": round(float(p1["hours"][i]), 2),
                "observed_count_so_far": so_far[r][0],
                "observed_hours_so_far": round(so_far[r][1], 2),
            },
        }
        if g.active[i, col]:
            entry["next_hours"] = "alert in progress"
            entry["alert_elapsed_h"] = round(float(D.feats["elapsed_active_h"][i, col]), 2)
        else:
            entry["next_hours"] = {f"{hz}h": round(float(row2[f"p_model_{hz}"].iloc[i]), 3) for hz in config.HORIZONS}
        regions.append(entry)
    return {
        "as_of_utc": str(t0),
        "as_of_kyiv": str(t0.tz_convert(config.KYIV_TZ)),
        "today_kyiv": str(day.date()),
        "phase1_issued_at_kyiv": str(day),
        "data_end_utc": str(config.DATA_END_UTC),
        "train": {"phase1_rows": len(train1), "phase1_known_until": int(train1["known_at"].max()), "phase2_known_until": int(g.hours[col])},
        "regions": regions,
    }


def render(res: dict) -> str:
    lines = [f"Forecast as of {res['as_of_kyiv']} (Kyiv); data until {res['data_end_utc']} UTC", ""]
    lines.append(f"Today ({res['today_kyiv']}): forecast issued at 00:00 Kyiv, observed so far in brackets.")
    lines.append(f"{'region':16} {'now':15} {'alerts today':>14} {'hours today':>13}   next 1h..6h: P(alert starts)")
    for e in res["regions"]:
        t = e["today"]
        nxt = e["next_hours"] if isinstance(e["next_hours"], str) else "  ".join(f"{v:.2f}" for v in e["next_hours"].values())
        alerts = f"{t['forecast_count']:.1f} ({t['observed_count_so_far']})"
        hours = f"{t['forecast_hours']:.1f} ({t['observed_hours_so_far']:.1f})"
        lines.append(f"{e['name']:16} {e['state_now']:15} {alerts:>14} {hours:>13}   {nxt}")
    return "\n".join(lines)
