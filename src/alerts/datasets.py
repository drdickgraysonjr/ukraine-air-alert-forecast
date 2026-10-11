"""Model-ready tables for both phases, with the moment each label becomes known.

Each row carries `kind`: "region" (oblast- and raion-level alerts, the rest of
the oblast) or "zone" (the oblast's zone of regular alerts). The two kinds are
modelled and scored separately.
"""

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


def default_posts():
    """Posts of config.TEXT_CHANNELS, or None when no channel is used."""
    if not config.TEXT_CHANNELS:
        return None
    from . import telegram

    return telegram.Posts.load(config.TEXT_CHANNELS, drop_edited=config.DROP_EDITED_POSTS)


def load_default() -> Data:
    from . import stitch

    return build(stitch.load_all(), config.DATA_END_UTC, posts=default_posts())


def build(alerts: pd.DataFrame, data_end: pd.Timestamp, posts=None) -> Data:
    g = features.build_grid(alerts, data_end, posts=posts)
    f = features.feature_matrix(g)
    cols = [k for k in f]
    first_day = str(config.HISTORY_START_UTC.tz_convert(config.KYIV_TZ).date())
    last_day = last_complete_day(g.data_end)
    sidx = {s: i for i, s in enumerate(g.series)}

    # Phase 1: one row per series x Kyiv day, features at the day's midnight.
    d = features.daily_targets(g, first_day, last_day)
    col_of = pd.Series(np.arange(len(g.hours)), index=g.hours)
    d["col"] = col_of.reindex(d["m0"]).to_numpy()
    d = d[d["col"].notna()].copy()
    d["col"] = d["col"].astype(int)
    ri = d["region_id"].map(sidx).to_numpy()
    feats = pd.DataFrame({k: f[k][ri, d["col"].to_numpy()].astype(np.float32) for k in cols}, index=d.index)
    d = pd.concat([d, feats], axis=1)
    d["known_at"] = d["m1"]  # the day's targets are final at its end
    d = d.sort_values(["day", "kind", "region_id"], kind="stable").reset_index(drop=True)

    # Phase 2: one row per series x hour with at least one usable horizon.
    y = features.hourly_targets(g)
    hstart = int(T.to_sec(config.HISTORY_START_UTC))
    mask = np.zeros(g.active.shape, bool)
    for v in y.values():
        mask |= np.isfinite(v)
    mask[:, g.hours < hstart] = False
    r_i, c_i = np.nonzero(mask)
    h = pd.DataFrame({"t": g.hours[c_i], "region_id": np.array(g.series)[r_i], "col": c_i})
    h["kind"] = np.where(g.is_zone[r_i], "zone", "region")
    h = pd.concat([h, pd.DataFrame({k: f[k][r_i, c_i].astype(np.float32) for k in cols})], axis=1)
    for hz, v in y.items():
        h[f"y{hz}"] = v[r_i, c_i].astype(np.float32)
    h["day"] = features.kyiv_date(h["t"].to_numpy())
    # Ongoing alerts are not forecast; keep their count for the report.
    act = g.active & g.exists
    act[:, g.hours < hstart] = False
    act[:, ~g.valid] = False
    g.n_ongoing = {"region": int(act[~g.is_zone].sum()), "zone": int(act[g.is_zone].sum())}
    return Data(g, f, d, h, cols)
