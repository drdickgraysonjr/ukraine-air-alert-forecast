"""Gate G2: the stitched series is consistent.

Recomputes everything from the raw CSV files with plain pandas instead of the
package loaders, then compares with alerts.stitch.load_stitched().
"""

import sys

import numpy as np
import pandas as pd

from alerts import config, features, stitch
from alerts.regions import REGION_IDS, raion_count
from alerts.translit import raion_key

LEVELS = list(config.REGION_UNIT_LEVELS)


def raw_frames():
    off = pd.read_csv(config.OFFICIAL.local_path, dtype=str, keep_default_na=False).drop_duplicates()
    off = off[off["level"].isin(LEVELS) & (off["oblast"] != "Luhanska oblast")].copy()
    off["start"] = pd.to_datetime(off["started_at"], utc=True, format="ISO8601")
    off["end"] = pd.to_datetime(off["finished_at"].replace("", None), utc=True, format="ISO8601")
    aiu = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False)
    aiu = aiu[(aiu["alert_type"] == "air_raid") & aiu["location_type"].isin(LEVELS)]
    aiu = aiu[~aiu["location_oblast"].isin(["Луганська область", "Автономна Республіка Крим"])].copy()
    aiu["start"] = pd.to_datetime(aiu["started_at"], utc=True, format="ISO8601")
    aiu["end"] = pd.to_datetime(aiu["finished_at"].replace("", None), utc=True, format="ISO8601")
    return off, aiu


def minutes(df, lo, hi):
    grid = pd.date_range(lo, hi, freq="min", inclusive="left")
    v = np.zeros(len(grid), bool)
    for s, e in zip(df["start"], df["end"].fillna(hi)):
        v |= (grid >= s) & (grid < e)
    return v


def main() -> int:
    errors = []
    off, aiu = raw_frames()
    cut, end = config.CUTOVER_UTC, config.DATA_END_UTC
    expected = int((off["start"] < cut).sum() + ((aiu["start"] >= cut) & (aiu["start"] < end)).sum())
    st = stitch.load_stitched()
    print(f"rows: stitched {len(st)}, recomputed from raw {expected}")
    if len(st) != expected:
        errors.append("row count differs from raw recomputation")

    # (b) one source per alert, by start time
    pre, post = st[st["started_at"] < cut], st[st["started_at"] >= cut]
    if set(pre["source"]) != {"official"} or set(post["source"]) != {"alerts_in_ua"}:
        errors.append("source assignment around the cutover is wrong")
    key = st.assign(k=st["unit"].map(lambda u: raion_key(u) if u else ""))
    dups = key.duplicated(["region", "unit_level", "k", "started_at"]).sum()
    print(f"duplicate (region, unit, start): {dups}")
    if dups:
        errors.append("duplicated alerts in stitched table")

    # (c) excluded regions and unit levels
    if not set(st["region"]) <= set(REGION_IDS) or set(st["unit_level"]) - set(LEVELS):
        errors.append("excluded region or unit level present")

    # (d) 24 regions in both epochs
    for name, part in (("pre-cutover (last 90 days)", pre[pre["started_at"] >= cut - pd.Timedelta(days=90)]), ("post-cutover", post)):
        missing = sorted(set(REGION_IDS) - set(part["region"]))
        print(f"{name}: {part['region'].nunique()} regions")
        if missing:
            errors.append(f"{name}: no alerts for {missing}")

    # (e) raion sets agree between sources and with the 2020 reform counts
    ko = off[off["level"] == "raion"].groupby("oblast")["raion"].apply(lambda s: {raion_key(x) for x in s})
    ka = aiu[aiu["location_type"] == "raion"].groupby("location_oblast")["location_title"].apply(lambda s: {raion_key(x) for x in s})
    from alerts.regions import REGIONS

    total = 0
    for rid, (en, uk, _, n) in REGIONS.items():
        a, b = ko.get(en, set()), ka.get(uk, set())
        total += len(a)
        if a != b or len(a) != n:
            errors.append(f"{rid}: raions official={len(a)} alerts.in.ua={len(b)} expected={n} diff={sorted(a ^ b)}")
    print(f"raions matched across sources: {total} (expected {sum(raion_count(r) for r in REGION_IDS)})")

    # (f) excluded days carry no targets
    g = features.build_grid(st, end)
    d = features.daily_targets(g, "2026-08-20", "2026-09-10")
    leaked = d[d["day"].isin(config.EXCLUDED_DAYS) & d["valid"]]
    y = features.hourly_targets(g)
    in_excl = features.excluded_mask(g.hours)
    usable = sum(int(np.isfinite(v[:, in_excl]).sum()) for v in y.values())
    print(f"excluded days with valid daily targets: {len(leaked)}; usable hourly samples inside excluded days: {usable}")
    if len(leaked) or usable:
        errors.append("excluded days leak into targets")
    # official feed hole really is empty
    hole = off[(off["start"] >= pd.Timestamp("2026-08-30", tz=config.KYIV_TZ)) & (off["start"] < pd.Timestamp("2026-09-02", tz=config.KYIV_TZ))]
    print(f"official starts on 2026-08-30..09-01 (Kyiv): {len(hole)}")

    # (g) agreement on the overlap of both sources, recomputed per region
    lo, hi = cut, pd.Timestamp("2026-09-07 05:00", tz="UTC")
    rows = []
    for rid, (en, uk, _, _) in REGIONS.items():
        a = minutes(off[(off["oblast"] == en) & (off["end"] > lo) & (off["start"] < hi)], lo, hi)
        b = minutes(aiu[(aiu["location_oblast"] == uk) & (aiu["end"].fillna(hi) > lo) & (aiu["start"] < hi)], lo, hi)
        u = (a | b).sum()
        if u >= 30:
            rows.append((rid, int(a.sum()), int(b.sum()), round((a & b).sum() / u, 3)))
    seam = pd.DataFrame(rows, columns=["region", "official_min", "alerts_in_ua_min", "jaccard"])
    print(seam.to_string(index=False))
    med = float(seam["jaccard"].median())
    print(f"seam: {len(seam)} active regions, median Jaccard {med:.3f}")
    if len(seam) < 8 or med < 0.9:
        errors.append("sources disagree on the overlap window")

    if errors:
        print("\n".join(errors))
        return 1
    print("STITCH VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
