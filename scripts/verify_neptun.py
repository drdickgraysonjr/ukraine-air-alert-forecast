"""Gate: the NEPTUN adapter (alerts.neptun) agrees with the pinned data. Offline:
reads data/live/neptun/pinned (written by `alerts neptun-pin`) and the pinned
alerts.in.ua CSV.

    mapping  every raion and oblast key in the pinned alerts.json and in both
             pinned GeoJSON files maps to exactly one location and region; no
             two keys share a location; every modelled raion and region has a
             key; control: an unknown key is rejected.
    history  every alert of the pinned snapshot that started before the data end
             is an alert of alerts_in_ua.csv open at the data end, same location,
             start within 1 s (or in the same minute when NEPTUN gives a
             whole-minute start); control: starts shifted by +1 h must mismatch.

Prints the marker only when every assertion passes, each failure otherwise."""

import sys
from collections import Counter

import pandas as pd

from alerts import config
from alerts import neptun as N
from alerts.regions import REGION_IDS, REGIONS
from alerts.translit import raion_key


def mapping() -> int:
    err = []
    line, man = N.load_pinned()
    keys = [("raions", e["key"], e.get("oblast")) for e in line["raions"]] + [("oblasts", e["key"], e.get("oblast")) for e in line["oblasts"]]
    keys += [("raions", k, None) for k, _ in N.geojson_keys(N.PINNED_DIR / "raions.geojson")]
    keys += [("oblasts", k, parent) for k, parent in N.geojson_keys(N.PINNED_DIR / "oblasts.geojson")]
    locs = {}
    for kind, key, parent in keys:
        try:
            loc = N.locate({"key": key, "oblast": parent}, kind)
        except N.UnmappedKey as e:
            err.append(f"{kind} {key!r}: {e}")
            continue
        if loc.region is not None and loc.region not in REGION_IDS:
            err.append(f"{kind} {key!r}: region {loc.region} is not a model region")
        if loc.region is None and loc.location_oblast not in (N.CRIMEA, N.LUHANSK):
            err.append(f"{kind} {key!r}: no region outside the excluded regions ({loc})")
        prev = locs.setdefault((kind, N._raion_key(key) if kind == "raions" else key), loc)
        if prev != loc:
            err.append(f"{kind} {key!r}: maps to {loc} and {prev}")
    by_loc = Counter(loc.location_uid for loc in locs.values())
    err += [f"location {u} has {n} NEPTUN keys" for u, n in by_loc.items() if n > 1]
    geo_raions = {loc.location_uid for (kind, _), loc in locs.items() if kind == "raions"}
    raions, _ = N._aiu_catalog()
    missing = sorted(t for (o, rk), (u, t) in raions.items() if o in N.UA_TO_ID and u not in geo_raions)
    if missing:
        err.append(f"modelled raions without a NEPTUN key: {missing}")
    n_model_raions = sum(v[3] for v in REGIONS.values())
    if len(raions) != n_model_raions:
        err.append(f"alerts.in.ua lists {len(raions)} raions, regions.py {n_model_raions}")
    obl_regions = {loc.region for (kind, _), loc in locs.items() if kind == "oblasts"}
    if obl_regions - {None} != set(REGION_IDS):
        err.append(f"oblast keys cover regions {sorted(obl_regions - {None})}, not all 24")
    try:
        N.locate_raion("атлантидський")
        err.append("control: an unknown raion key was accepted")
    except N.UnmappedKey:
        pass
    n_r = sum(1 for k in locs if k[0] == "raions")
    n_o = sum(1 for k in locs if k[0] == "oblasts")
    n_excl = sum(1 for loc in locs.values() if loc.region is None)
    print(f"keys: {len(keys)} occurrences, {n_r} distinct raions, {n_o} distinct oblast-level; "
          f"{n_excl} in the excluded regions (Crimea, Sevastopol, Luhansk); modelled raions covered "
          f"{n_model_raions - len(missing)}/{n_model_raions}; snapshot fetched {man['files']['alerts.json']['fetched_at']}")
    kyiv = N.locate_oblast("м. київ")
    print(f"Kyiv city: key 'м. київ' -> {kyiv.location_type} «{kyiv.location_title}» uid {kyiv.location_uid}, region {kyiv.region}")
    if err:
        print("\n".join(err))
        return 1
    print("NEPTUN MAPPED")
    return 0


def _open_at_end():
    raw = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False)
    raw = raw[raw["alert_type"] == "air_raid"].copy()
    raw["start"] = pd.to_datetime(raw["started_at"], utc=True, format="ISO8601")
    raw["end"] = pd.to_datetime(raw["finished_at"].replace("", None), utc=True, format="ISO8601")
    end = config.DATA_END_UTC
    return raw, raw[(raw["start"] <= end) & (raw["end"].isna() | (raw["end"] > end))]


