"""Hourly feature grid and targets for both phases.

The grid has one row per series: the 24 regions (oblast- and raion-level alerts)
and, for oblasts that have one, the zone of regular alerts (alerts.zones). Grid
column i describes the moment H[i] (on the hour, UTC). Every feature at H[i] is
computed only from alert starts <= H[i], from the part of each alert that lies
before H[i], and from Telegram posts published before H[i];
scripts/verify_leakage.py checks this by recomputing features from the alert
table and the posts as they were known at H[i].

Hourly bin i is the hour that ENDS at H[i]: [H[i] - 1h, H[i]).

A zone's membership is fixed for a Kyiv day at its midnight. Its features at
H[i] describe the history of the hromadas that are members on the day of H[i],
so a zone that gains a hromada also "remembers" that hromada's past alerts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config
from . import timeline as T
from . import zones as Z
from .regions import NEIGHBORS, REGION_IDS

HOUR = 3600
DAY = 24 * HOUR
ROLL_COVER = (1, 3, 6, 12, 24, 72, 168, 672)
ROLL_COUNT = (24, 168, 672)
CLIM_DAYS = 28
CAP_H = 24 * 28.0
ZONE_SUFFIX = "~zone"
BASE_LAGS = 28  # days of target history kept for the naive forecasts


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Sum over the last w columns (inclusive) along axis 1."""
    c = np.cumsum(x, axis=1, dtype=np.float64)
    out = c.copy()
    out[:, w:] = c[:, w:] - c[:, :-w]
    return out


def kyiv_date(sec: np.ndarray) -> np.ndarray:
    return pd.to_datetime(sec, unit="s", utc=True).tz_convert(config.KYIV_TZ).strftime("%Y-%m-%d").to_numpy()


def excluded_mask(sec_bin_start: np.ndarray) -> np.ndarray:
    return np.isin(kyiv_date(sec_bin_start), np.array(config.EXCLUDED_DAYS))


def kyiv_midnights(first_day: str, last_day: str) -> pd.DataFrame:
    days = pd.date_range(first_day, last_day, freq="D")
    m0 = days.tz_localize(config.KYIV_TZ)
    m1 = (days + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)
    return pd.DataFrame(
        {
            "day": days.strftime("%Y-%m-%d"),
            "m0": pd.DatetimeIndex(m0).tz_convert("UTC").as_unit("s").asi8,
            "m1": pd.DatetimeIndex(m1).tz_convert("UTC").as_unit("s").asi8,
        }
    )


@dataclass
class ZoneSeries:
    """One oblast's zone: the member set on each grid day and its alert intervals."""

    region: str
    day_set: np.ndarray  # (D,) index into sets, -1 = empty zone that day
    sets: list[frozenset]
    intervals: list[tuple[np.ndarray, np.ndarray]]  # per set: union of zone alerts
    members: list[list[str]]  # per set: unit keys


