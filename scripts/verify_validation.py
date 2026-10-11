"""Gate: every model choice was made on the validation window only, by the
criterion named before the trials, and the configuration follows it.

reports/validation.json holds the trials of each decision (zones, history,
text); reports/tuning.json those of the hyper-parameters. For each: every trial
scored only validation days, the selected option is the best by the stated
criterion (recomputed here), and config / the backtest use that option.
The hyper-parameters were tuned under the final data choices.
"""

import json
import sys

import pandas as pd

from alerts import config


def main() -> int:
    err = []
    v = json.loads((config.REPORTS_DIR / "validation.json").read_text())
    first, last = config.VALIDATION_WINDOW
    if v.get("validation_window") != [first, last]:
        err.append(f"validation window {v.get('validation_window')} != config")
    last_m1 = int(pd.Timestamp(last).tz_localize(config.KYIV_TZ).tz_convert("UTC").timestamp()) + 86400 + 3600

    # Zones: highest F1, ties to the longer lookback and the higher share.
    z = v["zones"]
    if not (first <= z["days"][0] and z["days"][1] <= str((pd.Timestamp(last) - pd.Timedelta(days=6)).date())):
        err.append(f"zones scored on {z['days']}, outside the validation window (with 7 days of look-forward)")
    best = max(z["trials"], key=lambda t: (round(t["f1"], 12), t["lookback_days"], t["min_share"]))
    sel = {"lookback_days": best["lookback_days"], "min_share": best["min_share"]}
    if z["selected"] != sel or (config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE) != (sel["lookback_days"], sel["min_share"]):
        err.append(f"zones: selected {z['selected']}, best {sel}, config {config.ZONE_LOOKBACK_DAYS}/{config.ZONE_MIN_SHARE}")
    print(f"zones: {len(z['trials'])} trials, best {sel} F1 {best['f1']:.4f}")

    def scored(step):
        s = v[step]
        for t in s["trials"]:
            if t["first_scored_day"] < first or t["last_scored_day"] > last:
                err.append(f"{step}/{t['option']}: scored {t['first_scored_day']}..{t['last_scored_day']}, outside the validation window")
            if t["train_known_until_max"] > last_m1:
                err.append(f"{step}/{t['option']}: trained on labels known after the validation window")
        b = min(s["trials"], key=lambda t: t["score"])
        if s["selected"] != b["option"]:
            err.append(f"{step}: selected {s['selected']} but the best score is {b['option']}")
        print(f"{step}: " + "; ".join(f"{t['option']} {t['score']:.4f}" for t in s["trials"]) + f" -> {s['selected']}")
        return b

    b = scored("history")
    if str(config.HISTORY_START_UTC.tz_convert(config.KYIV_TZ).date()) != b["option"]:
        err.append(f"history start in config {config.HISTORY_START_UTC} != selected {b['option']}")
    b = scored("text")
    if list(config.TEXT_CHANNELS) != b["channels"]:
        err.append(f"text channels in config {config.TEXT_CHANNELS} != selected {b['channels']}")

    # Hyper-parameters.
    t = json.loads((config.REPORTS_DIR / "tuning.json").read_text())
    if t["validation_window"] != [first, last]:
        err.append("tuning: other window")
    b1 = min(t["phase1_trials"], key=lambda x: x["relative_mae"])
    b2 = min(t["phase2_trials"], key=lambda x: x["mean_log_loss"])
    if (t["selected"]["phase1"]["params"], t["selected"]["phase1"]["train_days"]) != (b1["params"], b1["train_days"]):
        err.append("tuning: phase 1 selection is not the best trial")
    if (t["selected"]["phase2"]["params"], t["selected"]["phase2"]["train_days"]) != (b2["params"], b2["train_days"]):
        err.append("tuning: phase 2 selection is not the best trial")
    snap = t.get("config", {})
    want = {"history_start_utc": str(config.HISTORY_START_UTC), "zone_lookback_days": config.ZONE_LOOKBACK_DAYS,
            "zone_min_share": config.ZONE_MIN_SHARE, "text_channels": list(config.TEXT_CHANNELS), "merge_gap_sec": config.MERGE_GAP_SEC}
    for k, w in want.items():
        if snap.get(k) != w:
            err.append(f"tuning was run with {k} = {snap.get(k)!r}, config has {w!r}")
    m = json.loads((config.REPORTS_DIR / "metrics_phase1.json").read_text())["meta"]
    if snap.get("features") != m["features"]:
        err.append("tuning feature set differs from the backtest's")
    if (m["params"], m["train_days"]) != (t["selected"]["phase1"]["params"], t["selected"]["phase1"]["train_days"]):
        err.append("backtest phase 1 does not use the tuned parameters")
    print(f"tuning: {len(t['phase1_trials'])} + {len(t['phase2_trials'])} trials under {want}")
    if err:
        print("\n".join(err))
        return 1
    print("VALIDATION CHOICES CONSISTENT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
