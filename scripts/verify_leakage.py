"""Gate: features at moment t use nothing published after t.

For sampled moments t (random plus edge cases) every feature of every series
(24 regions and the zones that exist at t), including the text features, is
recomputed twice and compared with the full-data features at t:
  1. from the alert table and the posts as they were known at t
     (timeline.as_of, posts with time <= t, data end = t);
  2. from data whose future was rewritten: alerts after t shuffled in time,
     finish times of alerts active at t replaced with random later times,
     posts after t moved to random later times.
Positive controls, caught by the same comparison:
  - alerts: hours until the current alert ends;
  - text: posts mentioning the region in the hour after t.
"""

import json
import sys

import numpy as np
import pandas as pd

from alerts import config, features, stitch, telegram
from alerts import timeline as T

N_RANDOM = 20


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


def scramble_posts(posts: telegram.Posts, t: int, end: int, rng) -> telegram.Posts:
    out = {}
    for c, df in posts.frames.items():
        df = df.copy()
        fut = df["ts"].to_numpy() > t
        df.loc[fut, "ts"] = t + rng.integers(1, max(end - t, 2), fut.sum())
        out[c] = df.sort_values("ts", kind="stable").reset_index(drop=True)
    return telegram.Posts(out)


def leaky(g: features.Grid) -> np.ndarray:
    """Control feature: hours until the alert active at H ends (uses the future)."""
    out = np.zeros((len(g.regions), len(g.hours)))
    for k in range(len(g.regions)):
        tl = g.timelines[g.regions[k]]
        idx = np.searchsorted(tl.s, g.hours, side="right") - 1
        has = (idx >= 0) & g.active[k]
        out[k, has] = np.minimum((tl.e[idx[has]] - g.hours[has]) / 3600, 1e6)
    return out


def leaky_text(g: features.Grid) -> np.ndarray:
    """Control feature: posts mentioning the region in [H, H + 1h) (the future)."""
    R, n = len(g.regions), len(g.hours)
    out = np.zeros((R, n))
    ridx = {r: i for i, r in enumerate(g.regions)}
    for df in g.posts.frames.values():
        b = (df["ts"].to_numpy() - g.hours[0]) // 3600
        for j, obl in zip(b, df["oblasts"]):
            if 0 <= j < n:
                for r in obl:
                    out[ridx[r], j] += 1
    return out


def at(f: dict, g: features.Grid, col: int) -> dict:
    """Feature values at a column, keyed by (series, feature), existing series only."""
    out = {}
    for k, sid in enumerate(g.series):
        if g.exists[k, col]:
            for name, a in f.items():
                out[(sid, name)] = a[k, col]
    return out


def compare(ref: dict, got: dict) -> list[str]:
    bad = sorted({sid for sid, _ in set(ref) ^ set(got)})
    bad = [f"series {s} exists in only one of the two" for s in bad]
    for key in set(ref) & set(got):
        if not np.isclose(ref[key], got[key], rtol=1e-9, atol=1e-9, equal_nan=True):
            bad.append(f"{key[0]}:{key[1]}")
    return bad


def main() -> int:
    rng = np.random.default_rng(config.SEED)
    alerts = stitch.load_all()
    pinned = sorted(json.loads(config.MANIFEST_PATH.read_text()).get("telegram", {}))
    posts = telegram.Posts.load(pinned)
    full = features.build_grid(alerts, config.DATA_END_UTC, posts=posts)
    ffull = features.feature_matrix(full)
    lfull, tfull = leaky(full), leaky_text(full)
    col_of = {int(h): i for i, h in enumerate(full.hours)}
    end = int(T.to_sec(config.DATA_END_UTC))

    lo = int(T.to_sec(pd.Timestamp("2025-01-01", tz="UTC")))
    hi = end - 24 * 3600
    cand = full.hours[(full.hours >= lo) & (full.hours <= hi)]
    picks = list(rng.choice(cand, N_RANDOM, replace=False))
    for s in ["2026-09-06 13:00", "2026-09-07 06:00", "2026-09-03 00:00", "2026-03-29 01:00",
              "2025-10-26 01:00", "2025-10-26 02:00", "2026-04-14 21:00", "2026-10-10 21:00"]:
        picks.append(int(T.to_sec(pd.Timestamp(s, tz="UTC"))))
    busy = cand[full.active[: len(full.regions), np.isin(full.hours, cand)].sum(axis=0) >= 5]
    picks += list(rng.choice(busy, 6, replace=False))

    failures, hits, thits, n_zone, n_text = [], 0, 0, 0, 0
    for h in picks:
        t = pd.Timestamp(int(h), unit="s", tz="UTC")
        col = col_of[int(h)]
        ref = at(ffull, full, col)
        n_zone += int(full.exists[full.is_zone, col].sum())
        n_text += int(sum(v != 0 for (s, k), v in ref.items() if k.startswith("tg_") and np.isfinite(v)))
        for name, tab, pst, e in (
            ("as_of", T.as_of(alerts, t), posts.as_of(int(h)), t),
            ("scrambled", scramble_future(alerts, t, rng), scramble_posts(posts, int(h), end, rng), config.DATA_END_UTC),
        ):
            g = features.build_grid(tab, e, posts=pst)
            if g.hours[col] != h:
                failures.append(f"{t} {name}: grid misaligned")
                continue
            bad = compare(ref, at(features.feature_matrix(g), g, col))
            if bad:
                failures.append(f"{t} {name}: {bad[:8]}")
            if not np.allclose(leaky(g)[:, col], lfull[:, col], equal_nan=True):
                hits += 1
            if not np.allclose(leaky_text(g)[:, col], tfull[:, col]):
                thits += 1
    print(f"moments checked: {len(picks)}; zone-moments: {n_zone}; non-zero text feature values compared: {n_text}; channels: {pinned}")
    print(f"control (alert) detected at {hits} of {2 * len(picks)} recomputations; control (text) at {thits}")
    if failures:
        print("\n".join(failures[:20]))
        return 1
    if hits == 0 or thits == 0:
        print("a control was never detected: the comparison cannot see leaks")
        return 1
    print("NO LEAKAGE; CONTROL DETECTED; TEXT CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
