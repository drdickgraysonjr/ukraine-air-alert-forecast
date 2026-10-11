"""Model-ready tables for both phases, with the moment each label becomes known."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config, features
from . import timeline as T

HOUR = 3600


@dataclass
class Data:
    grid: features.Grid
    feats: dict[str, np.ndarray]
    daily: pd.DataFrame  # phase 1 rows
    hourly: pd.DataFrame  # phase 2 rows
    feature_cols: list[str]


def last_complete_day(data_end_sec: int) -> str:
    """Last Kyiv calendar day that ended before the data end."""
    end = pd.Timestamp(data_end_sec, unit="s", tz="UTC").tz_convert(config.KYIV_TZ)
    return str((end.normalize() - pd.Timedelta(days=1)).date())


def build(alerts: pd.DataFrame, data_end: pd.Timestamp) -> Data:
    g = features.build_grid(alerts, data_end)
    f = features.feature_matrix(g)
    cols = [k for k in f]
    first_day = str(config.HISTORY_START_UTC.tz_convert(config.KYIV_TZ).date())
    last_day = last_complete_day(g.data_end)

    # Phase 1: one row per region x Kyiv day, features at the day's midnight.
    d = features.daily_targets(g, first_day, last_day)
    col_of = pd.Series(np.arange(len(g.hours)), index=g.hours)
    d["col"] = col_of.reindex(d["m0"]).to_numpy()
    d = d[d["col"].notna()].copy()
    d["col"] = d["col"].astype(int)
    ridx = {r: i for i, r in enumerate(g.regions)}
    ri = d["region_id"].map(ridx).to_numpy()
    for k in cols:
        d[k] = f[k][ri, d["col"].to_numpy()].astype(np.float32)
    d = _add_daily_baselines(d)
    d["known_at"] = d["m1"]  # the day's targets are final at its end

    # Phase 2: one row per region x hour with at least one usable horizon.
    y = features.hourly_targets(g)
    hstart = int(T.to_sec(config.HISTORY_START_UTC))
    mask = np.zeros(g.active.shape, bool)
    for v in y.values():
        mask |= np.isfinite(v)
    mask[:, g.hours < hstart] = False
    r_i, c_i = np.nonzero(mask)
    h = pd.DataFrame({"t": g.hours[c_i], "region_id": np.array(g.regions)[r_i], "col": c_i})
    for k in cols:
        h[k] = f[k][r_i, c_i].astype(np.float32)
    for hz, v in y.items():
        h[f"y{hz}"] = v[r_i, c_i].astype(np.float32)
    h["day"] = features.kyiv_date(h["t"].to_numpy())
    # Ongoing alerts are not forecast; keep their count for the report.
    act = g.active.copy()
    act[:, g.hours < hstart] = False
    act[:, ~g.valid] = False
    g.n_ongoing = int(act.sum())
    return Data(g, f, d.reset_index(drop=True), h, cols)


def _add_daily_baselines(d: pd.DataFrame) -> pd.DataFrame:
    """Naive forecasts known at the day's midnight: yesterday, the same weekday
    last week, and means of the last 7 and 28 valid days."""
    out = []
    for r, x in d.sort_values("day").groupby("region_id", sort=False):
        x = x.copy()
        for tgt in ("count", "hours"):
            val = x[tgt].where(x["valid"])
            mean7 = val.shift(1).rolling(7, min_periods=1).mean()
            x[f"b_mean7_{tgt}"] = mean7
            x[f"b_mean28_{tgt}"] = val.shift(1).rolling(28, min_periods=1).mean()
            x[f"b_yesterday_{tgt}"] = val.shift(1).fillna(mean7)
            x[f"b_lastweek_{tgt}"] = val.shift(7).fillna(mean7)
        out.append(x)
    return pd.concat(out).sort_values(["day", "region_id"]).reset_index(drop=True)
