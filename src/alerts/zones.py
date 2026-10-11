"""Zones of regular alerts: hromadas where alerts are declared almost every day.

Near the front, alerts are declared for single hromadas (Nikopol, Marhanets,
Pokrovsk, the cities of Kharkiv and Zaporizhzhia) far more often than for the
rest of the oblast. Each oblast gets a separate forecast for its zone and for
the rest; the rest is the oblast- and raion-level series used everywhere else.

A hromada belongs to the zone on Kyiv day d if it had an alert on at least
`min_share` of the valid days among the `lookback` days before d. Membership is
fixed at d's midnight from days that are already over, so it uses nothing
published after the forecast moment. The zone's alert timeline is the union of
the member hromadas' own alerts, the alerts of the raions they lie in and the
oblast-level alerts: an alert for the raion or the whole oblast covers the zone
too.

alerts.in.ua lists a city and its hromada as two locations with the same alert
(m. Kryvyi Rih and Kryvorizka hromada); city rows that repeat a hromada alert
are dropped so a zone is not counted twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config
from . import timeline as T
from .translit import raion_key, transliterate

HROMADA_LEVELS = ("hromada", "city")
# A city row repeats a hromada alert when both start within this many seconds.
CITY_DUP_SEC = 120
# Realised regularity used to choose the parameters: alerts on at least this
# many of the next 7 days.
REGULAR_DAYS_OF_7 = 5


def hromada_key(name: str) -> str:
    """'m. Nikopol ta Nikopolska terytorialna hromada' and
    'Нікопольська територіальна громада' -> 'nikopolska'."""
    latin = transliterate(name) if re.search("[Ѐ-ӿ]", name) else name
    m = re.search(r"(\S+)\s+terytorialna\s+hromada", latin, re.I)
    return (m.group(1) if m else latin).strip().lower()


def unit_alerts(alerts_all: pd.DataFrame) -> pd.DataFrame:
    """Hromada-level alerts with a unit key; city rows repeating a hromada alert dropped.
    Columns: region, unit_key, raion_key, started_at, finished_at."""
    h = alerts_all[alerts_all["unit_level"] == "hromada"].copy()
    c = alerts_all[alerts_all["unit_level"] == "city"].copy()
    if len(c):
        hs = h[["region", "started_at"]].sort_values("started_at").rename(columns={"started_at": "h_start"})
        hs["h_key"] = hs["h_start"]
        m = pd.merge_asof(
            c.sort_values("started_at"), hs, left_on="started_at", right_on="h_start", by="region",
            tolerance=pd.Timedelta(seconds=CITY_DUP_SEC), direction="nearest",
        )
        c = m[m["h_key"].isna()][alerts_all.columns]
    h["raion_key"] = h["raion"].map(raion_key)
    h["unit_key"] = h["raion_key"] + "/" + h["unit"].map(hromada_key)
    if len(c):
        c["raion_key"] = ""
        c["unit_key"] = "city/" + c["unit"].map(lambda s: transliterate(s).lower())
    out = pd.concat([h, c], ignore_index=True)
    return out[["region", "unit_key", "raion_key", "started_at", "finished_at"]].sort_values(
        ["region", "unit_key", "started_at"], kind="stable"
    ).reset_index(drop=True)


@dataclass
class Activity:
    keys: list[tuple[str, str, str]]  # (region, unit_key, raion_key)
    days: pd.DataFrame  # day, m0, m1 (Kyiv days)
    act: np.ndarray  # (U, D) bool: the unit had an alert at some moment of the day
    day_ok: np.ndarray  # (D,) day is not excluded
    intervals: dict  # unit index -> (s, e) union of its alerts, seconds


def activity(units: pd.DataFrame, first_day: str, last_day: str) -> Activity:
    from .features import kyiv_midnights

    d = kyiv_midnights(first_day, last_day)
    m0, m1 = d["m0"].to_numpy(), d["m1"].to_numpy()
    keys, rows, ivs = [], [], {}
    for (r, u, rk), g in units.groupby(["region", "unit_key", "raion_key"], sort=True):
        s, e = T.union(T.to_sec(g["started_at"]).astype(np.int64), T.to_sec(g["finished_at"]).astype(np.int64))
        ivs[len(keys)] = (s, e)
        keys.append((r, u, rk))
        rows.append(T.covered_before(s, e, m1) - T.covered_before(s, e, m0) > 0)
    act = np.array(rows, bool) if rows else np.zeros((0, len(d)), bool)
    ok = ~np.isin(d["day"].to_numpy(), np.array(config.EXCLUDED_DAYS))
    return Activity(keys, d, act, ok, ivs)


def membership(a: Activity, lookback: int, min_share: float) -> np.ndarray:
    """(U, D) bool: unit is in its oblast's zone on day i, from days i-lookback .. i-1."""
    U, D = a.act.shape
    x = (a.act & a.day_ok[None, :]).astype(np.int64)
    v = a.day_ok.astype(np.int64)
    cx = np.concatenate([np.zeros((U, 1), np.int64), np.cumsum(x, axis=1)], axis=1)
    cv = np.concatenate([[0], np.cumsum(v)])
    i = np.arange(D)
    lo = np.maximum(i - lookback, 0)
    hits = cx[:, i] - cx[:, lo]
    n = cv[i] - cv[lo]
    # Require a full lookback and at least half of it valid.
    enough = (i >= lookback) & (n * 2 >= lookback)
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(n > 0, hits / np.maximum(n, 1), 0.0)
    return (share >= min_share) & enough[None, :]


