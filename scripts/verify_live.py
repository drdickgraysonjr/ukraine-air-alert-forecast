"""Gate: the live forecast (alerts.forecast.run_live) is complete and agrees with
the latest NEPTUN snapshot. Offline: uses the snapshots recorded in
data/live/neptun when there are at least two, otherwise the pinned snapshot
twice (the second copy one minute later), and the recorded messages (else the
pinned ones).

Checks: 24 regions; "alert in progress" exactly for the regions with a NEPTUN
alert in the latest snapshot and the state (part / whole region) recomputed
from that snapshot; phase 1 forecasts and phase 2 probabilities well-formed;
warnings name the gap and every text channel missing from the feed; the
attribution links https://neptun.in.ua/ and the disclaimer is present; the
recorder refuses a poll interval under MIN_POLL_SEC. Control: the state check
flags a forecast with one region's state flipped."""

import copy
import json
import sys
import tempfile

import numpy as np
import pandas as pd

from alerts import config, forecast, neptun as N, telegram
from alerts.regions import REGIONS


def snapshot_input():
    lines = N.load_snapshots()
    msgs, fetches = N.load_messages()
    if len(lines) >= 2:
        src = f"{len(lines)} recorded snapshots"
    else:
        line, man = N.load_pinned()
        second = dict(line, fetched_at=(N._utc(line["fetched_at"]) + pd.Timedelta(minutes=1)).isoformat())
        lines = [line, second]
        src = "the pinned snapshot twice"
    if not msgs:
        _, man = N.load_pinned()
        fa = man["files"]["messages.json"]["fetched_at"]
        msgs = [dict(m, fetched_at=fa) for m in json.loads((N.PINNED_DIR / "messages.json").read_text())["messages"]]
        fetches = []
        src += ", pinned messages"
    else:
        src += f", {len(msgs)} recorded messages"
    return lines, msgs, fetches, src


def expected_state(line) -> dict:
    """Region -> 'whole region' | 'part of region' from one snapshot, by hand."""
    raions, whole = {}, set()
    for kind in ("oblasts", "raions"):
        for e in line[kind]:
            loc = N.locate(e, kind)
            if loc.region is None:
                continue
            if kind == "oblasts":
                whole.add(loc.region)
            else:
                raions.setdefault(loc.region, set()).add(loc.location_uid)
    out = {}
    for r, (_, _, _, n) in REGIONS.items():
        if r in whole or (n and len(raions.get(r, ())) >= n):
            out[r] = "whole region"
        elif r in raions:
            out[r] = "part of region"
    return out


def check_state(res, line) -> list[str]:
    err = []
    want = expected_state(line)
    regs = {e["region"]: e for e in res["regions"]}
    if set(regs) != set(REGIONS):
        err.append(f"regions: {len(regs)} != 24")
    for rid, e in regs.items():
        on = rid in want
        if (e["next_hours"] == "alert in progress") != on:
            err.append(f"{rid}: forecast says {e['next_hours']!r}, latest snapshot says alert={on}")
        if e["state_now"] != want.get(rid, "none"):
            err.append(f"{rid}: state {e['state_now']!r}, latest snapshot {want.get(rid, 'none')!r}")
    return err


def check_values(res) -> list[str]:
    err = []
    for e in res["regions"]:
        t = e["today"]
        if not (0 <= t["forecast_count"] and 0 <= t["forecast_hours"] <= 25 and t["observed_count_so_far"] >= 0
                and 0 <= t["observed_hours_so_far"] <= 25):
            err.append(f"{e['region']}: bad phase 1 values {t}")
        nxt = e["next_hours"]
        if isinstance(nxt, dict):
            p = np.array(list(nxt.values()))
            if len(p) != len(config.HORIZONS) or (p < 0).any() or (p > 1).any() or (np.diff(p) < 0).any():
                err.append(f"{e['region']}: bad probabilities {p}")
        elif nxt != "alert in progress" or not e.get("alert_elapsed_h", -1) >= 0:
            err.append(f"{e['region']}: bad phase 2 output {nxt!r} / {e.get('alert_elapsed_h')}")
    return err


def main() -> int:
    err = []
    lines, msgs, fetches, src = snapshot_input()
    latest = max(lines, key=lambda x: N._utc(x["fetched_at"]))
    res = forecast.run_live(snapshot_lines=lines, messages=msgs, fetches=fetches)
    n_on = sum(e["next_hours"] == "alert in progress" for e in res["regions"])
    print(f"input: {src}; latest snapshot {latest['fetched_at']}; {len(res['regions'])} regions, {n_on} with an alert in progress")
    err += check_state(res, latest)
    err += check_values(res)
    if res["snapshot_utc"] != str(N._utc(latest["fetched_at"])) or res["data_end_utc"] != res["snapshot_utc"]:
        err.append(f"data end {res['data_end_utc']} is not the latest snapshot {latest['fetched_at']}")
    if res["zones"]:
        err.append("zones in live output")

    # Warnings: the gap and every configured text channel absent from the feed.
    w = "\n".join(res["warnings"])
    gap_h = (N._utc(min(x["fetched_at"] for x in lines)) - config.DATA_END_UTC).total_seconds() / 3600
    if f"Gap of {gap_h:.1f} h" not in w:
        err.append(f"warnings do not state the {gap_h:.1f} h gap")
    in_feed = {N.channel_key(m["channel"]) for m in msgs}
    missing = [c for c in config.TEXT_CHANNELS if c not in in_feed]
    for c in missing:
        if c not in w or "missing from the NEPTUN feed" not in w:
            err.append(f"warnings do not name the missing channel {c}")
    if "zones" not in w.lower():
        err.append("warnings do not explain the dropped zones")
    print(f"text channels {list(config.TEXT_CHANNELS)}; missing from the feed: {missing or 'none'} "
          f"(feed handles of the repo channels: {sorted({c.handle for k, c in telegram.CHANNELS.items() if k in in_feed})})")

    # Attribution and disclaimer (also in the rendered text).
    text = forecast.render_live(res)
    if "https://neptun.in.ua/" not in res["attribution"] or "https://neptun.in.ua/" not in text:
        err.append("attribution without https://neptun.in.ua/")
    if "not an official warning system" not in res["disclaimer"] or res["disclaimer"] not in text:
        err.append("disclaimer missing")

    # Polling limits.
    if N.MIN_POLL_SEC < 5:
        err.append(f"MIN_POLL_SEC = {N.MIN_POLL_SEC} < 5")
    try:
        with tempfile.TemporaryDirectory() as d:
            N.record(interval=4, hours=0.001, out_dir=d)
        err.append("record(interval=4) did not raise")
    except ValueError:
        pass

    # Control: a flipped region must be caught by the state check.
    bad = copy.deepcopy(res)
    e = bad["regions"][0]
    e["next_hours"] = {f"{h}h": 0.5 for h in config.HORIZONS} if e["next_hours"] == "alert in progress" else "alert in progress"
    if not check_state(bad, latest):
        err.append("control: a flipped region was not detected")
    print("warnings:")
    for x in res["warnings"]:
        print(f"  - {x}")
    if err:
        print("\n".join(err))
        return 1
    print("LIVE FORECAST VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