def _same_location(rows: pd.DataFrame, loc) -> pd.Series:
    m = (rows["location_type"] == loc.location_type) & (rows["location_oblast"] == loc.location_oblast)
    if loc.location_type == "raion":
        return m & (rows["location_title"].map(raion_key) == raion_key(loc.location_title))
    return m & (rows["location_title"] == loc.location_title)


def match(entries, raw, open_rows) -> dict:
    out = {"exact": [], "minute": [], "mismatch": [], "not_in_archive": [], "level_differs": []}
    for kind, e in entries:
        loc = N.locate(e, kind)
        since = N._utc(e["since"])
        desc = f"{loc.location_title} ({loc.location_oblast}) {e['level']} since {since}"
        if not _same_location(raw, loc).any():
            out["not_in_archive"].append(desc)
            continue
        cand = open_rows[_same_location(open_rows, loc)]
        hit = [(N.start_match(since, s), lv, s) for s, lv in zip(cand["start"], cand["alert_level"])]
        hit = [h for h in hit if h[0]]
        if not hit:
            starts = ", ".join(str(s) for s in cand["start"]) or "none open"
            out["mismatch"].append(f"{desc}: alerts.in.ua open at the data end: {starts}")
            continue
        kindm, lv, s = hit[0]
        out[kindm].append(f"{desc} = alerts.in.ua {s} (diff {(s - since).total_seconds():+.3f} s)")
        if lv != e["level"]:
            out["level_differs"].append(f"{desc}: alerts.in.ua level {lv}")
    return out


def history() -> int:
    err = []
    line, _ = N.load_pinned()
    end = config.DATA_END_UTC
    raw, open_rows = _open_at_end()
    old = [(k, e) for k in ("oblasts", "raions") for e in line[k] if N._utc(e["since"]) < end]
    res = match(old, raw, open_rows)
    n_match = len(res["exact"]) + len(res["minute"])
    print(f"snapshot {line['fetched_at']}: {len(line['raions'])} raion and {len(line['oblasts'])} oblast-level alerts; "
          f"{len(old)} started before the data end {end}")
    for k in ("exact", "minute", "mismatch", "not_in_archive", "level_differs"):
        print(f"  {k}: {len(res[k])}")
        for d in res[k]:
            print(f"    {d}")
    old_locs = {N.locate(e, k).location_oblast for k, e in old}
    if old and all(k == "oblasts" for k, _ in old) and old_locs <= {N.CRIMEA, N.LUHANSK}:
        print("  note: the snapshot has no alert older than the data end other than the oblast-level "
              "Crimea / Sevastopol / Luhansk alerts (excluded regions, never modelled)")
    if res["minute"]:
        print("  note: 'minute' = NEPTUN gives a whole-minute start (no seconds) and alerts.in.ua's start lies in that minute")
    # Informative: alerts open at the data end that are not in the snapshot.
    snap_locs = []
    for k in ("oblasts", "raions"):
        for e in line[k]:
            snap_locs.append(N.locate(e, k))
    present = pd.Series(False, index=open_rows.index)
    for loc in snap_locs:
        present |= _same_location(open_rows, loc)
    region_level = open_rows["location_type"].isin(["oblast", "raion"])
    print(f"  informative: {int((~present & region_level).sum())} of {int(region_level.sum())} oblast/raion alerts open at the data end "
          f"have no alert at the same location in the snapshot ({(N._utc(line['fetched_at']) - end).total_seconds() / 3600:.1f} h later); "
          f"{int((~region_level).sum())} hromada/city alerts open at the data end cannot be compared (NEPTUN has no hromada level)")
    if n_match < 1:
        err.append("no alert of the snapshot matches the history")
    if res["mismatch"]:
        err.append(f"{len(res['mismatch'])} mismatches")
    # Control: starts shifted by an hour must not match.
    shifted = [(k, dict(e, since=(N._utc(e["since"]) + pd.Timedelta(hours=1)).isoformat())) for k, e in old]
    ctl = match(shifted, raw, open_rows)
    print(f"  control (+1 h): {len(ctl['exact']) + len(ctl['minute'])} matches, {len(ctl['mismatch'])} mismatches")
    if not ctl["mismatch"] or ctl["exact"] or ctl["minute"]:
        err.append("control: shifted starts were not detected")
    if err:
        print("\n".join(err))
        return 1
    print("NEPTUN MATCHES HISTORY; CONTROL DETECTED")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("mapping", "history"):
        print("usage: verify_neptun.py {mapping,history}")
        return 2
    return mapping() if sys.argv[1] == "mapping" else history()


if __name__ == "__main__":
    sys.exit(main())
