"""Gate: the series after the seam is cross-checked against @UkraineAlarmSignal.

reports/crosscheck.json is recomputed from the pinned export and must match;
the export must reach the last complete day of the alert data, so the whole
post-seam window is covered; both windows must have alert posts to compare.
"""

import json
import sys

import pandas as pd

from alerts import config, crosscheck


def main() -> int:
    err = []
    saved = json.loads((config.REPORTS_DIR / "crosscheck.json").read_text())
    keep = config.REPORTS_DIR
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        config.REPORTS_DIR = Path(d)
        try:
            fresh = crosscheck.run()
        finally:
            config.REPORTS_DIR = keep
    if fresh != saved:
        err.append("reports/crosscheck.json is not reproduced from the export")
    last = pd.Timestamp(fresh["windows"]["after_seam"]["last_day"]).tz_localize(config.KYIV_TZ) + pd.Timedelta(days=1)
    if pd.Timestamp(fresh["posts_last_utc"]) < last.tz_convert("UTC") - pd.Timedelta(hours=6):
        err.append(f"the export ends {fresh['posts_last_utc']}, before the end of the post-seam window {last}")
    for name, w in fresh["windows"].items():
        if not w["alert_posts"]:
            err.append(f"{name}: no alert posts")
        lv = ", ".join(f"{k}: {v['series_starts']} starts, {v['series_to_posts']:.1%} with a post" for k, v in w["levels"].items() if v["series_to_posts"] is not None)
        print(f"{name} {w['first_day']}..{w['last_day']}: {w['alert_posts']} alert posts, {w['posts_to_series']:.1%} matched by a series start within {fresh['tolerance_min']} min; {lv}")
    if err:
        print("\n".join(err))
        return 1
    print("SEAM CROSSCHECK VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
