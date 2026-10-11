"""Interval algebra on alert timelines.

Times are int64 seconds since the Unix epoch (UTC). An alert that has no
finish time yet (still active when the data was captured) ends at OPEN_END,
so "is it active at t" is answered correctly for any t, and every quantity is
computed only from the part of an interval that lies before the query time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .regions import raion_count
from .translit import raion_key

OPEN_END = np.int64(2**62)

# Region state codes.
NONE, PART, WHOLE = 0, 1, 2


def to_sec(ts) -> np.ndarray | np.int64:
    """UTC timestamps (scalar, Series, Index) -> int64 seconds; NaT -> OPEN_END."""
    # Resolution-independent: pandas may store datetimes in s, ms, us or ns.
    if isinstance(ts, pd.Timestamp):
        return np.int64(ts.value // 10**9)  # .value is always nanoseconds
    s = pd.Series(ts)
    out = np.full(len(s), OPEN_END, dtype=np.int64)
    ok = s.notna().to_numpy()
    out[ok] = pd.DatetimeIndex(s[ok]).as_unit("s").asi8.astype(np.int64)
    return out


def union(starts: np.ndarray, ends: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Merge overlapping or touching intervals into a sorted disjoint set."""
    return merge_gaps(starts, ends, 0)


def merge_gaps(starts: np.ndarray, ends: np.ndarray, gap: int) -> tuple[np.ndarray, np.ndarray]:
    """Merge intervals whose gap is <= `gap` seconds. The merge is causal: whether a
    start joins the previous interval depends only on times before that start."""
    if len(starts) == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    order = np.argsort(starts, kind="stable")
    s, e = starts[order], ends[order]
    out_s, out_e = [s[0]], [e[0]]
    for a, b in zip(s[1:], e[1:]):
        if a - out_e[-1] <= gap:
            if b > out_e[-1]:
                out_e[-1] = b
        else:
            out_s.append(a)
            out_e.append(b)
    return np.asarray(out_s, np.int64), np.asarray(out_e, np.int64)


def covered_before(s: np.ndarray, e: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Total seconds covered by disjoint sorted intervals within (-inf, x)."""
    x = np.asarray(x, np.int64)
    full = np.concatenate([[0], np.cumsum(np.minimum(e, OPEN_END) - s)])
    # Intervals that started before x.
    k = np.searchsorted(s, x, side="left")
    done = full[k]  # all started intervals counted fully ...
    last = k - 1
    has = last >= 0
    # ... minus the part of the last started interval that lies after x.
    over = np.zeros_like(x)
    over[has] = np.maximum(0, np.minimum(e[last[has]], OPEN_END) - x[has])
    return done - over


def active_at(s: np.ndarray, e: np.ndarray, x: np.ndarray) -> np.ndarray:
    """True where some interval satisfies start <= x < end."""
    x = np.asarray(x, np.int64)
    k = np.searchsorted(s, x, side="right") - 1
    out = np.zeros(len(x), bool)
    has = k >= 0
    out[has] = e[k[has]] > x[has]
    return out


def count_in(points: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Number of sorted points p with lo <= p < hi."""
    return np.searchsorted(points, hi, side="left") - np.searchsorted(points, lo, side="left")


def last_before(points: np.ndarray, x: np.ndarray, inclusive: bool = True) -> np.ndarray:
    """Most recent sorted point <= x (or < x); NaN-like -1 when none."""
    k = np.searchsorted(points, x, side="right" if inclusive else "left") - 1
    out = np.full(len(x), -1, np.int64)
    has = k >= 0
    out[has] = points[k[has]]
    return out


def next_after(points: np.ndarray, x: np.ndarray) -> np.ndarray:
    """First sorted point > x; OPEN_END when none."""
    k = np.searchsorted(points, x, side="right")
    out = np.full(len(x), OPEN_END, np.int64)
    has = k < len(points)
    out[has] = points[k[has]]
    return out


@dataclass
class RegionTimeline:
    """All alert intervals of one region, as known from the given alert rows."""

    region: str
    s: np.ndarray  # union of all region-level alerts
    e: np.ndarray
    ep_s: np.ndarray  # union with short gaps merged: counted "alerts"
    ep_e: np.ndarray
    oblast_s: np.ndarray  # oblast-level alerts only
    oblast_e: np.ndarray
    raions: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)

    def state_at(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.int64)
        any_active = active_at(self.s, self.e, x)
        whole = active_at(self.oblast_s, self.oblast_e, x)
        n = raion_count(self.region)
        if n:
            k = np.zeros(len(x), np.int64)
            for rs, re_ in self.raions.values():
                k += active_at(rs, re_, x)
            whole |= k >= n
        return np.where(whole, WHOLE, np.where(any_active, PART, NONE)).astype(np.int8)


def build_timeline(rows: pd.DataFrame, region: str, merge_gap_sec: int) -> RegionTimeline:
    """rows: normalised alerts of one region (oblast and raion level)."""
    st = to_sec(rows["started_at"]).astype(np.int64)
    fi = to_sec(rows["finished_at"]).astype(np.int64)
    s, e = union(st, fi)
    ep_s, ep_e = merge_gaps(st, fi, merge_gap_sec)
    ob = (rows["unit_level"] == "oblast").to_numpy()
    oblast_s, oblast_e = union(st[ob], fi[ob])
    raions = {}
    rr = (rows["unit_level"] == "raion").to_numpy()
    if rr.any():
        keys = rows.loc[rr, "unit"].map(raion_key).to_numpy()
        for key in np.unique(keys):
            m = keys == key
            raions[key] = union(st[rr][m], fi[rr][m])
    return RegionTimeline(region, s, e, ep_s, ep_e, oblast_s, oblast_e, raions)


def build_timelines(alerts: pd.DataFrame, regions, merge_gap_sec: int) -> dict[str, RegionTimeline]:
    out = {}
    groups = dict(tuple(alerts.groupby("region", sort=False)))
    for r in regions:
        rows = groups.get(r, alerts.iloc[0:0])
        out[r] = build_timeline(rows, r, merge_gap_sec)
    return out


def as_of(alerts: pd.DataFrame, t: pd.Timestamp) -> pd.DataFrame:
    """The alert table as it could be known at moment t: alerts that have started
    by t, with the finish time hidden when the alert was still active at t."""
    known = alerts[alerts["started_at"] <= t].copy()
    known.loc[known["finished_at"] > t, "finished_at"] = pd.NaT
    return known
