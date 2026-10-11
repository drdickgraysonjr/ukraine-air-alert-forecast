"""Models for both phases. Hyper-parameters were chosen on the validation window
(config.VALIDATION_WINDOW) by `alerts tune`; the backtest and the forecast read
them from reports/tuning.json, and the defaults below mirror that selection."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

from . import config

# Selected on the validation window; kept fixed for both test windows.
PHASE1_PARAMS = dict(learning_rate=0.05, max_iter=300, max_leaf_nodes=31, min_samples_leaf=100, l2_regularization=1.0)
PHASE2_PARAMS = dict(learning_rate=0.1, max_iter=200, max_leaf_nodes=31, min_samples_leaf=200, l2_regularization=1.0)
# Training history used at each refit (days before the refit moment; None = all).
TRAIN_DAYS = None


def _cat(cols: list[str]) -> list[int]:
    return [cols.index("region")]


def fit_phase1(train: pd.DataFrame, cols: list[str], target: str, params=None):
    p = dict(PHASE1_PARAMS if params is None else params)
    m = HistGradientBoostingRegressor(loss="poisson", early_stopping=False, categorical_features=_cat(cols), random_state=config.SEED, **p)
    return m.fit(train[cols], train[target])


def predict_phase1(model, test: pd.DataFrame, cols: list[str], target: str) -> np.ndarray:
    pred = model.predict(test[cols])
    if target == "hours":
        pred = np.minimum(pred, test["day_hours"].to_numpy())
    return np.maximum(pred, 0)


def fit_phase2(train: pd.DataFrame, cols: list[str], target: str, params=None):
    p = dict(PHASE2_PARAMS if params is None else params)
    m = HistGradientBoostingClassifier(early_stopping=False, categorical_features=_cat(cols), random_state=config.SEED, **p)
    return m.fit(train[cols], train[target].astype(int))


def predict_phase2(model, test: pd.DataFrame, cols: list[str]) -> np.ndarray:
    return model.predict_proba(test[cols])[:, 1]


def climatology(train: pd.DataFrame, test: pd.DataFrame, target: str, refit_sec: int, days: int = 28, prior: float = 2.0) -> np.ndarray:
    """Baseline: frequency of the event for (region, Kyiv hour of day) over the
    last `days` days of labels known at the refit, shrunk to the region mean."""
    recent = train[train["t"] >= refit_sec - days * 86400]
    y = recent[target]
    reg = recent.groupby("region_id")[target].mean()
    cell = recent.assign(y=y).groupby(["region_id", "hour"])["y"].agg(["sum", "count"])
    key = pd.MultiIndex.from_arrays([test["region_id"], test["hour"]])
    s = cell["sum"].reindex(key).to_numpy()
    n = cell["count"].reindex(key).to_numpy()
    base = reg.reindex(test["region_id"]).to_numpy()
    base = np.where(np.isnan(base), float(y.mean()), base)
    s = np.nan_to_num(s)
    n = np.nan_to_num(n)
    return (s + prior * base) / (n + prior)


def poisson_rate(test: pd.DataFrame, hz: int) -> np.ndarray:
    """Baseline: alert starts per day over the last 24 h as a Poisson rate."""
    rate = np.nan_to_num(test["starts_24h"].to_numpy(), nan=0.0) / 24.0
    return 1 - np.exp(-rate * hz)


def make_monotone(df: pd.DataFrame, name: str, horizons) -> None:
    """P(alert within h hours) cannot fall as h grows; separately fitted models
    can disagree slightly, so take the running maximum over horizons."""
    cols = [f"p_{name}_{hz}" for hz in sorted(horizons)]
    df[cols] = np.maximum.accumulate(df[cols].to_numpy(), axis=1)
