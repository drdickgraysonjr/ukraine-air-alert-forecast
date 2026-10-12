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
    return _run(alerts, as_of, posts, config.DATA_END_UTC)


def _run(alerts: pd.DataFrame, as_of: pd.Timestamp, posts, data_end: pd.Timestamp,
         kinds=("region", "zone"), keep_probs: bool = False) -> dict:
    """run() for an explicit data end. kinds: series kinds to forecast;
    keep_probs: also return the phase 2 probabilities of series with an alert in
    progress (key "_p"), for the live path."""
    t0 = as_of.tz_convert("UTC").floor("h")
    if t0 > data_end:
        raise ValueError(f"as_of {t0} is after the data end {data_end}")
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
    for kind in kinds:
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
            if keep_probs:
                entry["_p"] = {f"{hz}h": round(float(row2[f"p_model_{hz}"].iloc[j]), 3) for hz in config.HORIZONS}
            out["regions" if kind == "region" else "zones"].append(entry)
    return {
        "as_of_utc": str(t0),
        "as_of_kyiv": str(t0.tz_convert(config.KYIV_TZ)),
        "today_kyiv": str(day.date()),
        "phase1_issued_at_kyiv": str(day),
        "data_end_utc": str(data_end),
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


# ------------------------------------------------------------------ live

LIVE_ZONES_WARNING = (
    "Zones of regular alerts are not forecast live: NEPTUN has no hromada-level alerts, so zone "
    "membership and zone alerts cannot be updated after the pinned data end."
)


def gap_days(data_end: pd.Timestamp, first_snapshot: pd.Timestamp, today: str) -> list[str]:
    """Kyiv days touched by the hole (data_end, first_snapshot), except `today`:
    they are handled like the series' other data holes (config.EXCLUDED_DAYS)."""
    a = data_end.tz_convert(config.KYIV_TZ).normalize().tz_localize(None)
    b = first_snapshot.tz_convert(config.KYIV_TZ).normalize().tz_localize(None)
    return [str(d.date()) for d in pd.date_range(a, b, freq="D") if str(d.date()) != today]


class _ExtraExcludedDays:
    """Temporarily add days to config.EXCLUDED_DAYS (read at call time by
    alerts.features and alerts.zones)."""

    def __init__(self, days):
        self.days = tuple(days)

    def __enter__(self):
        self.old = config.EXCLUDED_DAYS
        config.EXCLUDED_DAYS = tuple(sorted(set(self.old) | set(self.days)))

    def __exit__(self, *exc):
        config.EXCLUDED_DAYS = self.old
        return False


def run_live(as_of: pd.Timestamp | None = None, snapshot_lines=None, messages=None, fetches=None,
             now: pd.Timestamp | None = None) -> dict:
    """Forecast from the pinned data continued with recorded NEPTUN snapshots.

    Uses only snapshots and posts recorded by `as_of` (default: everything
    recorded). The data end is the latest such snapshot S: region states,
    "alert in progress" and today's observed values are as of S; features and
    the phase 2 windows start at the last full hour before S (features_at_utc),
    as everywhere in the model. Returns run()'s structure (zones empty, see
    LIVE_ZONES_WARNING) plus live details, warnings, attribution and disclaimer."""
    from . import neptun

    lines = neptun.load_snapshots() if snapshot_lines is None else list(snapshot_lines)
    lines.sort(key=lambda x: neptun._utc(x["fetched_at"]))
    if as_of is not None:
        as_of = neptun._utc(as_of)
        lines = [x for x in lines if neptun._utc(x["fetched_at"]) <= as_of]
    if not lines:
        raise ValueError("no NEPTUN snapshot recorded" + (f" by {as_of}" if as_of is not None else "") + ": run `alerts neptun-record`")
    if messages is None:
        messages, rec_fetches = neptun.load_messages()
        fetches = rec_fetches if fetches is None else fetches
    snap = neptun._utc(lines[-1]["fetched_at"])
    alerts, arep = neptun.live_alerts(lines)
    posts, prep = neptun.live_posts(messages, fetches, until=snap)
    t0 = snap.floor("h")
    today = str(t0.tz_convert(config.KYIV_TZ).date())
    first = pd.Timestamp(arep["first_snapshot_utc"])
    hole = gap_days(config.DATA_END_UTC, first, today)
    with _ExtraExcludedDays(hole):
        res = _run(alerts, snap, posts, data_end=snap, kinds=("region",), keep_probs=True)

    # State, "alert in progress" and today's observed values at the snapshot.
    known = T.as_of(alerts, snap)
    tls = T.build_timelines(known[known["unit_level"].isin(config.REGION_UNIT_LEVELS)], list(REGIONS), config.MERGE_GAP_SEC)
    x = np.array([T.to_sec(snap)], np.int64)
    m0 = np.array([T.to_sec(pd.Timestamp(res["phase1_issued_at_kyiv"]))], np.int64)
    for e in res["regions"]:
        tl = tls[e["region"]]
        e["state_now"] = STATE_NAMES[int(tl.state_at(x)[0])]
        e["today"]["observed_count_so_far"] = int(T.count_in(tl.s, m0, x + 1)[0])
        e["today"]["observed_hours_so_far"] = round(float((T.covered_before(tl.s, tl.e, x) - T.covered_before(tl.s, tl.e, m0))[0] / 3600), 2)
        p = e.pop("_p")
        e.pop("alert_elapsed_h", None)
        if T.active_at(tl.s, tl.e, x)[0]:
            k = int(np.searchsorted(tl.s, x[0], side="right")) - 1
            e["next_hours"] = "alert in progress"
            e["alert_elapsed_h"] = round(float((x[0] - tl.s[k]) / 3600), 2)
        else:
            e["next_hours"] = p

    warnings = []
    gap = arep["gap_h"]
    warnings.append(
        f"Gap of {gap:.1f} h between the pinned data end ({arep['data_end_utc']}) and the first NEPTUN snapshot "
        f"({arep['first_snapshot_utc']}): alerts that started and ended inside it are missing. Of "
        f"{arep['open_at_data_end']} alerts open at the data end, {len(arep['continued'])} continue in NEPTUN with the same start; "
        f"{len(arep['closed_in_gap_region_level'])} oblast/raion and {len(arep['closed_in_gap_hromada_city'])} hromada/city alerts "
        f"are absent from the first snapshot and closed "
        + ("at it, or at the start of a newer alert of the same location " if gap * 3600 <= neptun.GAP_CLOSE_AT_SNAPSHOT_SEC
           else "at the data end, since the gap is too long to guess (an undercount) ")
        + "(their true finish lies somewhere in the gap). "
        + (f"Kyiv days {', '.join(hole)} are treated as excluded days (like the series' other data holes)." if hole else "")
    )
    if first > pd.Timestamp(res["phase1_issued_at_kyiv"]):
        warnings.append(f"The gap reaches into today ({today}): today's observed values so far and the features of its first "
                        f"{(first - pd.Timestamp(res['phase1_issued_at_kyiv'])).total_seconds() / 3600:.1f} h may understate alerts.")
    if arep["recording_gaps"]:
        warnings.append(f"{len(arep['recording_gaps'])} pauses of the recorder longer than {neptun.RECORDING_GAP_SEC // 60} min "
                        f"(longest {max(g[2] for g in arep['recording_gaps']):.0f} min): finishes inside them are late and "
                        f"alerts that started and ended inside them are missing.")
    if arep["conflicts"]:
        warnings.append(f"{len(arep['conflicts'])} NEPTUN alerts start before the data end but continue no alert of the pinned "
                        f"table (kept as NEPTUN reports them): " + "; ".join(arep["conflicts"][:5]))
    if prep["missing_from_feed"]:
        warnings.append("Text channels missing from the NEPTUN feed: " + ", ".join(
            f"{c} (export ends {v['export_last_post_utc']})" for c, v in prep["missing_from_feed"].items())
            + "; their posts after the export are absent, so their text features read as zero.")
    first_msg = min((neptun._utc(m["fetched_at"]) for m in messages if m.get("fetched_at")), default=None)
    for c, v in prep["in_feed"].items():
        if first_msg is not None and v["export_last_post_utc"] and neptun._utc(v["export_last_post_utc"]) < first_msg - pd.Timedelta(minutes=15):
            warnings.append(f"Posts of {c} between its export end ({v['export_last_post_utc']}) and the first recorded NEPTUN "
                            f"messages (~15 min before {first_msg}) are missing.")
    if prep["possible_message_gaps"]:
        warnings.append(f"{prep['possible_message_gaps']} message fetches had no overlap with the previous one: posts may be missing.")
    warnings.append(LIVE_ZONES_WARNING)
    now = neptun._utc(now) if now is not None else (as_of if as_of is not None else pd.Timestamp.now(tz="UTC"))
    age_min = (now - snap).total_seconds() / 60
    if age_min > 10:
        warnings.append(f"The latest NEPTUN snapshot is {age_min:.0f} min old: is `alerts neptun-record` running?")

    res.update({
        "mode": "live",
        "snapshot_utc": str(snap),
        "features_at_utc": str(t0),
        "state_at_utc": str(snap),
        "zones": [],
        "live": {"alerts": arep, "posts": prep, "gap_excluded_days": hole},
        "warnings": warnings,
        "attribution": config.NEPTUN_ATTRIBUTION,
        "disclaimer": config.NEPTUN_DISCLAIMER,
    })
    return res


def render_live(res: dict) -> str:
    head = [res["attribution"], res["disclaimer"], "",
            f"Live: state at {pd.Timestamp(res['state_at_utc']).tz_convert(config.KYIV_TZ)} (Kyiv, latest NEPTUN snapshot); "
            f"P(alert starts) for windows from {pd.Timestamp(res['features_at_utc']).tz_convert(config.KYIV_TZ)}."]
    body = render(res).split("\n")
    tail = ["", "Warnings:"] + [f"- {w}" for w in res["warnings"]] + ["", res["attribution"]]
    return "\n".join(head + body + tail)
