"""Rolling-origin backtest: refit every week on everything known at the refit
moment, forecast the following week, never look past the forecast moment."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config, models
from . import timeline as T
from .datasets import Data, last_complete_day

DAY = 86400


def refit_points(first_day: str, last_day: str, every_days: int) -> list[tuple[int, int]]:
    """(refit moment, end of its forecast block) as Kyiv-midnight seconds."""
    days = pd.date_range(first_day, last_day, freq="D").tz_localize(config.KYIV_TZ)
    end = int(T.to_sec((pd.Timestamp(last_day) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)))
    starts = [int(T.to_sec(d)) for d in days[::every_days]]
    ends = starts[1:] + [end]
    return list(zip(starts, ends))


def _train_filter(df: pd.DataFrame, refit: int, time_col: str, train_days) -> pd.Series:
    m = df["known_at"] <= refit
    if train_days:
        m &= df[time_col] >= refit - train_days * DAY
    return m


def phase1(D: Data, first_day: str, last_day: str, params=None, train_days=None, every_days=config.REFIT_DAYS):
    d = D.daily[D.daily["valid"]].copy()
    preds, refits = [], []
    for refit, block_end in refit_points(first_day, last_day, every_days):
        train = d[_train_filter(d, refit, "m0", train_days)]
        test = d[(d["m0"] >= refit) & (d["m0"] < block_end)].copy()
        if test.empty:
            continue
        for tgt in ("count", "hours"):
            m = models.fit_phase1(train, D.feature_cols, tgt, params)
            test[f"p_model_{tgt}"] = models.predict_phase1(m, test, D.feature_cols, tgt)
        test["refit"] = refit
        preds.append(test)
        refits.append({"refit": refit, "train_known_until": int(train["known_at"].max()), "first_forecast": int(test["m0"].min()), "n_train": len(train), "n_test": len(test)})
    return pd.concat(preds, ignore_index=True), refits


def phase2(D: Data, first_day: str, last_day: str, params=None, train_days=None, every_days=config.REFIT_DAYS, horizons=config.HORIZONS):
    h = D.hourly
    lo = int(T.to_sec(pd.Timestamp(first_day).tz_localize(config.KYIV_TZ)))
    hi = int(T.to_sec((pd.Timestamp(last_day) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)))
    window = h[(h["t"] >= lo) & (h["t"] < hi)]
    blocks, refits = [], []
    for refit, block_end in refit_points(first_day, last_day, every_days):
        test = window[(window["t"] >= refit) & (window["t"] < block_end)][["t", "region_id", "day", "hour", "starts_24h"] + [f"y{z}" for z in horizons]].copy()
        if test.empty:
            continue
        for hz in horizons:
            y = f"y{hz}"
            known = h["t"] + hz * 3600 <= refit
            if train_days:
                known &= h["t"] >= refit - train_days * DAY
            train = h[known & h[y].notna()]
            m = models.fit_phase2(train, D.feature_cols, y, params)
            test[f"p_model_{hz}"] = models.predict_phase2(m, window.loc[test.index], D.feature_cols)
            test[f"p_clim_{hz}"] = models.climatology(train, test, y, refit)
            test[f"p_poisson_{hz}"] = models.poisson_rate(test, hz)
            refits.append({"refit": refit, "horizon": hz, "train_known_until": int((train["t"] + hz * 3600).max()), "first_forecast": int(test["t"].min()), "n_train": len(train), "n_test": int(test[y].notna().sum())})
        for name in ("model", "clim"):
            models.make_monotone(test, name, horizons)
        test["refit"] = refit
        blocks.append(test)
    return pd.concat(blocks, ignore_index=True), refits


def selected_params():
    path = config.REPORTS_DIR / "tuning.json"
    if path.exists():
        sel = json.loads(path.read_text())["selected"]
        return sel["phase1"]["params"], sel["phase1"]["train_days"], sel["phase2"]["params"], sel["phase2"]["train_days"]
    return models.PHASE1_PARAMS, models.TRAIN_DAYS, models.PHASE2_PARAMS, models.TRAIN_DAYS


def windows(D: Data) -> dict[str, tuple[str, str]]:
    last = last_complete_day(D.grid.data_end)
    return {"pre_break": config.PRE_BREAK_WINDOW, "post_break": (config.POST_BREAK_START, last)}
