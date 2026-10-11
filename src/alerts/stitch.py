"""Stitch the official feed and the alerts.in.ua archive into one alert table."""

from __future__ import annotations

import pandas as pd

from . import config
from .data import load_alerts_in_ua, load_official


def stitch(official: pd.DataFrame, alerts_in_ua: pd.DataFrame, cutover=config.CUTOVER_UTC) -> pd.DataFrame:
    """Alerts starting before the cutover come from the official feed, the rest
    from alerts.in.ua. An alert is assigned to exactly one source by its start,
    so nothing is counted twice across the seam."""
    a = official[official["started_at"] < cutover]
    b = alerts_in_ua[alerts_in_ua["started_at"] >= cutover]
    out = pd.concat([a, b], ignore_index=True)
    out = out.sort_values(["region", "started_at", "unit_level", "unit"], kind="stable").reset_index(drop=True)
    return out


def region_alerts(alerts: pd.DataFrame, levels=config.REGION_UNIT_LEVELS) -> pd.DataFrame:
    """Keep only the unit levels that define a region's alert state."""
    return alerts[alerts["unit_level"].isin(levels)].reset_index(drop=True)


def load_all() -> pd.DataFrame:
    """Every unit level (oblast, raion, hromada, city) of the stitched table."""
    alerts = stitch(load_official(), load_alerts_in_ua())
    return alerts[alerts["started_at"] < config.DATA_END_UTC].reset_index(drop=True)


def load_stitched(levels=config.REGION_UNIT_LEVELS) -> pd.DataFrame:
    return region_alerts(load_all(), levels)
