"""Gate: zones of regular alerts are defined without look-ahead.

1. For sampled Kyiv days d, membership is recomputed from the alert table as it
   was known at d's midnight (timeline.as_of) and must equal the membership
   computed from the full table.
2. Control: a leaky definition that also counts day d itself (not over at the
   forecast moment) must differ from the as-of membership on some sampled day.
3. Every dropped city row of alerts.in.ua repeats a hromada alert of the same
   oblast (start and finish within 2 minutes), so nothing is lost or doubled.
4. The parameters in config equal the validation choice in reports/validation.json.
"""

import json
import sys

import numpy as np
import pandas as pd

from alerts import config, stitch
from alerts import timeline as T
from alerts import zones as Z

N_RANDOM = 25


def members_at(alerts: pd.DataFrame, day: str, leaky: bool = False) -> set[tuple[str, str]]:
    units = Z.unit_alerts(alerts)
    a = Z.activity(units, "2022-03-16", day)
    m = Z.membership(a, config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE)
    if leaky:  # shift by one day: day d's own alerts count towards d
        m = np.zeros_like(m)
        x = (a.act & a.day_ok[None, :]).astype(int)
        lb = config.ZONE_LOOKBACK_DAYS
        i = len(a.days) - 1
        share = x[:, i - lb + 1 : i + 1].sum(1) / max(a.day_ok[i - lb + 1 : i + 1].sum(), 1)
        return {(a.keys[u][0], a.keys[u][1]) for u in np.nonzero(share >= config.ZONE_MIN_SHARE)[0]}
    i = len(a.days) - 1
    return {(a.keys[u][0], a.keys[u][1]) for u in np.nonzero(m[:, i])[0]}


def main() -> int:
    err = []
    alerts = stitch.load_all()
    rng = np.random.default_rng(config.SEED)

    # 4. Parameters.
    v = json.loads((config.REPORTS_DIR / "validation.json").read_text())["zones"]["selected"]
    if (v["lookback_days"], v["min_share"]) != (config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE):
        err.append(f"config zone parameters {config.ZONE_LOOKBACK_DAYS}/{config.ZONE_MIN_SHARE} != validation choice {v}")

    # 1-2. As-of membership on sampled days.
    days = pd.date_range("2023-02-01", config.DATA_END_UTC.tz_convert(config.KYIV_TZ).date() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    picks = list(rng.choice(days, N_RANDOM, replace=False)) + ["2026-09-06", "2026-09-07", "2026-09-08", "2026-03-29", "2025-10-26", "2026-04-15"]
    full_units = Z.unit_alerts(alerts)
    full = Z.activity(full_units, "2022-03-16", str(days[-1]))
    fm = Z.membership(full, config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE)
    dlist = list(full.days["day"])
    leak_hits, n_members = 0, 0
    for d in picks:
        m0 = pd.Timestamp(d).tz_localize(config.KYIV_TZ).tz_convert("UTC")
        known = T.as_of(alerts, m0)
        got = members_at(known, d)
        i = dlist.index(d)
        want = {(full.keys[u][0], full.keys[u][1]) for u in np.nonzero(fm[:, i])[0]}
        n_members += len(want)
        if got != want:
            err.append(f"{d}: as-of members {sorted(got)} != full {sorted(want)}")
        if members_at(alerts, d, leaky=True) != got:
            leak_hits += 1
    print(f"days checked: {len(picks)}; zone memberships on them: {n_members}; leaky control differs on {leak_hits} days")
    if leak_hits == 0:
        err.append("control: the leaky definition never differs, the check cannot see a leak")

    # 3. City rows.
    c = alerts[alerts["unit_level"] == "city"].sort_values("started_at")
    h = alerts[alerts["unit_level"] == "hromada"].sort_values("started_at")[["region", "started_at", "finished_at"]]
    m = pd.merge_asof(c, h.rename(columns={"started_at": "hs", "finished_at": "hf"}), left_on="started_at", right_on="hs",
                      by="region", tolerance=pd.Timedelta(seconds=Z.CITY_DUP_SEC), direction="nearest")
    dropped = m["hs"].notna()
    fin_ok = (m.loc[dropped, "finished_at"] - m.loc[dropped, "hf"]).abs() <= pd.Timedelta(seconds=Z.CITY_DUP_SEC)
    fin_ok |= m.loc[dropped, "finished_at"].isna() & m.loc[dropped, "hf"].isna()
    units = Z.unit_alerts(alerts)
    kept = int(units["unit_key"].str.startswith("city/").sum())
    print(f"city rows: {len(c)}; dropped as repeats of a hromada alert: {int(dropped.sum())} (finish also within 2 min: {int(fin_ok.sum())}); kept as own units: {kept}")
    if int(dropped.sum()) + kept != len(c):
        err.append("city rows neither dropped nor kept")
    if not fin_ok.all():
        err.append(f"{int((~fin_ok).sum())} dropped city rows end differently from their hromada twin")

    # Sanity: the known regular hromadas are in the zone on the validation window.
    vi = [dlist.index(x) for x in pd.date_range(*config.VALIDATION_WINDOW).strftime("%Y-%m-%d")]
    for key in [("dnipropetrovsk", "nikopolskyi/nikopolska"), ("kharkiv", "kharkivskyi/kharkivska")]:
        u = full.keys.index(next(k for k in full.keys if k[:2] == key))
        share = fm[u, vi].mean()
        print(f"{key}: member on {share:.0%} of validation days")
        if share < 0.9:
            err.append(f"{key} is a member on only {share:.0%} of validation days")
    if err:
        print("\n".join(err))
        return 1
    print("ZONES VERIFIED; CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
