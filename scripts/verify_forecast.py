"""Gate: the CLI forecast is complete and agrees with an independently
reconstructed state of every region and zone at the forecast moment.

Zones: the CLI's member hromadas must be the zone membership of the day computed
from the alert table as known at the day's midnight, and "alert in progress"
must match the raw rows (a member hromada's own alert, its raion's or the
oblast's) active at the moment. Text features come only from the configured
channels' posts published by the moment (the reader is shared; the leakage
gate checks the timing).

Two moments are checked: the busiest hour of the last complete week (many
alerts in progress, so the 'alert in progress' branch is exercised) and the
quietest one (probabilities for every region)."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_stitch import raw_frames  # noqa: E402

from alerts import config, stitch  # noqa: E402
from alerts import timeline as T  # noqa: E402
from alerts import zones as Z  # noqa: E402
from alerts.regions import REGIONS  # noqa: E402
from alerts.translit import raion_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def raw_active(off, aiu, t):
    """Region ids with an oblast/raion alert in progress at t, from raw CSVs."""
    o = off[(off["start"] < config.CUTOVER_UTC) & (off["start"] <= t) & (off["end"].isna() | (off["end"] > t))]
    a = aiu[(aiu["start"] >= config.CUTOVER_UTC) & (aiu["start"] <= t) & (aiu["end"].isna() | (aiu["end"] > t))]
    en = {v[0]: k for k, v in REGIONS.items()}
    uk = {v[1]: k for k, v in REGIONS.items()}
    return set(o["oblast"].map(en).dropna()) | set(a["location_oblast"].map(uk).dropna())


def pick_moments(off, aiu):
    end = config.DATA_END_UTC.floor("h")
    hours = pd.date_range(end - pd.Timedelta(days=7), end - pd.Timedelta(days=1), freq="h")
    counts = np.array([len(raw_active(off, aiu, t)) for t in hours])
    return hours[int(counts.argmax())], hours[int(counts.argmin())]


def raw_hromada():
    off = pd.read_csv(config.OFFICIAL.local_path, dtype=str, keep_default_na=False).drop_duplicates()
    off = off[off["level"] == "hromada"]
    aiu = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False)
    aiu = aiu[(aiu["alert_type"] == "air_raid") & (aiu["location_type"] == "hromada")]
    en = {v[0]: k for k, v in REGIONS.items()}
    uk = {v[1]: k for k, v in REGIONS.items()}
    a = pd.DataFrame({"region": off["oblast"].map(en), "raion": off["raion"].map(raion_key), "unit": off["hromada"].map(Z.hromada_key),
                      "start": pd.to_datetime(off["started_at"], utc=True), "end": pd.to_datetime(off["finished_at"].replace("", None), utc=True), "src": "official"})
    b = pd.DataFrame({"region": aiu["location_oblast"].map(uk), "raion": aiu["location_raion"].map(raion_key), "unit": aiu["location_title"].map(Z.hromada_key),
                      "start": pd.to_datetime(aiu["started_at"], utc=True), "end": pd.to_datetime(aiu["finished_at"].replace("", None), utc=True), "src": "alerts_in_ua"})
    return pd.concat([a, b], ignore_index=True)


def zone_active(rid, members, t, off, aiu, hrom) -> bool:
    keys = {tuple(m.split("/")) for m in members}
    raions = {k[0] for k in keys}
    src_ok = lambda df: (df["start"] < config.CUTOVER_UTC) if df is off else (df["start"] >= config.CUTOVER_UTC)  # noqa: E731
    on = lambda df: (df["start"] <= t) & (df["end"].isna() | (df["end"] > t))  # noqa: E731
    en = {v[0]: k for k, v in REGIONS.items()}
    uk = {v[1]: k for k, v in REGIONS.items()}
    o = off[src_ok(off) & on(off) & (off["oblast"].map(en) == rid)]
    a = aiu[src_ok(aiu) & on(aiu) & (aiu["location_oblast"].map(uk) == rid)]
    o_hit = ((o["level"] == "oblast") | ((o["level"] == "raion") & o["raion"].map(raion_key).isin(raions))).any()
    a_raion = a["location_title"].where(a["location_type"] == "raion", "")
    a_hit = ((a["location_type"] == "oblast") | ((a["location_type"] == "raion") & a_raion.map(lambda x: raion_key(x) if x else "").isin(raions))).any()
    h = hrom[(hrom["region"] == rid) & on(hrom) & (((hrom["src"] == "official") & (hrom["start"] < config.CUTOVER_UTC)) | ((hrom["src"] == "alerts_in_ua") & (hrom["start"] >= config.CUTOVER_UTC)))]
    h_hit = any((r, u) in keys for r, u in zip(h["raion"], h["unit"]))
    return bool(o_hit or a_hit or h_hit)


def check(t, off, aiu) -> list[str]:
    err = []
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "f.json"
        r = subprocess.run(["uv", "run", "alerts", "forecast", "--as-of", t.isoformat(), "--json", str(out)], cwd=ROOT, capture_output=True, text=True)
        if r.returncode:
            return [f"{t}: CLI failed: {r.stderr[-500:]}"]
        res = json.loads(out.read_text())
    active = raw_active(off, aiu, t)
    regs = {e["region"]: e for e in res["regions"]}
    if set(regs) != set(REGIONS):
        err.append(f"{t}: regions {len(regs)} != 24")
    if res["train"]["phase1_known_until"] > pd.Timestamp(res["phase1_issued_at_kyiv"]).value // 10**9:
        err.append(f"{t}: phase 1 trained on labels known after its issue time")
    for rid, e in regs.items():
        nxt = e["next_hours"]
        if (nxt == "alert in progress") != (rid in active):
            err.append(f"{t} {rid}: CLI says {nxt!r}, raw data says active={rid in active}")
        if isinstance(nxt, dict):
            p = np.array(list(nxt.values()))
            if len(p) != len(config.HORIZONS) or (p < 0).any() or (p > 1).any() or (np.diff(p) < 0).any():
                err.append(f"{t} {rid}: bad probabilities {p}")
        today = e["today"]
        if not (0 <= today["forecast_count"] and 0 <= today["forecast_hours"] <= 25):
            err.append(f"{t} {rid}: bad phase 1 forecast {today}")
    # Zones.
    day = t.tz_convert(config.KYIV_TZ).normalize()
    known = T.as_of(stitch.load_all(), day.tz_convert("UTC"))
    a = Z.activity(Z.unit_alerts(known), "2022-03-16", str(day.date()))
    m = Z.membership(a, config.ZONE_LOOKBACK_DAYS, config.ZONE_MIN_SHARE)[:, -1]
    want = {}
    for u in np.nonzero(m)[0]:
        want.setdefault(a.keys[u][0], []).append(a.keys[u][1])
    got = {e["region"]: e for e in res["zones"]}
    if set(got) != set(want):
        err.append(f"{t}: CLI zones {sorted(got)} != membership {sorted(want)}")
    hrom = raw_hromada()
    n_on = 0
    for rid, e in got.items():
        if sorted(e["members"]) != sorted(want.get(rid, [])):
            err.append(f"{t} {rid} zone: members {e['members']} != {sorted(want.get(rid, []))}")
        on = zone_active(rid, e["members"], t, off, aiu, hrom)
        n_on += on
        if (e["next_hours"] == "alert in progress") != on:
            err.append(f"{t} {rid} zone: CLI says {e['next_hours']!r}, raw data says active={on}")
    if res["text_channels"] != sorted(config.TEXT_CHANNELS):
        err.append(f"{t}: text channels {res['text_channels']} != config {sorted(config.TEXT_CHANNELS)}")
    print(f"{t}: {len(regs)} regions, {len(active)} with an alert in progress (raw); {len(got)} zones, {n_on} under alert (raw); CLI agrees: {not err}")
    return err


def main() -> int:
    off, aiu = raw_frames()
    busy, quiet = pick_moments(off, aiu)
    err = check(busy, off, aiu) + check(quiet, off, aiu)
    if len(raw_active(off, aiu, busy)) == 0:
        err.append("control: the busy moment has no alert in progress")
    if err:
        print("\n".join(err))
        return 1
    print("FORECAST VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
