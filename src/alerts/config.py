"""Project-wide constants: data sources, the stitching seam, excluded periods.

Every number here is a decision documented in README.md ("Data decisions").
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
# Overridable so the reproducibility check can write to a scratch directory.
ARTIFACTS_DIR = Path(os.environ.get("ALERTS_ARTIFACTS_DIR", ROOT / "artifacts"))
REPORTS_DIR = Path(os.environ.get("ALERTS_REPORTS_DIR", ROOT / "reports"))
MANIFEST_PATH = ROOT / "data_manifest.json"

KYIV_TZ = "Europe/Kyiv"


@dataclass(frozen=True)
class Source:
    name: str
    repo: str
    commit: str
    path: str
    license: str
    # Moment the pinned snapshot was taken (commit time). Alerts without a
    # finish time are treated as still active at this moment.
    snapshot_utc: str

    @property
    def url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{self.path}"

    @property
    def local_path(self) -> Path:
        return RAW_DIR / f"{self.name}.csv"


# Official alerts (mirror of the @air_alert_ua channel), 2022-03-15 .. 2026-09-07.
OFFICIAL = Source(
    name="official",
    repo="Vadimkin/ukrainian-air-raid-sirens-dataset",
    commit="bf0c323c78e3e4508721458797dcd38c95df13f3",
    path="datasets/official_data_en.csv",
    license="MIT",
    snapshot_utc="2026-10-11T04:50:14Z",
)

# alerts.in.ua archive with red/yellow levels, complete from 2026-09-06 ~12:20 UTC.
ALERTS_IN_UA = Source(
    name="alerts_in_ua",
    repo="JustJimer/ukraine-air-alert-stats",
    commit="7abc556ba5a0ff47a9e33c6abc136b8da3a9feb8",
    path="archive/alerts_in_ua.csv",
    license="none stated (data is downloaded, never redistributed)",
    snapshot_utc="2026-10-11T10:07:59Z",
)

SOURCES = (OFFICIAL, ALERTS_IN_UA)

# Alerts that start before the cutover come from OFFICIAL, the rest from
# ALERTS_IN_UA. The two overlap for ~16 h (2026-09-06 13:00 .. 09-07 05:28 UTC)
# and agree at Jaccard >= 0.93 for 11 of 13 regions active in that window.
CUTOVER_UTC = pd.Timestamp("2026-09-06 13:00", tz="UTC")

# End of usable data: the alerts.in.ua snapshot moment.
DATA_END_UTC = pd.Timestamp(ALERTS_IN_UA.snapshot_utc)

# First moment covered by the official feed (its first alert starts 16:10 UTC);
# grid hours before it are not valid data.
DATA_START_UTC = pd.Timestamp("2022-03-15 16:00", tz="UTC")

# Start of the modelling history. 2022 is a distinct regime (invasion phase,
# official data only from 2022-03-15) and is left out of training.
HISTORY_START_UTC = pd.Timestamp("2023-01-01 00:00", tz=KYIV_TZ).tz_convert("UTC")

# Kyiv calendar days that are neither trained on nor evaluated:
# 2026-08-29..09-02 - the official feed has no starts on 08-30..09-01 and
#   partial days around them;
# 2026-09-06..09-07 - the stitching seam between the two sources.
EXCLUDED_DAYS = tuple(
    str(d.date())
    for d in list(pd.date_range("2026-08-29", "2026-09-02")) + list(pd.date_range("2026-09-06", "2026-09-07"))
)

# First yellow-level alert in alerts.in.ua: the two-level alert system
# (Cabinet resolution No 1092) changes what an "alert" covers from here on.
LEVEL_SYSTEM_START_UTC = pd.Timestamp("2026-09-07 05:36:03", tz="UTC")

# Only oblast- and raion-level alerts define a region's state ("an alert in a
# raion means an alert in the oblast"). Hromada- and city-level alerts define
# the zones of regular alerts (alerts.zones) and get their own forecast: in
# frontline communities they are declared almost daily and some last for months
# (one in Kharkivskyi raion started 2024-05-12 and lasted ~14,500 h), so they
# would pin the whole oblast to "alert".
REGION_UNIT_LEVELS = ("oblast", "raion")

# Zone of regular alerts: hromadas with an alert on at least ZONE_MIN_SHARE of
# the valid days among the ZONE_LOOKBACK_DAYS before the day. Chosen on the
# validation window (reports/validation.json, "zones").
ZONE_LOOKBACK_DAYS = 7
ZONE_MIN_SHARE = 0.9

# Telegram channels whose posts feed the text features (alerts.telegram).
# Chosen on the validation window (reports/validation.json, "text").
TEXT_CHANNELS: tuple[str, ...] = ("war_monitor", "eradar", "alarm_signal")
# Keep posts that were edited after publication (see alerts.telegram).
DROP_EDITED_POSTS = False

# Evaluation windows (Kyiv calendar days, inclusive).
PRE_BREAK_WINDOW = ("2026-06-01", "2026-08-22")
POST_BREAK_START = "2026-09-14"
# Model choices are made on this window only, before either test window.
VALIDATION_WINDOW = ("2026-03-02", "2026-05-31")

# Weekly refit cadence of the rolling-origin backtest.
REFIT_DAYS = 7

# Alerts of a region separated by any gap, however short, count separately:
# under intense attacks an alert can be re-declared within a minute, or declared
# late. Overlapping and touching alerts are one alert (union of intervals).
MERGE_GAP_SEC = 0

# The feature grid starts this long before the history so the longest lookback
# window (28 days) is filled from the first training sample on.
LOOKBACK_PAD = pd.Timedelta(days=35)

# Phase 2 horizons in hours.
HORIZONS = (1, 2, 3, 4, 5, 6)

SEED = 20261011