@dataclass
class Grid:
    hours: np.ndarray  # (n,) int seconds, H[i]
    regions: tuple[str, ...]  # the 24 regions: rows 0..23
    series: tuple[str, ...]  # regions, then zones ("<region>~zone")
    parent: np.ndarray  # (S,) region row of each series
    is_zone: np.ndarray  # (S,) bool
    data_end: int
    valid: np.ndarray  # (n,) bin [H-1h, H) holds trustworthy data
    exists: np.ndarray  # (S, n) series is defined at H (a zone has members that day)
    active: np.ndarray  # (S, n) bool, active at H
    state: np.ndarray  # (S, n) int8
    own: dict  # own-history features, each (S, n)
    timelines: dict  # region -> RegionTimeline
    days: pd.DataFrame  # grid days (Kyiv): day, m0, m1
    col_day: np.ndarray  # (n,) index into days of the Kyiv day containing H
    zones: dict = field(default_factory=dict)  # series id -> ZoneSeries
    posts: object = None  # alerts.telegram.Posts or None
    n_ongoing: int = 0

    def series_intervals(self, k: int, set_idx: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        sid = self.series[k]
        if not self.is_zone[k]:
            tl = self.timelines[sid]
            return tl.s, tl.e
        return self.zones[sid].intervals[set_idx]


def _own_arrays(s: np.ndarray, e: np.ndarray, hours: np.ndarray) -> dict:
    ends = e[e < T.OPEN_END]
    return {
        "active": T.active_at(s, e, hours),
        "cover": T.covered_before(s, e, hours) - T.covered_before(s, e, hours - HOUR),
        "starts": T.count_in(s, hours - HOUR, hours).astype(np.float64),
        "last_start": T.last_before(s, hours),
        "last_end": T.last_before(ends, hours),
    }


def _since(h: np.ndarray, last: np.ndarray) -> np.ndarray:
    out = (h[None, :] - last) / HOUR
    return np.where(last < 0, np.nan, np.minimum(out, CAP_H))


def own_features(h: np.ndarray, valid: np.ndarray, state, active, cover, starts, last_start, last_end) -> dict:
    """Features of one series' own alert history, all arrays (k, n)."""
    k, n = active.shape
    v = valid.astype(np.float64)[None, :]
    f: dict[str, np.ndarray] = {}
    f["state_now"] = state.astype(np.float64)
    f["active_now"] = active.astype(np.float64)
    since_start = _since(h, last_start)
    f["elapsed_active_h"] = np.where(active, since_start, 0.0)
    f["since_start_h"] = since_start
    f["since_end_h"] = _since(h, last_end)
    vsum = {w: _rolling_sum(v, w)[0] for w in set(ROLL_COVER) | set(ROLL_COUNT)}
    with np.errstate(invalid="ignore", divide="ignore"):
        for w in ROLL_COVER:
            f[f"cover_{w}h"] = _rolling_sum(cover * v, w) / (vsum[w] * HOUR)
        for w in ROLL_COUNT:
            f[f"starts_{w}h"] = _rolling_sum(starts * v, w) / vsum[w] * 24
    for lag in (24, 168):
        a = np.full((k, n), np.nan)
        a[:, lag:] = active[:, :-lag]
        f[f"active_lag_{lag}h"] = a
    # Same-clock-time climatology over the previous 28 days: did an alert start
    # within the next j hours of that moment? Uses bins that end <= H only.
    nxt = np.zeros((k, n + 6))
    nxt[:, :n] = starts
    vv = np.zeros(n + 6)
    vv[:n] = valid
    for j in (1, 3, 6):
        win = sum(nxt[:, 1 + d : n + 1 + d] for d in range(j)) > 0
        wv = np.prod([vv[1 + d : n + 1 + d] for d in range(j)], axis=0) > 0
        acc = np.zeros((k, n))
        cnt = np.zeros(n)
        for day in range(1, CLIM_DAYS + 1):
            lag = 24 * day
            acc[:, lag:] += win[:, :-lag] & wv[None, :-lag]
            cnt[lag:] += wv[:-lag]
        with np.errstate(invalid="ignore", divide="ignore"):
            f[f"clim_start_{j}h"] = acc / cnt[None, :]
    return f


def build_grid(alerts: pd.DataFrame, data_end: pd.Timestamp, start: pd.Timestamp | None = None, posts=None) -> Grid:
    """alerts: normalised rows of any unit level. Oblast and raion rows define the
    regions; hromada and city rows define the zones."""
    start = start if start is not None else config.HISTORY_START_UTC - config.LOOKBACK_PAD
    s0 = int(T.to_sec(start.floor("h")))
    end = int(T.to_sec(data_end))
    hours = np.arange(s0, end - end % HOUR + 1, HOUR, dtype=np.int64)
    region_rows = alerts[alerts["unit_level"].isin(config.REGION_UNIT_LEVELS)]
    tls = T.build_timelines(region_rows, REGION_IDS, config.MERGE_GAP_SEC)
    n = len(hours)
    valid = ~excluded_mask(hours - HOUR) & (hours <= end) & (hours - HOUR >= int(T.to_sec(config.DATA_START_UTC)))

    # Grid days: every Kyiv day that has a grid hour.
    first_day, last_day = kyiv_date(hours[[0, -1]])
    days = kyiv_midnights(first_day, last_day)
    col_day = np.searchsorted(days["day"].to_numpy(), kyiv_date(hours))

    # Zones: membership per grid day, from days that are over at its midnight.
    zone_list: dict[str, ZoneSeries] = {}
    units = Z.unit_alerts(alerts)
    if len(units):
        act = Z.activity(units, first_day, last_day)
        member = Z.membership(act, config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE)
        for r, per_day in Z.zone_sets(act, member, REGION_IDS).items():
            uniq = sorted({s for s in per_day if s}, key=lambda s: sorted(s))
            index = {s: i for i, s in enumerate(uniq)}
            day_set = np.array([index[s] if s else -1 for s in per_day], np.int64)
            ivs = [Z.zone_intervals(act, s, tls[r]) for s in uniq]
            names = [sorted(act.keys[u][1] for u in s) for s in uniq]
            zone_list[r + ZONE_SUFFIX] = ZoneSeries(r, day_set, uniq, ivs, names)

    series = tuple(REGION_IDS) + tuple(zone_list)
    S, R = len(series), len(REGION_IDS)
    parent = np.array([REGION_IDS.index(s.split(ZONE_SUFFIX)[0]) for s in series])
    is_zone = np.array([s in zone_list for s in series])
    exists = np.ones((S, n), bool)
    active = np.zeros((S, n), bool)
    state = np.zeros((S, n), np.int8)
    arrays = {k: np.zeros((S, n)) for k in ("cover", "starts")}
    arrays.update({k: np.full((S, n), -1, np.int64) for k in ("last_start", "last_end")})
    for k, r in enumerate(REGION_IDS):
        tl = tls[r]
        a = _own_arrays(tl.s, tl.e, hours)
        active[k] = a["active"]
        state[k] = tl.state_at(hours)
        for q in ("cover", "starts", "last_start", "last_end"):
            arrays[q][k] = a[q]
    own = own_features(hours, valid, state[:R], active[:R], arrays["cover"][:R], arrays["starts"][:R], arrays["last_start"][:R], arrays["last_end"][:R])
    own = {q: np.concatenate([x, np.full((S - R, n), np.nan)]) for q, x in own.items()}
    for k in range(R, S):
        z = zone_list[series[k]]
        set_of_col = z.day_set[col_day]
        exists[k] = set_of_col >= 0
        p = parent[k]
        for j, (s, e) in enumerate(z.intervals):
            cols = set_of_col == j
            a = _own_arrays(s, e, hours)
            st = np.where(a["active"], np.maximum(state[p], T.PART), T.NONE).astype(np.int8)
            fj = own_features(hours, valid, st[None, :], a["active"][None, :], a["cover"][None, :], a["starts"][None, :], a["last_start"][None, :], a["last_end"][None, :])
            for q, x in fj.items():
                own[q][k, cols] = x[0, cols]
            active[k, cols] = a["active"][cols]
            state[k, cols] = st[cols]
    return Grid(hours, tuple(REGION_IDS), series, parent, is_zone, end, valid, exists, active, state, own, tls, days, col_day, zone_list, posts)


def feature_matrix(g: Grid) -> dict[str, np.ndarray]:
    """All features as (S, n) arrays. Zones take neighbourhood, country and
    calendar features from their oblast."""
    S, n = g.active.shape
    R = len(g.regions)
    h = g.hours
    p = g.parent
    f: dict[str, np.ndarray] = dict(g.own)

    def colmean(a):  # mean over regions; NaN where every region is NaN (excluded hours)
        with np.errstate(invalid="ignore"):
            cnt = np.isfinite(a).sum(axis=0)
            return np.where(cnt > 0, np.nansum(a, axis=0) / np.maximum(cnt, 1), np.nan)

    reg_active = g.active[:R].astype(np.float64)
    reg_starts = g.own["starts_24h"][:R]
    starts1 = np.zeros((R, n))
    # Starts in the last hour and the last 3 hours, regions only.
    cover1 = g.own["cover_1h"][:R]
    cover3 = g.own["cover_3h"][:R]
    for k in range(R):
        tl = g.timelines[g.regions[k]]
        starts1[k] = T.count_in(tl.s, h - HOUR, h)
    s3 = _rolling_sum(starts1, 3)
    nb = {q: np.zeros((R, n)) for q in ("nb_active_now", "nb_cover_1h", "nb_cover_3h", "nb_starts_1h", "nb_starts_3h")}
    idx = {r: i for i, r in enumerate(g.regions)}
    for r, i in idx.items():
        nbs = [idx[x] for x in NEIGHBORS[r]]
        nb["nb_active_now"][i] = reg_active[nbs].mean(axis=0)
        nb["nb_cover_1h"][i] = cover1[nbs].mean(axis=0)
        nb["nb_cover_3h"][i] = cover3[nbs].mean(axis=0)
        nb["nb_starts_1h"][i] = starts1[nbs].sum(axis=0)
        nb["nb_starts_3h"][i] = s3[nbs].sum(axis=0)
    for q, a in nb.items():
        f[q] = a[p]
    ones = np.ones((S, 1))
    f["ua_active_now"] = ones * reg_active.mean(axis=0)[None, :]
    f["ua_cover_1h"] = ones * colmean(cover1)[None, :]
    f["ua_cover_24h"] = ones * colmean(g.own["cover_24h"][:R])[None, :]
    f["ua_starts_1h"] = ones * starts1.sum(axis=0)[None, :]
    f["ua_starts_3h"] = ones * s3.sum(axis=0)[None, :]
    f["ua_starts_24h"] = ones * colmean(reg_starts)[None, :]
    # The oblast around a zone (for a region row: its own values).
    f["parent_active_now"] = reg_active[p]
    f["parent_cover_24h"] = g.own["cover_24h"][p]
    f["parent_starts_24h"] = g.own["starts_24h"][p]
    # Calendar (Kyiv local time) and regime.
    local = pd.to_datetime(h, unit="s", utc=True).tz_convert(config.KYIV_TZ)
    hod = local.hour.to_numpy()
    f["hour"] = ones * hod[None, :]
    f["hour_sin"] = ones * np.sin(2 * np.pi * hod / 24)[None, :]
    f["hour_cos"] = ones * np.cos(2 * np.pi * hod / 24)[None, :]
    f["dow"] = ones * local.dayofweek.to_numpy()[None, :]
    doy = local.dayofyear.to_numpy()
    f["doy_sin"] = ones * np.sin(2 * np.pi * doy / 365.25)[None, :]
    f["doy_cos"] = ones * np.cos(2 * np.pi * doy / 365.25)[None, :]
    f["two_level_era"] = ones * (h >= int(T.to_sec(config.LEVEL_SYSTEM_START_UTC)))[None, :]
    v = g.valid.astype(np.float64)[None, :]
    f["valid_frac_672h"] = ones * (_rolling_sum(v, 672)[0] / 672)[None, :]
    f["region"] = p.astype(np.float64)[:, None] * np.ones((1, n))
    if g.posts is not None:
        from . import telegram

        f.update(telegram.text_features(g))
    return f


def to_frame(g: Grid, f: dict[str, np.ndarray], cols: np.ndarray | None = None, rows: np.ndarray | None = None) -> pd.DataFrame:
    """Long table (one row per series x hour) for the selected grid columns."""
    cols = np.arange(len(g.hours)) if cols is None else np.asarray(cols)
    rows = np.arange(len(g.series)) if rows is None else np.asarray(rows)
    data = {k: a[np.ix_(rows, cols)].reshape(-1).astype(np.float32) for k, a in f.items()}
    df = pd.DataFrame(data)
    df.insert(0, "t", np.tile(g.hours[cols], len(rows)))
    df.insert(1, "region_id", np.repeat(np.array(g.series)[rows], len(cols)))
    df.insert(2, "col", np.tile(cols, len(rows)))
    return df


# ---------------------------------------------------------------- targets


def _nanmean(x: np.ndarray) -> np.ndarray:
    n = np.isfinite(x).sum(axis=1)
    return np.where(n > 0, np.nansum(x, axis=1) / np.maximum(n, 1), np.nan)


def _day_values(s: np.ndarray, e: np.ndarray, m0: np.ndarray, m1: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cnt = T.count_in(s, m0, m1).astype(np.float64)
    hrs = (T.covered_before(s, e, m1) - T.covered_before(s, e, m0)) / HOUR
    return cnt, hrs


def daily_targets(g: Grid, first_day: str, last_day: str) -> pd.DataFrame:
    """Phase 1 targets per series x Kyiv day: alerts started in the day and hours
    under alert clipped to the day's bounds (23-25 h on DST days), with naive
    forecasts known at the day's midnight: yesterday, the same weekday last week,
    and means of the last 7 and 28 valid days. For a zone, the history behind
    the naive forecasts is that of the day's members."""
    d = g.days
    m0, m1 = d["m0"].to_numpy(), d["m1"].to_numpy()
    D = len(d)
    day_ok = ~np.isin(d["day"].to_numpy(), np.array(config.EXCLUDED_DAYS)) & (m1 <= g.data_end) & (m0 >= int(T.to_sec(config.DATA_START_UTC)))
    want = np.nonzero((d["day"].to_numpy() >= first_day) & (d["day"].to_numpy() <= last_day))[0]
    rows = []
    for k, sid in enumerate(g.series):
        if g.is_zone[k]:
            z = g.zones[sid]
            day_set = z.day_set
            ivs = z.intervals
        else:
            day_set = np.zeros(D, np.int64)
            tl = g.timelines[sid]
            ivs = [(tl.s, tl.e)]
        vals = [_day_values(s, e, m0, m1) for s, e in ivs]
        cnt_all = np.array([v[0] for v in vals]) if vals else np.zeros((0, D))
        hrs_all = np.array([v[1] for v in vals]) if vals else np.zeros((0, D))
        sel = want[day_set[want] >= 0]
        if len(sel) == 0:
            continue
        j = day_set[sel]
        out = pd.DataFrame({
            "day": d["day"].to_numpy()[sel], "region_id": sid, "kind": "zone" if g.is_zone[k] else "region",
            "m0": m0[sel], "m1": m1[sel], "count": cnt_all[j, sel], "hours": hrs_all[j, sel],
            "day_hours": (m1[sel] - m0[sel]) / HOUR, "valid": day_ok[sel],
        })
        for tgt, allv in (("count", cnt_all), ("hours", hrs_all)):
            lag = np.full((len(sel), BASE_LAGS), np.nan)
            for L in range(1, BASE_LAGS + 1):
                src = sel - L
                ok = (src >= 0) & day_ok[np.maximum(src, 0)]
                lag[ok, L - 1] = allv[j[ok], src[ok]]
            mean7 = _nanmean(lag[:, :7])
            mean28 = _nanmean(lag)
            out[f"b_mean7_{tgt}"] = mean7
            out[f"b_mean28_{tgt}"] = mean28
            out[f"b_yesterday_{tgt}"] = np.where(np.isfinite(lag[:, 0]), lag[:, 0], mean7)
            out[f"b_lastweek_{tgt}"] = np.where(np.isfinite(lag[:, 6]), lag[:, 6], mean7)
        out["set_id"] = j
        rows.append(out)
    return pd.concat(rows, ignore_index=True)


def hourly_targets(g: Grid) -> dict[int, np.ndarray]:
    """Phase 2 targets: y[h][k, i] = 1 if an alert of series k starts in
    (H[i], H[i] + h], NaN where the sample is not usable (alert active at H[i],
    window not fully valid, window beyond the data end, zone without members).
    A zone is judged by the members fixed at the forecast moment."""
    S, n = g.active.shape
    nxt = np.full((S, n), T.OPEN_END, np.int64)
    for k, sid in enumerate(g.series):
        if g.is_zone[k]:
            z = g.zones[sid]
            set_of_col = z.day_set[g.col_day]
            for j, (s, _e) in enumerate(z.intervals):
                cols = set_of_col == j
                nxt[k, cols] = T.next_after(s, g.hours[cols])
        else:
            nxt[k] = T.next_after(g.timelines[sid].s, g.hours)
    out = {}
    for hz in config.HORIZONS:
        y = ((nxt - g.hours[None, :]) <= hz * HOUR).astype(np.float64)
        ok = np.ones(n, bool)
        for d in range(1, hz + 1):
            shifted = np.zeros(n, bool)
            shifted[:-d] = g.valid[d:]
            ok &= shifted
        ok &= g.valid  # the hour before H as well: state at H must be trustworthy
        ok &= g.hours + hz * HOUR <= g.data_end
        y[:, ~ok] = np.nan
        y[g.active] = np.nan
        y[~g.exists] = np.nan
        out[hz] = y
    return out
