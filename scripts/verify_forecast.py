"""Gate G8: the CLI forecast is complete and agrees with an independently
reconstructed state of every region at the forecast moment.

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

from alerts import config  # noqa: E402
from alerts.regions import REGIONS  # noqa: E402

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
    print(f"{t}: {len(regs)} regions, {len(active)} with an alert in progress (raw), CLI agrees: {not err}")
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
