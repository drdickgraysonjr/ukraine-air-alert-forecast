"""Hourly feature grid and targets for both phases.

Grid row i describes the moment H[i] (on the hour, UTC). Every feature at H[i]
is computed only from alert starts <= H[i] and from the part of each alert that
lies before H[i]; scripts/verify_leakage.py checks this by recomputing features
from the alert table as it was known at H[i] (timeline.as_of).

Hourly bin i is the hour that ENDS at H[i]: [H[i] - 1h, H[i]).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config
from . import timeline as T
from .regions import NEIGHBORS, REGION_IDS

HOUR = 3600
DAY = 24 * HOUR
ROLL_COVER = (1, 3, 6, 12, 24, 72, 168, 672)
ROLL_COUNT = (24, 168, 672)
CLIM_DAYS = 28
CAP_H = 24 * 28.0


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


@dataclass
class Grid:
    hours: np.ndarray  # (n,) int seconds, H[i]
    regions: tuple[str, ...]
    data_end: int
    valid: np.ndarray  # (n,) bin [H-1h, H) holds trustworthy data
    active: np.ndarray  # (R, n) bool, active at H
    state: np.ndarray  # (R, n) int8
    cover: np.ndarray  # (R, n) seconds under alert in bin
    starts: np.ndarray  # (R, n) alert starts (union) in bin
    ep_starts: np.ndarray  # (R, n) counted alerts (gap-merged) starting in bin
    last_start: np.ndarray  # (R, n) last union start <= H, -1 if none
    last_end: np.ndarray  # (R, n) last union end <= H, -1 if none
    timelines: dict


def build_grid(alerts: pd.DataFrame, data_end: pd.Timestamp, start: pd.Timestamp | None = None) -> Grid:
    start = start if start is not None else config.HISTORY_START_UTC - config.LOOKBACK_PAD
    s0 = int(T.to_sec(start.floor("h")))
    end = int(T.to_sec(data_end))
    hours = np.arange(s0, end - end % HOUR + 1, HOUR, dtype=np.int64)
    tls = T.build_timelines(alerts, REGION_IDS, config.MERGE_GAP_SEC)
    R, n = len(REGION_IDS), len(hours)
    active = np.zeros((R, n), bool)
    state = np.zeros((R, n), np.int8)
    cover = np.zeros((R, n), np.float64)
    starts = np.zeros((R, n), np.float64)
    ep_starts = np.zeros((R, n), np.float64)
    last_start = np.zeros((R, n), np.int64)
    last_end = np.zeros((R, n), np.int64)
    for k, r in enumerate(REGION_IDS):
        tl = tls[r]
        active[k] = T.active_at(tl.s, tl.e, hours)
        state[k] = tl.state_at(hours)
        cover[k] = T.covered_before(tl.s, tl.e, hours) - T.covered_before(tl.s, tl.e, hours - HOUR)
        starts[k] = T.count_in(tl.s, hours - HOUR, hours)
        ep_starts[k] = T.count_in(tl.ep_s, hours - HOUR, hours)
        last_start[k] = T.last_before(tl.s, hours)
        ends = tl.e[tl.e < T.OPEN_END]
        last_end[k] = T.last_before(ends, hours)
    valid = ~excluded_mask(hours - HOUR) & (hours <= end)
    return Grid(hours, REGION_IDS, end, valid, active, state, cover, starts, ep_starts, last_start, last_end, tls)


def _since(h: np.ndarray, last: np.ndarray) -> np.ndarray:
    out = (h[None, :] - last) / HOUR
    out = np.where(last < 0, np.nan, np.minimum(out, CAP_H))
    return out


def feature_matrix(g: Grid) -> dict[str, np.ndarray]:
    """All features as (R, n) arrays."""
    R, n = g.active.shape
    h = g.hours
    v = g.valid.astype(np.float64)[None, :]
    f: dict[str, np.ndarray] = {}
    f["state_now"] = g.state.astype(np.float64)
    f["active_now"] = g.active.astype(np.float64)
    since_start = _since(h, g.last_start)
    f["elapsed_active_h"] = np.where(g.active, since_start, 0.0)
    f["since_start_h"] = since_start
    f["since_end_h"] = _since(h, g.last_end)
    vsum = {w: _rolling_sum(np.broadcast_to(v, (1, n)), w)[0] for w in set(ROLL_COVER) | set(ROLL_COUNT)}
    with np.errstate(invalid="ignore", divide="ignore"):
        for w in ROLL_COVER:
            f[f"cover_{w}h"] = _rolling_sum(g.cover * v, w) / (vsum[w] * HOUR)
        for w in ROLL_COUNT:
            f[f"starts_{w}h"] = _rolling_sum(g.starts * v, w) / vsum[w] * 24
            f[f"alerts_{w}h"] = _rolling_sum(g.ep_starts * v, w) / vsum[w] * 24
    for lag in (24, 168):
        a = np.full((R, n), np.nan)
        a[:, lag:] = g.active[:, :-lag]
        f[f"active_lag_{lag}h"] = a
    # Same-clock-time climatology over the previous 28 days: did an alert start
    # within the next k hours of that moment? Uses bins that end <= H only.
    nxt = np.zeros((R, n + 6))
    nxt[:, :n] = g.starts  # starts in bin ending at H[i]
    vv = np.zeros(n + 6)
    vv[:n] = g.valid
    for k in (1, 3, 6):
        # any start in (H[j], H[j] + k h]  ==  bins j+1 .. j+k
        win = sum(nxt[:, 1 + d : n + 1 + d] for d in range(k)) > 0
        wv = np.prod([vv[1 + d : n + 1 + d] for d in range(k)], axis=0) > 0
        acc = np.zeros((R, n))
        cnt = np.zeros(n)
        for day in range(1, CLIM_DAYS + 1):
            lag = 24 * day
            acc[:, lag:] += (win[:, :-lag] & wv[None, :-lag])
            cnt[lag:] += wv[:-lag]
        with np.errstate(invalid="ignore", divide="ignore"):
            f[f"clim_start_{k}h"] = acc / cnt[None, :]
    # Neighbours and the whole country.
    idx = {r: i for i, r in enumerate(g.regions)}
    nb_active = np.zeros((R, n))
    nb_cover1 = np.zeros((R, n))
    nb_cover3 = np.zeros((R, n))
    nb_starts1 = np.zeros((R, n))
    nb_starts3 = np.zeros((R, n))
    s3 = _rolling_sum(g.starts, 3)
    for r, i in idx.items():
        nbs = [idx[x] for x in NEIGHBORS[r]]
        nb_active[i] = g.active[nbs].mean(axis=0)
        nb_cover1[i] = f["cover_1h"][nbs].mean(axis=0)
        nb_cover3[i] = f["cover_3h"][nbs].mean(axis=0)
        nb_starts1[i] = g.starts[nbs].sum(axis=0)
        nb_starts3[i] = s3[nbs].sum(axis=0)
    f["nb_active_now"] = nb_active
    f["nb_cover_1h"] = nb_cover1
    f["nb_cover_3h"] = nb_cover3
    f["nb_starts_1h"] = nb_starts1
    f["nb_starts_3h"] = nb_starts3
    ones = np.ones((R, 1))
    def colmean(a):  # mean over regions; NaN where every region is NaN (excluded hours)
        with np.errstate(invalid="ignore"):
            cnt = np.isfinite(a).sum(axis=0)
            return np.where(cnt > 0, np.nansum(a, axis=0) / np.maximum(cnt, 1), np.nan)
    f["ua_active_now"] = ones * g.active.mean(axis=0)[None, :]
    f["ua_cover_1h"] = ones * colmean(f["cover_1h"])[None, :]
    f["ua_cover_24h"] = ones * colmean(f["cover_24h"])[None, :]
    f["ua_starts_1h"] = ones * g.starts.sum(axis=0)[None, :]
    f["ua_starts_3h"] = ones * s3.sum(axis=0)[None, :]
    f["ua_alerts_24h"] = ones * colmean(f["alerts_24h"])[None, :]
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
    f["valid_frac_672h"] = ones * (vsum[672] / 672)[None, :]
    f["region"] = np.arange(R, dtype=np.float64)[:, None] * np.ones((1, n))
    return f


FEATURES: tuple[str, ...] = ()  # filled on first call of to_frame


def to_frame(g: Grid, f: dict[str, np.ndarray], cols: np.ndarray | None = None) -> pd.DataFrame:
    """Long table (one row per region x hour) for the selected grid columns."""
    cols = np.arange(len(g.hours)) if cols is None else np.asarray(cols)
    R = len(g.regions)
    data = {k: a[:, cols].reshape(-1).astype(np.float32) for k, a in f.items()}
    df = pd.DataFrame(data)
    df.insert(0, "t", np.tile(g.hours[cols], R))
    df.insert(1, "region_id", np.repeat(np.array(g.regions), len(cols)))
    df.insert(2, "col", np.tile(cols, R))
    return df


# ---------------------------------------------------------------- targets


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


def daily_targets(g: Grid, first_day: str, last_day: str) -> pd.DataFrame:
    """Phase 1 targets per region x Kyiv day: alerts started in the day (gap-merged)
    and hours under alert clipped to the day's bounds (23-25 h on DST days)."""
    d = kyiv_midnights(first_day, last_day)
    m0, m1 = d["m0"].to_numpy(), d["m1"].to_numpy()
    ok = ~np.isin(d["day"].to_numpy(), np.array(config.EXCLUDED_DAYS)) & (m1 <= g.data_end)
    rows = []
    for r in g.regions:
        tl = g.timelines[r]
        cnt = T.count_in(tl.ep_s, m0, m1)
        hrs = (T.covered_before(tl.s, tl.e, m1) - T.covered_before(tl.s, tl.e, m0)) / HOUR
        rows.append(pd.DataFrame({"day": d["day"], "region_id": r, "m0": m0, "m1": m1, "count": cnt, "hours": hrs, "day_hours": (m1 - m0) / HOUR, "valid": ok}))
    return pd.concat(rows, ignore_index=True)


def hourly_targets(g: Grid) -> dict[int, np.ndarray]:
    """Phase 2 targets: y[h][r, i] = 1 if an alert starts in (H[i], H[i] + h],
    NaN where the sample is not usable (alert active at H[i], window not fully
    valid, or window beyond the data end)."""
    R, n = g.active.shape
    nxt = np.zeros((R, n), np.int64)
    for k, r in enumerate(g.regions):
        nxt[k] = T.next_after(g.timelines[r].s, g.hours)
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
        out[hz] = y
    return out
