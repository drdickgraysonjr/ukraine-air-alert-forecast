"""Model choices made on the validation window only (config.VALIDATION_WINDOW).

Every decision is a set of trials scored on validation days, written to
reports/validation.json together with the criterion named before the trials:

- zones: (lookback, share) by the F1 of zone membership against realised
  regularity in the next week (alerts.zones.select_params);
- history: the first day of the training history;
- text: which Telegram channels feed the text features (none, all, or all but one).

History and text are scored with the same criterion, `score`: the mean of the
phase 1 ratio "model MAE / best naive MAE" (weekly refits, both targets) and the
phase 2 ratio "model log loss / climatology log loss" (refits every 28 days,
horizons 1, 3 and 6 h), both for the 24 regions. Lower is better.
"""

from __future__ import annotations

import json
import time

import numpy as np

from . import backtest, config, evaluate

P2_EVERY = 28
P2_HORIZONS = (1, 3, 6)
PATH = "validation.json"


def load() -> dict:
    p = config.REPORTS_DIR / PATH
    return json.loads(p.read_text()) if p.exists() else {"validation_window": list(config.VALIDATION_WINDOW)}


def save(out: dict) -> None:
    config.REPORTS_DIR.mkdir(exist_ok=True)
    (config.REPORTS_DIR / PATH).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")


def score(D, kind: str = "region") -> dict:
    """Validation-window score of the current configuration (see module doc)."""
    first, last = config.VALIDATION_WINDOW
    p1_params, p1_days, p2_params, p2_days = backtest.selected_params()
    rng = np.random.default_rng(config.SEED)
    t0 = time.time()
    p, refits1 = backtest.phase1(D, first, last, p1_params, p1_days, kind=kind)
    m1 = evaluate.phase1_metrics(p, rng)["targets"]
    rel1 = {t: m1[t]["methods"]["model"]["mae"] / m1[t]["methods"][m1[t]["best_baseline"]]["mae"] for t in m1}
    q, refits2 = backtest.phase2(D, first, last, p2_params, p2_days, P2_EVERY, P2_HORIZONS, kind=kind)
    m2 = evaluate.phase2_metrics(q, rng, P2_HORIZONS)["horizons"]
    rel2 = {h: m2[h]["methods"]["model"]["log_loss"] / m2[h]["methods"]["clim"]["log_loss"] for h in m2}
    days = sorted(set(p["day"]) | set(q["day"]))
    return {
        "phase1_rel_mae": {t: float(v) for t, v in rel1.items()},
        "phase1_mae": {t: m1[t]["methods"]["model"]["mae"] for t in m1},
        "phase2_rel_logloss": {h: float(v) for h, v in rel2.items()},
        "phase2_bss_clim": {h: m2[h]["methods"]["clim"]["model_bss"] for h in m2},
        "score": float((np.mean(list(rel1.values())) + np.mean(list(rel2.values()))) / 2),
        "first_scored_day": days[0],
        "last_scored_day": days[-1],
        "n_phase1_rows": int(len(p)),
        "n_phase2_rows": int(len(q)),
        "train_known_until_max": int(max([r["train_known_until"] for r in refits1 + refits2])),
        "seconds": round(time.time() - t0, 1),
    }


def pick(trials: list[dict], key: str = "option") -> dict:
    best = min(trials, key=lambda t: t["score"])
    return {"criterion": "lowest score: mean of phase 1 relative MAE and phase 2 relative log loss (regions)", "selected": best[key]}


ZONE_LOOKBACKS = (7, 14, 28, 56)
ZONE_SHARES = (0.5, 0.6, 0.7, 0.8, 0.9)
HISTORY_OPTIONS = ("2022-04-13", "2023-01-01")


def _data(posts=None):
    from . import datasets, stitch

    return datasets.build(stitch.load_all(), config.DATA_END_UTC, posts=posts)


def _posts(channels, drop_edited=False):
    from . import telegram

    return telegram.Posts.load(channels, drop_edited=drop_edited) if channels else None


