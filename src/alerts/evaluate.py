"""Metrics with day-level bootstrap intervals."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from . import config

B = 1000
EPS = 1e-6


def _boot_days(day: np.ndarray, rng) -> np.ndarray:
    """(B, n_days) indices of resampled days."""
    uniq = np.unique(day)
    return uniq, rng.integers(0, len(uniq), (B, len(uniq)))


def _per_day(day: np.ndarray, uniq: np.ndarray, x: np.ndarray) -> np.ndarray:
    pos = np.searchsorted(uniq, day)
    return np.bincount(pos, weights=x, minlength=len(uniq))


def phase1_metrics(p: pd.DataFrame, rng) -> dict:
    out = {"n_rows": len(p), "n_days": int(p["day"].nunique()), "targets": {}}
    day = p["day"].to_numpy()
    uniq, idx = _boot_days(day, rng)
    for tgt in ("count", "hours"):
        y = p[tgt].to_numpy(float)
        res = {"mean_actual": float(y.mean()), "methods": {}}
        cand = {"model": p[f"p_model_{tgt}"].to_numpy(float)}
        for b in ("yesterday", "lastweek", "mean7", "mean28"):
            cand[b] = p[f"b_{b}_{tgt}"].to_numpy(float)
        abs_day = {k: _per_day(day, uniq, np.abs(v - y)) for k, v in cand.items()}
        for k, v in cand.items():
            res["methods"][k] = {"mae": float(np.mean(np.abs(v - y))), "rmse": float(np.sqrt(np.mean((v - y) ** 2)))}
        for b in ("yesterday", "lastweek", "mean7", "mean28"):
            # Skill = 1 - MAE_model / MAE_baseline, bootstrap over days.
            bm = abs_day["model"][idx].sum(1)
            bb = abs_day[b][idx].sum(1)
            skill = 1 - bm / bb
            res["methods"][b]["model_skill"] = float(1 - res["methods"]["model"]["mae"] / res["methods"][b]["mae"])
            res["methods"][b]["model_skill_ci90"] = [float(np.quantile(skill, 0.05)), float(np.quantile(skill, 0.95))]
        best = min(("yesterday", "lastweek", "mean7", "mean28"), key=lambda b: res["methods"][b]["mae"])
        res["best_baseline"] = best
        res["per_region_mae"] = {
            r: {"model": float(np.mean(np.abs(g[f"p_model_{tgt}"] - g[tgt]))), best: float(np.mean(np.abs(g[f"b_{best}_{tgt}"] - g[tgt])))}
            for r, g in p.groupby("region_id")
        }
        out["targets"][tgt] = res
    return out


def _brier(p, y):
    return float(np.mean((p - y) ** 2))


def _logloss(p, y):
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    k = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    out = []
    for b in range(bins):
        m = k == b
        if m.any():
            out.append({"bin": [float(edges[b]), float(edges[b + 1])], "n": int(m.sum()), "mean_forecast": float(p[m].mean()), "observed": float(y[m].mean())})
    return out


def phase2_metrics(p: pd.DataFrame, rng, horizons=config.HORIZONS) -> dict:
    out = {"horizons": {}}
    for hz in horizons:
        m = p[f"y{hz}"].notna()
        q = p[m]
        y = q[f"y{hz}"].to_numpy(float)
        day = q["day"].to_numpy()
        uniq, idx = _boot_days(day, rng)
        res = {"n": int(len(q)), "base_rate": float(y.mean()), "methods": {}}
        sq = {}
        for name in ("model", "clim", "poisson"):
            pr = q[f"p_{name}_{hz}"].to_numpy(float)
            # AUC is undefined when every label is the same (a zone that always gets an alert).
            auc = float(roc_auc_score(y, pr)) if 0 < y.sum() < len(y) else None
            res["methods"][name] = {"brier": _brier(pr, y), "log_loss": _logloss(pr, y), "auc": auc}
            sq[name] = _per_day(day, uniq, (pr - y) ** 2)
        for ref in ("clim", "poisson"):
            bss = 1 - sq["model"][idx].sum(1) / sq[ref][idx].sum(1)
            res["methods"][ref]["model_bss"] = float(1 - res["methods"]["model"]["brier"] / res["methods"][ref]["brier"])
            res["methods"][ref]["model_bss_ci90"] = [float(np.quantile(bss, 0.05)), float(np.quantile(bss, 0.95))]
        res["reliability"] = reliability(q[f"p_model_{hz}"].to_numpy(float), y)
        out["horizons"][str(hz)] = res
    return out
