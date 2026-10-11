"""Gate: alerts are counted without merging short gaps.

An independent sweep over the raw normalised rows (oblast and raion level)
counts, per region and Kyiv day, the starts of alerts: a row that begins
strictly after the end of everything before it starts a new alert, a row that
overlaps or touches continues the current one. The result must equal the
phase 1 count target. Control: the same sweep with a 2-minute merge window (the
MVP rule) must give different counts, so the comparison can see the difference.
"""

import sys
from collections import Counter

import numpy as np
import pandas as pd

from alerts import config, features, stitch

CONTROL_GAP = pd.Timedelta(minutes=2)


def sweep(alerts: pd.DataFrame, gap: pd.Timedelta) -> Counter:
    out: Counter = Counter()
    open_end = config.DATA_END_UTC + pd.Timedelta(days=3650)
    for region, rows in alerts.groupby("region", sort=False):
        rows = rows.sort_values("started_at", kind="stable")
        cur_end = None
        for s, f in zip(rows["started_at"], rows["finished_at"]):
            f = open_end if pd.isna(f) else f
            if cur_end is None or s - cur_end > gap:
                day = s.tz_convert(config.KYIV_TZ).strftime("%Y-%m-%d")
                out[(region, day)] += 1
                cur_end = f
            elif f > cur_end:
                cur_end = f
    return out


def main() -> int:
    if config.MERGE_GAP_SEC != 0:
        print(f"config.MERGE_GAP_SEC = {config.MERGE_GAP_SEC}, expected 0")
        return 1
    alerts = stitch.load_stitched()
    g = features.build_grid(alerts, config.DATA_END_UTC)
    first = str(config.HISTORY_START_UTC.tz_convert(config.KYIV_TZ).date())
    last = str((config.DATA_END_UTC.tz_convert(config.KYIV_TZ).normalize() - pd.Timedelta(days=1)).date())
    d = features.daily_targets(g, first, last)
    d = d[d["valid"]]
    target = {(r, day): int(c) for r, day, c in zip(d["region_id"], d["day"], d["count"])}
    keys = list(target)

    zero = sweep(alerts, pd.Timedelta(0))
    bad = [k for k in keys if zero.get(k, 0) != target[k]]
    ctrl = sweep(alerts, CONTROL_GAP)
    differ = [k for k in keys if ctrl.get(k, 0) != target[k]]
    total_t = sum(target.values())
    total_c = sum(ctrl.get(k, 0) for k in keys)
    print(f"region-days compared: {len(keys)}; alerts counted: {total_t}; with a 2-min merge they would be {total_c}")
    print(f"mismatches with the independent sweep: {len(bad)}; region-days where the 2-min control differs: {len(differ)}")
    if bad:
        for k in bad[:10]:
            print("  ", k, "target", target[k], "sweep", zero.get(k, 0))
        return 1
    if not differ:
        print("control not detected: merging 2-min gaps changes nothing, the check cannot see it")
        return 1
    print("COUNTING VERIFIED; CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
