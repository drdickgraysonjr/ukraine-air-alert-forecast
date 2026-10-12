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
        "history_start_utc": str(config.HISTORY_START_UTC),
        "zone_lookback_days": config.ZONE_LOOKBACK_DAYS,
        "zone_min_share": config.ZONE_MIN_SHARE,
        "zone_series": [s for s, z in zip(D.grid.series, D.grid.is_zone) if z],
        "refit_every_days": config.REFIT_DAYS,
        "params": params,
        "train_days": train_days,
        "features": D.feature_cols,
    }


def _ongoing(D: Data, first: str, last: str, kind: str) -> int:
    """Series-hours in the window with an alert in progress: answered with
    'alert in progress' instead of a probability, so not scored."""
    import pandas as pd

    from . import timeline as T

    lo = int(T.to_sec(pd.Timestamp(first).tz_localize(config.KYIV_TZ)))
    hi = int(T.to_sec((pd.Timestamp(last) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ)))
    g = D.grid
    cols = (g.hours >= lo) & (g.hours < hi) & g.valid
    rows = g.is_zone if kind == "zone" else ~g.is_zone
    return int((g.active & g.exists)[np.ix_(rows, cols)].sum())


def run(D: Data) -> None:
    p1_params, p1_days, p2_params, p2_days = backtest.selected_params()
    out1 = {"meta": _meta(D, p1_params, p1_days), "windows": {}, "zones": {"windows": {}}}
    out2 = {"meta": _meta(D, p2_params, p2_days), "windows": {}, "zones": {"windows": {}}}
    out2["meta"]["ongoing_series_hours_not_forecast"] = D.grid.n_ongoing
    config.REPORTS_DIR.mkdir(exist_ok=True)
    config.ARTIFACTS_DIR.mkdir(exist_ok=True)
    for kind in ("region", "zone"):
        tag = "" if kind == "region" else "zone_"
        dst1 = out1["windows"] if kind == "region" else out1["zones"]["windows"]
        dst2 = out2["windows"] if kind == "region" else out2["zones"]["windows"]
        for name, (first, last) in backtest.windows(D).items():
            rng = np.random.default_rng(config.SEED)
            p, refits = backtest.phase1(D, first, last, p1_params, p1_days, kind=kind)
            dst1[name] = {"first_day": first, "last_day": last, "refits": refits, **evaluate.phase1_metrics(p, rng)}
            p.drop(columns=D.feature_cols).to_csv(config.ARTIFACTS_DIR / f"phase1_{tag}{name}.csv.gz", index=False)
            print(f"phase1 {kind} {name}: {len(p)} rows", flush=True)
            rng = np.random.default_rng(config.SEED)
            q, refits = backtest.phase2(D, first, last, p2_params, p2_days, kind=kind)
            dst2[name] = {"first_day": first, "last_day": last, "refits": refits, "ongoing_samples_in_window": _ongoing(D, first, last, kind), **evaluate.phase2_metrics(q, rng)}
            q.to_csv(config.ARTIFACTS_DIR / f"phase2_{tag}{name}.csv.gz", index=False)
            print(f"phase2 {kind} {name}: {len(q)} rows", flush=True)
    from . import telegram

    out1["parser_digest"] = out2["parser_digest"] = telegram.parser_digest() if config.TEXT_CHANNELS else None
    (config.REPORTS_DIR / "metrics_phase1.json").write_text(json.dumps(out1, indent=2) + "\n")
    (config.REPORTS_DIR / "metrics_phase2.json").write_text(json.dumps(out2, indent=2) + "\n")
