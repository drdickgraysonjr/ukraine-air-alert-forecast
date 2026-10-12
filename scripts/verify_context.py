"""Gate: types taken from context use earlier posts only, and summaries feed their own
feature instead of the live threat counts.

Causality: parsing a channel cut at time t must give the same labels for every post up
to t as parsing the whole channel. A control parser that borrows the type of the NEXT
typed post must fail the same test. Window: every borrowed type comes from a post at
most CONTEXT_WINDOW_SEC earlier.
Summaries: on a tiny grid, a summary post adds nothing to tg_threat_* and counts in
tg_summary_24h; the same post not flagged as a summary does the opposite.
"""

import sys

import numpy as np
import pandas as pd

from alerts import telegram as TG

CHANNEL = "war_monitor"
CUTS = 12


def lookahead_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Control: an untyped post borrows the threat types of the next typed post."""
    out = TG.parse_frame(df.assign(ts=-df["ts"]))  # time reversed: "previous" becomes "next"
    return out.assign(ts=df["ts"].to_numpy())


def causal_violations(df: pd.DataFrame, parse) -> int:
    full = parse(df).set_index("id")
    rng = np.random.default_rng(7)
    ts = np.sort(df["ts"].to_numpy())
    bad = 0
    for t in rng.choice(ts[len(ts) // 10:], CUTS, replace=False):
        cut = parse(df[df["ts"] <= t]).set_index("id")
        a = cut["types"]
        b = full.loc[cut.index, "types"]
        bad += int((a != b).sum())
    return bad


def main() -> int:
    err = []
    raw = TG.load_channel(CHANNEL)[["id", "ts", "text"]]
    raw = raw[raw["ts"] >= int(pd.Timestamp("2025-06-01", tz="UTC").timestamp())].reset_index(drop=True)
    v = causal_violations(raw, TG.parse_frame)
    c = causal_violations(raw, lookahead_frame)
    if v:
        err.append(f"parse_frame: {v} labels change when later posts are removed")
    if c == 0:
        err.append("control: the look-ahead parser was not caught")
    # window and source of every borrowed type
    p = TG.parse_frame(raw).sort_values("ts", kind="stable").reset_index(drop=True)
    typed = p[[bool(set(t) & set(TG.THREATS)) and not s and not f for t, s, f in zip(p["types"], p["summary"], p["types_from_context"])]]
    for _, r in p[p["types_from_context"]].iterrows():
        prev = typed[typed["ts"] <= r["ts"]]
        prev = prev[prev.index < _]
        if prev.empty or r["ts"] - prev["ts"].iloc[-1] > TG.CONTEXT_WINDOW_SEC:
            err.append(f"post {r['id']}: borrowed type without a typed post in the window")
            break
        if set(r["types"]) != set(prev["types"].iloc[-1]) & set(TG.THREATS):
            err.append(f"post {r['id']}: borrowed types differ from the last typed post")
            break
    n_ctx = int(p["types_from_context"].sum())

    # summaries on a tiny grid
    from alerts import features as F

    # same alert row shape as tests/test_zones_telegram.py
    a = pd.DataFrame([{"region": "sumy", "unit_level": "oblast", "unit": "", "raion": "",
                       "started_at": pd.Timestamp("2026-06-01 00:00", tz="UTC"), "finished_at": pd.Timestamp("2026-06-01 01:00", tz="UTC"),
                       "level": "single", "source": "test"}])
    t_post = int(pd.Timestamp("2026-06-05 10:30", tz="UTC").timestamp())
    res = {}
    for flag in (True, False):
        posts = TG.Posts({"x": pd.DataFrame({"ts": [t_post], "oblasts": [("sumy",)], "types": [("drone",)], "summary": [flag]})})
        g = F.build_grid(a, pd.Timestamp("2026-06-10", tz="UTC"), start=pd.Timestamp("2026-05-20", tz="UTC"), posts=posts)
        f = F.feature_matrix(g)
        k = g.regions.index("sumy")
        col = {int(h): i for i, h in enumerate(g.hours)}[int(pd.Timestamp("2026-06-05 11:00", tz="UTC").timestamp())]
        res[flag] = (f["tg_threat_1h"][k, col], f["tg_summary_24h"][k, col], f["tg_ua_summary_24h"][k, col])
    if res[True] != (0, 1, 1):
        err.append(f"summary post: threat/summary/ua-summary = {res[True]}, expected (0, 1, 1)")
    if res[False] != (1, 0, 0):
        err.append(f"live post: threat/summary/ua-summary = {res[False]}, expected (1, 0, 0)")
    if err:
        print("\n".join(err))
        return 1
    print(f"{CHANNEL} since 2025-06-01: {len(raw)} posts, {n_ctx} with types from context; {CUTS} cuts: 0 changes; look-ahead control: {c} changes")
    print(f"summary post -> threat {res[True][0]}, summary {res[True][1]}; live post -> threat {res[False][0]}, summary {res[False][1]}")
    print("CONTEXT CAUSAL; SUMMARY FEATURE VERIFIED; CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
