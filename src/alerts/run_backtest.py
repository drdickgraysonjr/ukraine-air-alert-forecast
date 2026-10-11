"""Run both phases on both test windows and write reports/metrics_phase{1,2}.json."""

from __future__ import annotations

import json

import numpy as np

from . import backtest, config, evaluate
from .datasets import Data, last_complete_day


def _meta(D: Data, params, train_days) -> dict:
    return {
        "data_end_utc": str(config.DATA_END_UTC),
        "last_complete_day": last_complete_day(D.grid.data_end),
        "cutover_utc": str(config.CUTOVER_UTC),
        "excluded_days": list(config.EXCLUDED_DAYS),
        "merge_gap_sec": config.MERGE_GAP_SEC,
        "refit_every_days": config.REFIT_DAYS,
        "params": params,
        "train_days": train_days,
        "features": D.feature_cols,
    }


def _ongoing(D: Data, first: str, last: str) -> int:
    """Region-hours in the window with an alert in progress: answered with
    'alert in progress' instead of a probability, so not scored."""
    import pandas as pd

    from . import timeline as T

    lo = int(T.to_sec(pd.Timestamp(first).tz_localize(config.KYIV_TZ)))
    hi = int(T.to_sec((pd.Timestamp(last) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)))
    g = D.grid
    cols = (g.hours >= lo) & (g.hours < hi) & g.valid
    return int(g.active[:, cols].sum())


def run(D: Data) -> None:
    p1_params, p1_days, p2_params, p2_days = backtest.selected_params()
    out1 = {"meta": _meta(D, p1_params, p1_days), "windows": {}}
    out2 = {"meta": _meta(D, p2_params, p2_days), "windows": {}}
    out2["meta"]["ongoing_region_hours_not_forecast"] = D.grid.n_ongoing
    config.REPORTS_DIR.mkdir(exist_ok=True)
    config.ARTIFACTS_DIR.mkdir(exist_ok=True)
    for name, (first, last) in backtest.windows(D).items():
        rng = np.random.default_rng(config.SEED)
        p, refits = backtest.phase1(D, first, last, p1_params, p1_days)
        out1["windows"][name] = {"first_day": first, "last_day": last, "refits": refits, **evaluate.phase1_metrics(p, rng)}
        p.to_csv(config.ARTIFACTS_DIR / f"phase1_{name}.csv.gz", index=False)
        print(f"phase1 {name}: {len(p)} rows", flush=True)
        rng = np.random.default_rng(config.SEED)
        q, refits = backtest.phase2(D, first, last, p2_params, p2_days)
        out2["windows"][name] = {"first_day": first, "last_day": last, "refits": refits, "ongoing_samples_in_window": _ongoing(D, first, last), **evaluate.phase2_metrics(q, rng)}
        q.to_csv(config.ARTIFACTS_DIR / f"phase2_{name}.csv.gz", index=False)
        print(f"phase2 {name}: {len(q)} rows", flush=True)
    (config.REPORTS_DIR / "metrics_phase1.json").write_text(json.dumps(out1, indent=2) + "\n")
    (config.REPORTS_DIR / "metrics_phase2.json").write_text(json.dumps(out2, indent=2) + "\n")
