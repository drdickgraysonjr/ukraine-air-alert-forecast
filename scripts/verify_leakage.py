"""Gate G4: features at moment t use nothing published after t.

For sampled moments t (random plus edge cases) every feature of every region is
recomputed twice and compared with the full-data features at t:
  1. from the alert table as it was known at t (timeline.as_of, data end = t);
  2. from a table whose future was rewritten: alerts after t shuffled in time,
     finish times of alerts active at t replaced with random later times.
Positive control: a deliberately leaky feature (hours until the current alert
ends) must be caught by the same comparison.
"""

import sys

import numpy as np
import pandas as pd

from alerts import config, features, stitch
from alerts import timeline as T

N_RANDOM = 30


def scramble_future(alerts: pd.DataFrame, t: pd.Timestamp, rng) -> pd.DataFrame:
    a = alerts.copy()
    fut = a["started_at"] > t
    span = (config.DATA_END_UTC - t).total_seconds()
    shift = pd.to_timedelta(rng.integers(1, max(int(span), 2), fut.sum()), unit="s")
    dur = a.loc[fut, "finished_at"] - a.loc[fut, "started_at"]
    a.loc[fut, "started_at"] = t + shift
    a.loc[fut, "finished_at"] = a.loc[fut, "started_at"] + dur.fillna(pd.Timedelta(hours=1))
    act = (a["started_at"] <= t) & (a["finished_at"].isna() | (a["finished_at"] > t))
    a.loc[act, "finished_at"] = t + pd.to_timedelta(rng.integers(1, 48 * 3600, act.sum()), unit="s")
    return a


def leaky(g: features.Grid) -> np.ndarray:
    """Control feature: hours until the alert active at H ends (uses the future)."""
    out = np.zeros(g.active.shape)
    for k, r in enumerate(g.regions):
        tl = g.timelines[r]
        idx = np.searchsorted(tl.s, g.hours, side="right") - 1
        has = (idx >= 0) & g.active[k]
        out[k, has] = np.minimum((tl.e[idx[has]] - g.hours[has]) / 3600, 1e6)
    return out


def at_last(f: dict, col: int) -> dict:
    return {k: v[:, col] for k, v in f.items()}


def mismatches(a: dict, b: dict) -> list[str]:
    bad = []
    for k in a:
        x, y = a[k], b[k]
        same = np.isclose(x, y, rtol=1e-9, atol=1e-9, equal_nan=True)
        if not same.all():
            bad.append(k)
    return bad


def main() -> int:
    rng = np.random.default_rng(config.SEED)
    alerts = stitch.load_stitched()
    full = features.build_grid(alerts, config.DATA_END_UTC)
    ffull = features.feature_matrix(full)
    lfull = leaky(full)
    col_of = {int(h): i for i, h in enumerate(full.hours)}

    lo = int(T.to_sec(pd.Timestamp("2025-01-01", tz="UTC")))
    hi = int(T.to_sec(config.DATA_END_UTC)) - 24 * 3600
    cand = full.hours[(full.hours >= lo) & (full.hours <= hi)]
    picks = list(rng.choice(cand, N_RANDOM, replace=False))
    for s in ["2026-09-06 13:00", "2026-09-06 14:00", "2026-09-07 06:00", "2026-09-03 00:00",
              "2026-03-29 01:00", "2026-03-29 02:00", "2025-10-26 01:00", "2026-10-10 21:00"]:
        picks.append(int(T.to_sec(pd.Timestamp(s, tz="UTC"))))
    # Moments with an alert in progress somewhere are the interesting ones.
    busy = cand[full.active[:, np.isin(full.hours, cand)].sum(axis=0) >= 5]
    picks += list(rng.choice(busy, 10, replace=False))

    failures, control_hits, n_active = [], 0, 0
    for h in picks:
        t = pd.Timestamp(int(h), unit="s", tz="UTC")
        col = col_of[int(h)]
        ref = at_last(ffull, col)
        n_active += int(full.active[:, col].sum())
        for name, tab, end in (("as_of", T.as_of(alerts, t), t), ("scrambled", scramble_future(alerts, t, rng), config.DATA_END_UTC)):
            g = features.build_grid(tab, end)
            c = col_of[int(h)]
            if g.hours[c] != h:
                failures.append(f"{t} {name}: grid misaligned")
                continue
            bad = mismatches(ref, at_last(features.feature_matrix(g), c))
            if bad:
                failures.append(f"{t} {name}: {bad}")
            # Control: the leaky feature computed the same way must differ.
            if not np.allclose(leaky(g)[:, c], lfull[:, col], equal_nan=True):
                control_hits += 1
    print(f"moments checked: {len(picks)}; region-moments with an active alert: {n_active}; features per region: {len(ffull)}")
    print(f"control (leaky feature) detected at {control_hits} of {2 * len(picks)} recomputations")
    if failures:
        print("\n".join(failures[:20]))
        return 1
    if control_hits == 0:
        print("control was never detected: the comparison cannot see leaks")
        return 1
    print("NO LEAKAGE; CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