def realised_regular(a: Activity) -> np.ndarray:
    """(U, D) bool or NaN-free mask pair: alerts on >= REGULAR_DAYS_OF_7 of days i .. i+6."""
    U, D = a.act.shape
    x = (a.act & a.day_ok[None, :]).astype(np.int64)
    cx = np.concatenate([np.zeros((U, 1), np.int64), np.cumsum(x, axis=1)], axis=1)
    i = np.arange(D)
    hi = np.minimum(i + 7, D)
    return (cx[:, hi] - cx[:, i]) >= REGULAR_DAYS_OF_7


def zone_sets(a: Activity, member: np.ndarray, regions) -> dict[str, list[frozenset]]:
    """Per region: the member set (frozenset of unit indices) on every day."""
    out = {}
    reg = np.array([k[0] for k in a.keys])
    for r in regions:
        idx = np.nonzero(reg == r)[0]
        sets = [frozenset(idx[member[idx, i]].tolist()) for i in range(member.shape[1])]
        if any(sets):
            out[r] = sets
    return out


def zone_intervals(a: Activity, members: frozenset, tl: T.RegionTimeline) -> tuple[np.ndarray, np.ndarray]:
    """Union of the members' own alerts, their raions' alerts and the oblast alerts."""
    parts_s, parts_e = [tl.oblast_s], [tl.oblast_e]
    for u in members:
        s, e = a.intervals[u]
        parts_s.append(s)
        parts_e.append(e)
        rk = a.keys[u][2]
        if rk in tl.raions:
            rs, re_ = tl.raions[rk]
            parts_s.append(rs)
            parts_e.append(re_)
    return T.union(np.concatenate(parts_s), np.concatenate(parts_e))


def select_params(a: Activity, first_day: str, last_day: str, lookbacks, shares) -> dict:
    """Choose (lookback, min_share) on the days of [first_day, last_day] whose next
    7 days also lie in the window: F1 of 'member on day d' against 'alerts on at
    least 5 of the next 7 days', over all hromadas with any alert."""
    day = a.days["day"].to_numpy()
    last_ok = str((pd.Timestamp(last_day) - pd.Timedelta(days=6)).date())
    cols = np.nonzero((day >= first_day) & (day <= last_ok) & a.day_ok)[0]
    truth = realised_regular(a)[:, cols]
    trials = []
    for lb in lookbacks:
        for sh in shares:
            pred = membership(a, lb, sh)[:, cols]
            tp = int((pred & truth).sum())
            fp = int((pred & ~truth).sum())
            fn = int((~pred & truth).sum())
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            trials.append({"lookback_days": lb, "min_share": sh, "tp": tp, "fp": fp, "fn": fn, "precision": prec, "recall": rec, "f1": f1})
    # Highest F1; ties go to the longer lookback, then the higher share (steadier zones).
    best = max(trials, key=lambda t: (round(t["f1"], 12), t["lookback_days"], t["min_share"]))
    return {
        "criterion": f"F1 of membership on day d vs alerts on >= {REGULAR_DAYS_OF_7} of days d..d+6",
        "days": [str(day[cols[0]]), str(day[cols[-1]])],
        "n_unit_days": int(truth.size),
        "trials": trials,
        "selected": {"lookback_days": best["lookback_days"], "min_share": best["min_share"]},
    }
