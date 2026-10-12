"""Gates: the backtest report covers what it claims, without look-ahead, for
the 24 regions and for the zones of regular alerts.

Usage: verify_backtest.py phase1|phase2
"""

import json
import sys

import numpy as np
import pandas as pd

from alerts import config


def last_complete_day_from_raw() -> str:
    """Independent of the package: the latest timestamp in the raw archive."""
    raw = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False)
    ts = pd.concat([pd.to_datetime(raw["started_at"], utc=True, format="ISO8601"),
                    pd.to_datetime(raw["finished_at"].replace("", None), utc=True, format="ISO8601")]).max()
    return str((ts.tz_convert(config.KYIV_TZ).normalize() - pd.Timedelta(days=1)).date())


def valid_days(first: str, last: str) -> int:
    days = pd.date_range(first, last).strftime("%Y-%m-%d")
    return int((~days.isin(config.EXCLUDED_DAYS)).sum())


def finite(x) -> bool:
    return x is not None and np.isfinite(np.asarray(x, float)).all()


def zone_days(first: str, last: str) -> int:
    """Zone-days in the window, from the zone membership (not from the backtest)."""
    from alerts import stitch
    from alerts import zones as Z

    a = Z.activity(Z.unit_alerts(stitch.load_all()), "2022-03-16", last)
    m = Z.membership(a, config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE)
    reg = np.array([k[0] for k in a.keys])
    days = a.days["day"].to_numpy()
    cols = np.nonzero((days >= first) & (days <= last) & a.day_ok)[0]
    return int(sum(len(set(reg[m[:, i]])) for i in cols))


def check(phase: str) -> list[str]:
    m = json.loads((config.REPORTS_DIR / f"metrics_{phase}.json").read_text())
    err = check_windows(phase, m["windows"], "regions")
    if "zones" not in m:
        return err + ["no zone results"]
    return err + check_windows(phase, m["zones"]["windows"], "zones")


def check_windows(phase: str, w: dict, kind: str) -> list[str]:
    err = []
    last = last_complete_day_from_raw()
    if set(w) != {"pre_break", "post_break"}:
        return [f"windows {sorted(w)}"]
    if [w["pre_break"]["first_day"], w["pre_break"]["last_day"]] != list(config.PRE_BREAK_WINDOW):
        err.append("pre_break window differs from config")
    if w["post_break"]["first_day"] != config.POST_BREAK_START or w["post_break"]["last_day"] != last:
        err.append(f"post_break ends {w['post_break']['last_day']}, last complete day in raw data is {last}")
    print(f"[{kind}] last complete day (raw): {last}")
    for name, x in w.items():
        if not x["refits"]:
            err.append(f"{name}: no refits")
        for r in x["refits"]:
            if not (r["train_known_until"] <= r["refit"] <= r["first_forecast"]):
                err.append(f"{name}: refit {r['refit']} trains on labels known at {r['train_known_until']} or forecasts before it")
        if phase == "phase1":
            exp = 24 * valid_days(x["first_day"], x["last_day"]) if kind == "regions" else zone_days(x["first_day"], x["last_day"])
            if x["n_rows"] != exp:
                err.append(f"{kind}/{name}: {x['n_rows']} rows, expected {exp} ({'24 regions' if kind == 'regions' else 'zone membership'} x valid days)")
            for tgt in ("count", "hours"):
                meth = x["targets"][tgt]["methods"]
                bases = [k for k in meth if k != "model"]
                if "model" not in meth or len(bases) < 2:
                    err.append(f"{name}/{tgt}: model and >=2 baselines required")
                for k, v in meth.items():
                    if not finite(v["mae"]) or not finite(v["rmse"]):
                        err.append(f"{name}/{tgt}/{k}: non-finite metric")
                    if k != "model" and (len(v.get("model_skill_ci90", [])) != 2 or not finite(v["model_skill_ci90"])):
                        err.append(f"{name}/{tgt}/{k}: missing bootstrap interval")
                print(f"{name} {tgt}: rows {x['n_rows']}, model MAE {meth['model']['mae']:.3f}, best baseline {x['targets'][tgt]['best_baseline']} {meth[x['targets'][tgt]['best_baseline']]['mae']:.3f}")
        else:
            hz = x["horizons"]
            if sorted(hz, key=int) != [str(h) for h in config.HORIZONS]:
                err.append(f"{name}: horizons {sorted(hz)}")
            if x["ongoing_samples_in_window"] <= 0:
                err.append(f"{name}: no 'alert in progress' moments counted")
            for h, v in hz.items():
                for k in ("model", "clim", "poisson"):
                    mm = v["methods"].get(k)
                    one_class = v["base_rate"] in (0.0, 1.0)
                    if not mm or not all(finite(mm[q]) for q in ("brier", "log_loss")) or not (finite(mm["auc"]) or (one_class and mm["auc"] is None)):
                        err.append(f"{name}/{h}h/{k}: metrics missing")
                for k in ("clim", "poisson"):
                    if not finite(v["methods"][k].get("model_bss_ci90")):
                        err.append(f"{name}/{h}h: missing BSS interval vs {k}")
                rel = v["reliability"]
                if not rel or sum(b["n"] for b in rel) != v["n"]:
                    err.append(f"{name}/{h}h: reliability table does not cover all samples")
            # Forecasts exist only for moments without an ongoing alert, and are monotone in the horizon.
            q = pd.read_csv(config.ARTIFACTS_DIR / f"phase2_{'' if kind == 'regions' else 'zone_'}{name}.csv.gz")
            ys = q[[f"y{h}" for h in config.HORIZONS]]
            if ys.isna().all(axis=1).any():
                err.append(f"{name}: forecast rows without any usable label")
            p = q[[f"p_model_{h}" for h in config.HORIZONS]].to_numpy()
            if (np.diff(p, axis=1) < -1e-12).any():
                err.append(f"{name}: probabilities decrease with the horizon")
            print(f"{name}: {len(q)} forecast moments, {x['ongoing_samples_in_window']} 'alert in progress' moments, BSS 1h vs climatology {hz['1']['methods']['clim']['model_bss']:.3f}")
    return err


def main() -> int:
    phase = sys.argv[1]
    err = check(phase)
    if err:
        print("\n".join(err))
        return 1
    print(f"{phase.upper()} BACKTEST VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
