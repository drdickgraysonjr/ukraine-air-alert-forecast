"""Cross-check of the stitched alert series against @UkraineAlarmSignal
(reports/crosscheck.json).

The channel posts every oblast- and raion-level alert start ("🚨 Сумська обл."
or "🚨 Сумський район (Сумська обл.)") and end ("🟢 ..."). It is an independent
copy of the official alert feed, so it can confirm the series after the seam
(2026-09-07), where only alerts.in.ua is available.

For each window the check reports, per alert level:
  - posts -> series: the share of the channel's alert-start posts with a start
    of the same region's series within TOL minutes;
  - series -> posts: the share of series starts with such a post.
The pre-seam window (official feed) is the reference for what agreement looks like.
"""

from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from . import config, stitch, telegram
from . import timeline as T

TOL_MIN = 5
# One place per line: "Сумський район (Сумська обл.)", "Сумська обл.", "м. Київ" / "Київ".
_PLACE = re.compile(r"^\s*(?:(?P<raion>[^\n(]+?)\s+район\s*\()?\s*(?P<obl>[^\n()]+?)\s+обл\.?\)?\s*$")
_KYIV = re.compile(r"^\s*(м\.\s*)?київ\s*$", re.I)
_OBL = {v[1].replace(" область", "").lower(): k for k, v in __import__("alerts.regions", fromlist=["REGIONS"]).REGIONS.items()}
# Post headers that announce an alert start, and the level they announce.
_START = {"🚨": "single", "🟡": "yellow", "🔴": "red"}
WINDOWS = {"before_seam": ("2026-06-01", "2026-09-05"), "after_seam": ("2026-09-08", None)}


def _place(line: str):
    if _KYIV.match(line):
        return "kyiv_city", "oblast"
    m = _PLACE.match(line)
    if not m:
        return None
    r = _OBL.get(m.group("obl").strip().lower())
    return (r, "raion" if m.group("raion") else "oblast") if r else None


def alert_posts(df: pd.DataFrame) -> pd.DataFrame:
    """(ts, region, unit_level, level) for every place in an alert-start post.
    Single posts: "🟡 Полтавський район (Полтавська обл.)\nЖовтий рівень тривоги...";
    grouped posts: "🔴 Червоний рівень тривоги\n<place>\n<place>..." (blocks split by
    blank lines). "🟡 Відбій червоної тривоги" (red down to yellow) and "🟢" (end)
    are not starts."""
    rows = []
    for ts, text in zip(df["ts"], df["text"]):
        for block in re.split(r"\n\s*\n", text):
            lines = [x for x in block.split("\n") if x.strip()]
            if not lines:
                continue
            head = lines[0].strip()
            emoji = head[:1]
            if emoji not in _START or "відбій" in head.lower():
                continue
            rest = head[1:].strip()
            p = _place(rest)
            places = [p] if p else [_place(x) for x in lines[1:]]
            for q in places:
                if q:
                    rows.append((int(ts), q[0], q[1], _START[emoji]))
    return pd.DataFrame(rows, columns=["ts", "region", "unit_level", "level"])


def _share(a: np.ndarray, b: np.ndarray, tol: int) -> float | None:
    """Share of points in a with some point of b within tol seconds."""
    if len(a) == 0:
        return None
    b = np.sort(b)
    i = np.searchsorted(b, a)
    lo = np.abs(a - b[np.maximum(i - 1, 0)]) if len(b) else np.full(len(a), np.inf)
    hi = np.abs(b[np.minimum(i, len(b) - 1)] - a) if len(b) else np.full(len(a), np.inf)
    return float(np.mean(np.minimum(lo, hi) <= tol))


def run() -> dict:
    posts = alert_posts(telegram.load_channel("alarm_signal"))
    alerts = stitch.load_stitched()
    last = str((config.DATA_END_UTC.tz_convert(config.KYIV_TZ).normalize() - pd.Timedelta(days=1)).date())
    tol = TOL_MIN * 60
    out = {"tolerance_min": TOL_MIN, "posts_last_utc": str(pd.Timestamp(int(posts["ts"].max()), unit="s", tz="UTC")), "windows": {}}
    for name, (a, b) in WINDOWS.items():
        b = b or last
        lo = int(pd.Timestamp(a).tz_localize(config.KYIV_TZ).timestamp())
        hi = int((pd.Timestamp(b) + pd.Timedelta(days=1)).tz_localize(config.KYIV_TZ).timestamp())
        p = posts[(posts["ts"] >= lo) & (posts["ts"] < hi)]
        s = alerts[(alerts["started_at"] >= pd.Timestamp(lo, unit="s", tz="UTC")) & (alerts["started_at"] < pd.Timestamp(hi, unit="s", tz="UTC"))]
        res = {"first_day": a, "last_day": b, "levels": {}}
        for level in sorted(s["level"].unique()):
            sl = s[s["level"] == level]
            p2s, s2p, n_p, n_s = [], [], 0, 0
            for r in sorted(set(sl["region"]) | set(p["region"])):
                ps = p[p["region"] == r]["ts"].to_numpy(np.int64)
                ss = T.to_sec(sl[sl["region"] == r]["started_at"]).astype(np.int64)
                all_s = T.to_sec(s[s["region"] == r]["started_at"]).astype(np.int64)
                if len(ps):
                    p2s.append(_share(ps, all_s, tol) * len(ps))
                    n_p += len(ps)
                if len(ss):
                    s2p.append(_share(ss, ps, tol) * len(ss))
                    n_s += len(ss)
            res["levels"][level] = {"series_starts": n_s, "series_to_posts": sum(s2p) / n_s if n_s else None}
        res["alert_posts"] = int(len(p))

        def p2s(pp):
            hit = 0.0
            for r in sorted(set(pp["region"])):
                ps = pp[pp["region"] == r]["ts"].to_numpy(np.int64)
                hit += _share(ps, T.to_sec(s[s["region"] == r]["started_at"]).astype(np.int64), tol) * len(ps)
            return hit / len(pp) if len(pp) else None

        res["posts_to_series"] = p2s(p)
        res["post_levels"] = {lv: {"posts": int((p["level"] == lv).sum()), "posts_to_series": p2s(p[p["level"] == lv])} for lv in sorted(set(p["level"]))}
        out["windows"][name] = res
    out["parser_digest"] = telegram.parser_digest()
    (config.REPORTS_DIR / "crosscheck.json").write_text(json.dumps(out, indent=2) + "\n")
    return out
