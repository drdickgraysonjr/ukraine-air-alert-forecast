"""Choose hyper-parameters on the validation window only (before both test windows)."""

from __future__ import annotations

import itertools
import json

import numpy as np

from . import backtest, config, evaluate, telegram

P1_GRID = {"learning_rate": [0.05, 0.1], "max_leaf_nodes": [15, 31], "min_samples_leaf": [100], "l2_regularization": [1.0], "max_iter": [300]}
P2_GRID = {"learning_rate": [0.1], "max_leaf_nodes": [31, 63], "min_samples_leaf": [200, 1000], "l2_regularization": [1.0], "max_iter": [200]}
TRAIN_DAYS = [None, 365]
# Monthly refits keep tuning affordable; the test windows use weekly refits.
EVERY = 28
P2_TUNE_HORIZONS = (1, 3, 6)


def _grid(g):
    keys = list(g)
    for vals in itertools.product(*(g[k] for k in keys)):
        yield dict(zip(keys, vals))


def run(D) -> dict:
    first, last = config.VALIDATION_WINDOW
    rng = np.random.default_rng(config.SEED)
    trials1, trials2 = [], []
    for params, td in itertools.product(_grid(P1_GRID), TRAIN_DAYS):
        p, _ = backtest.phase1(D, first, last, params, td, EVERY)
        m = evaluate.phase1_metrics(p, rng)["targets"]
        # Score: model MAE relative to the best naive baseline, averaged over both targets.
        score = float(np.mean([m[t]["methods"]["model"]["mae"] / m[t]["methods"][m[t]["best_baseline"]]["mae"] for t in m]))
        trials1.append({"params": params, "train_days": td, "relative_mae": score})
        print("phase1", params, td, round(score, 4), flush=True)
    for params, td in itertools.product(_grid(P2_GRID), TRAIN_DAYS):
        p, _ = backtest.phase2(D, first, last, params, td, EVERY, P2_TUNE_HORIZONS)
        m = evaluate.phase2_metrics(p, rng, P2_TUNE_HORIZONS)["horizons"]
        score = float(np.mean([m[h]["methods"]["model"]["log_loss"] for h in m]))
        trials2.append({"params": params, "train_days": td, "mean_log_loss": score})
        print("phase2", params, td, round(score, 5), flush=True)
    best1 = min(trials1, key=lambda x: x["relative_mae"])
    best2 = min(trials2, key=lambda x: x["mean_log_loss"])
    out = {
        "validation_window": list(config.VALIDATION_WINDOW),
        "config": config_snapshot(D),
        "refit_every_days": EVERY,
        "phase1_trials": trials1,
        "phase2_trials": trials2,
        "selected": {"phase1": best1, "phase2": best2},
        "parser_digest": telegram.parser_digest() if config.TEXT_CHANNELS else None,
    }
    config.REPORTS_DIR.mkdir(exist_ok=True)
    (config.REPORTS_DIR / "tuning.json").write_text(json.dumps(out, indent=2) + "\n")
    return out


def config_snapshot(D) -> dict:
    """The data choices the hyper-parameters were tuned under."""
    return {
        "history_start_utc": str(config.HISTORY_START_UTC),
        "zone_lookback_days": config.ZONE_LOOKBACK_DAYS,
        "zone_min_share": config.ZONE_MIN_SHARE,
        "text_channels": list(config.TEXT_CHANNELS),
        "merge_gap_sec": config.MERGE_GAP_SEC,
        "features": list(D.feature_cols),
    }
