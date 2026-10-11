"""Download the pinned sources and normalise them to one alert table.

Normalised schema (one row per alert of one territorial unit):
    region       region id from alerts.regions (excluded regions dropped)
    unit_level   "oblast" | "raion" | "hromada" | "city"
    unit         unit name as given by the source ("" for oblast level)
    started_at   UTC timestamp
    finished_at  UTC timestamp, NaT while the alert is still active
    level        "red" | "yellow" | "single" (pre-2026-09 single-level system)
    source       source name
"""

from __future__ import annotations

import hashlib
import json
import urllib.request

import pandas as pd

from . import config
from .regions import EXCLUDED_ALERTS_IN_UA, EXCLUDED_OFFICIAL, OFFICIAL_TO_ID, UA_TO_ID

COLUMNS = ["region", "unit_level", "unit", "started_at", "finished_at", "level", "source"]


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(force: bool = False) -> dict:
    """Fetch every pinned source and (re)write the manifest of checksums."""
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"sources": {}}
    for src in config.SOURCES:
        if force or not src.local_path.exists():
            tmp = src.local_path.with_suffix(".part")
            with urllib.request.urlopen(src.url, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(src.local_path)
        manifest["sources"][src.name] = {
            "repo": src.repo,
            "commit": src.commit,
            "path": src.path,
            "url": src.url,
            "license": src.license,
            "snapshot_utc": src.snapshot_utc,
            "sha256": sha256(src.local_path),
            "bytes": src.local_path.stat().st_size,
        }
    if config.MANIFEST_PATH.exists():
        old = json.loads(config.MANIFEST_PATH.read_text())
        for name, entry in old.get("sources", {}).items():
            new = manifest["sources"].get(name)
            if new and new["commit"] == entry["commit"] and new["sha256"] != entry["sha256"]:
                raise RuntimeError(f"{name}: same commit, different bytes - refusing to overwrite manifest")
    config.MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def _utc(series: pd.Series) -> pd.Series:
    # Whole seconds: alerts.in.ua gives starts with milliseconds but finishes
    # rounded to the second, so a raion hand-over in the same second would
    # otherwise look like a sub-second gap between two alerts.
    return pd.to_datetime(series, utc=True, format="ISO8601").dt.floor("s")


def load_official() -> pd.DataFrame:
    raw = pd.read_csv(config.OFFICIAL.local_path, dtype=str, keep_default_na=False)
    # The published file repeats ~38% of its rows verbatim.
    raw = raw.drop_duplicates()
    raw = raw[~raw["oblast"].isin(EXCLUDED_OFFICIAL)]
    unknown = set(raw["oblast"]) - set(OFFICIAL_TO_ID)
    if unknown:
        raise ValueError(f"official: unmapped oblasts {sorted(unknown)}")
    unit = raw["raion"].where(raw["level"] == "raion", raw["hromada"])
    out = pd.DataFrame(
        {
            "region": raw["oblast"].map(OFFICIAL_TO_ID),
            "unit_level": raw["level"],
            "unit": unit.where(raw["level"] != "oblast", ""),
            "started_at": _utc(raw["started_at"]),
            "finished_at": _utc(raw["finished_at"].replace("", None)),
            "level": "single",
            "source": config.OFFICIAL.name,
        }
    )
    return out[COLUMNS].reset_index(drop=True)


def load_alerts_in_ua() -> pd.DataFrame:
    raw = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False)
    raw = raw[raw["alert_type"] == "air_raid"]
    raw = raw[~raw["location_oblast"].isin(EXCLUDED_ALERTS_IN_UA)]
    unknown = set(raw["location_oblast"]) - set(UA_TO_ID)
    if unknown:
        raise ValueError(f"alerts_in_ua: unmapped oblasts {sorted(unknown)}")
    level = raw["location_type"]
    out = pd.DataFrame(
        {
            "region": raw["location_oblast"].map(UA_TO_ID),
            "unit_level": level,
            "unit": raw["location_title"].where(level != "oblast", ""),
            "started_at": _utc(raw["started_at"]),
            "finished_at": _utc(raw["finished_at"].replace("", None)),
            "level": raw["alert_level"],
            "source": config.ALERTS_IN_UA.name,
        }
    )
    return out[COLUMNS].reset_index(drop=True)
