"""What edited Telegram posts and the text features change on the test
windows (reports/edited_ablation.json). Neither run is used to choose anything.

Edited posts are kept in the model: the export has only their final text and
no time of the edit. One run repeats the test-window backtest without them,
another without any text features; everything else is fixed.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from . import config, datasets, run_backtest, telegram


def headline(m1: dict, m2: dict) -> dict:
    out = {}
    for kind, w1, w2 in (("regions", m1["windows"], m2["windows"]), ("zones", m1["zones"]["windows"], m2["zones"]["windows"])):
        for name in w1:
            p1 = w1[name]["targets"]
            p2 = w2[name]["horizons"]
            out[f"{kind}/{name}"] = {
                "phase1_mae": {t: p1[t]["methods"]["model"]["mae"] for t in p1},
                "phase1_skill_vs_best_naive": {t: p1[t]["methods"][p1[t]["best_baseline"]]["model_skill"] for t in p1},
                "phase2_brier": {h: p2[h]["methods"]["model"]["brier"] for h in p2},
                "phase2_bss_vs_climatology": {h: p2[h]["methods"]["clim"]["model_bss"] for h in p2},
            }
    return out


def _variant(drop_edited: bool, channels) -> dict:
    keep = (config.REPORTS_DIR, config.ARTIFACTS_DIR, config.DROP_EDITED_POSTS, config.TEXT_CHANNELS)
    with tempfile.TemporaryDirectory() as rep, tempfile.TemporaryDirectory() as art:
        shutil.copy(keep[0] / "tuning.json", Path(rep) / "tuning.json")
        try:
            config.REPORTS_DIR, config.ARTIFACTS_DIR = Path(rep), Path(art)
            config.DROP_EDITED_POSTS, config.TEXT_CHANNELS = drop_edited, tuple(channels)
            run_backtest.run(datasets.load_default())
            return headline(json.loads((Path(rep) / "metrics_phase1.json").read_text()),
                            json.loads((Path(rep) / "metrics_phase2.json").read_text()))
        finally:
            config.REPORTS_DIR, config.ARTIFACTS_DIR, config.DROP_EDITED_POSTS, config.TEXT_CHANNELS = keep


def run() -> dict:
    dropped = {c: int(telegram.load_channel(c)["edited"].sum()) for c in config.TEXT_CHANNELS}
    without_edited = _variant(True, config.TEXT_CHANNELS)
    without_text = _variant(False, ())
    # Read last, so the main backtest may run alongside.
    with_ = headline(json.loads((config.REPORTS_DIR / "metrics_phase1.json").read_text()),
                     json.loads((config.REPORTS_DIR / "metrics_phase2.json").read_text()))
    out = {
        "parser_digest": telegram.parser_digest(),
        "channels": list(config.TEXT_CHANNELS),
        "dropped_posts": dropped,
        "with_edited": with_,
        "without_edited": without_edited,
        "without_text": without_text,
    }
    (config.REPORTS_DIR / "edited_ablation.json").write_text(json.dumps(out, indent=2) + "\n")
    return out