def step_zones() -> dict:
    from . import stitch, zones

    units = zones.unit_alerts(stitch.load_all())
    a = zones.activity(units, "2022-03-16", str(config.DATA_END_UTC.tz_convert(config.KYIV_TZ).date()))
    res = zones.select_params(a, *config.VALIDATION_WINDOW, ZONE_LOOKBACKS, ZONE_SHARES)
    out = load()
    out["zones"] = res
    save(out)
    return res


def step_history() -> dict:
    """First day of the training history: with 2022 (from 4 weeks after the
    official feed starts, so every lookback is filled) or from 2023."""
    import pandas as pd

    keep = config.HISTORY_START_UTC
    trials = []
    try:
        for opt in HISTORY_OPTIONS:
            config.HISTORY_START_UTC = pd.Timestamp(opt, tz=config.KYIV_TZ).tz_convert("UTC")
            s = score(_data())
            s["option"] = opt
            print("history", opt, round(s["score"], 4), s["phase1_rel_mae"], s["phase2_rel_logloss"], flush=True)
            trials.append(s)
    finally:
        config.HISTORY_START_UTC = keep
    out = load()
    out["history"] = {"trials": trials, **pick(trials)}
    save(out)
    return out["history"]


ALL_CHANNELS = ("kpszsu", "war_monitor", "eradar", "alarm_signal")


def _inputs_digest(channels) -> str:
    """Identity of a trial's inputs: export digests, parser rules, data choices."""
    import hashlib

    from . import telegram

    pinned = json.loads(config.MANIFEST_PATH.read_text()).get("telegram", {})
    parts = [f"{c}:{pinned[c]['sha256']}" for c in channels] + [telegram.parser_digest(), str(config.HISTORY_START_UTC),
             f"{config.ZONE_LOOKBACK_DAYS}/{config.ZONE_MIN_SHARE}", json.dumps(backtest.selected_params(), sort_keys=True)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def step_text(only=None) -> dict:
    """Which channels feed the text features: none, all four, or all but one.
    History start as chosen in the history step (config). A trial whose inputs
    are unchanged since it was scored is kept, not rerun."""
    options = [("none", ()), ("all", ALL_CHANNELS)] + [(f"all-{c}", tuple(x for x in ALL_CHANNELS if x != c)) for c in ALL_CHANNELS]
    out = load()
    done = {t["option"]: t for t in out.get("text", {}).get("trials", [])}
    trials = []
    for name, chans in options:
        if only is not None and name not in only and name not in done:
            continue
        dig = _inputs_digest(chans)
        if name in done and done[name].get("inputs") == dig:
            trials.append(done[name])
            continue
        if only is not None and name not in only:
            continue
        s = score(_data(_posts(chans)))
        s.update(option=name, channels=list(chans), inputs=dig)
        print("text", name, round(s["score"], 4), s["phase1_rel_mae"], s["phase2_rel_logloss"], flush=True)
        trials.append(s)
        out = load()
        out["text"] = {"trials": trials}
        save(out)
    out = load()
    res = {"trials": trials}
    if len(trials) == len(options):
        res.update(pick(trials))
        res["selected_channels"] = next(t["channels"] for t in trials if t["option"] == res["selected"])
    out["text"] = res
    save(out)
    return res


def step_edited() -> dict:
    """Not a choice: what dropping edited posts changes on the validation window,
    with the chosen channels. The flag is never a feature."""
    from . import telegram

    chans = config.TEXT_CHANNELS
    res = {"channels": list(chans), "dropped_posts": {}, "trials": []}
    for c in chans:
        df = telegram.load_channel(c)
        res["dropped_posts"][c] = int(df["edited"].sum())
    for drop in (False, True):
        s = score(_data(_posts(chans, drop_edited=drop)))
        s["option"] = "without edited posts" if drop else "all posts"
        print("edited", s["option"], round(s["score"], 4), flush=True)
        res["trials"].append(s)
    out = load()
    out["edited"] = res
    save(out)
    return res
