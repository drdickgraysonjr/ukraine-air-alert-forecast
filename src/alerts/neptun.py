"""Live alerts and Telegram posts from NEPTUN (https://neptun.in.ua/).

NEPTUN is an information aggregator, not an official warning system. Its API is
free, read-only and has no history: `/api/v1/alerts` returns the alerts active
right now (raions and oblast-level alerts, no hromadas), `/api/v1/messages` the
latest ~120 posts (~15 min) of ~31 Telegram channels. Terms
(https://neptun.in.ua/api-terms): poll no more often than once every 5 s and
show a visible link to https://neptun.in.ua/ next to the data
(config.NEPTUN_ATTRIBUTION, config.NEPTUN_DISCLAIMER).

Because there is no history, live data exists only from the moment `record()`
starts. Each poll appends one JSON line to
data/live/neptun/alerts-YYYY-MM-DD.jsonl (UTC date of the poll):
    {"fetched_at", "updatedAt", "version", "raions", "oblasts"}
and new posts to data/live/neptun/messages.jsonl:
    {"fetched_at", "channel", "date", "text"}.

From snapshots to alerts (`episodes`). An alert row is one continuous presence
of (location, level, since) in consecutive snapshots:
  * started_at = `since` (NEPTUN's start of the current alert);
  * finished_at = fetched_at of the first snapshot in which it is absent, so
    finishes are late by up to one poll interval (60 s by default) - except
    when the same location shows a new alert in that snapshot whose `since`
    lies between the two polls: then the old alert ended exactly at that
    `since` (a hand-over, e.g. a red -> yellow level change);
  * still present in the latest snapshot -> finished_at = NaT.
A level change is therefore two rows - the old level finishes, the new one
starts - which is how alerts.in.ua writes level changes after the seam
(2026-09-07): the old row finishes and a row with the new level starts 7-30 s
later; its rows of one location never overlap. If the new row's `since` lies
before the previous poll (NEPTUN kept the original start across the level
change) it would overlap the old row, so it starts when the old row finishes.
A key that disappears and comes back with the same `since` is the same alert
(a gap in the feed): the row is reopened. Every field of a row is fixed from
the snapshots up to the one that reveals it, never from later ones.

From NEPTUN keys to the model's locations (`locate_raion`, `locate_oblast`):
raion keys are unique in Ukraine and map to the alerts.in.ua raion title with
the same transliterated key (alerts.translit.raion_key folds apostrophe
variants and three 2024 renames; RAION_RENAMES adds the old names NEPTUN's
raions.geojson keeps - Новомосковський -> Самарівський, Новоград-Волинський ->
Звягельський - and Crimea's 2024 renames).
Oblast-level keys map to the alerts.in.ua oblast title; Kyiv city is the
oblast-level location «м. Київ» (region kyiv_city, key 'м. київ'). Crimea,
Sevastopol, Luhansk oblast and their raions map to locations of the excluded
regions (region None, dropped by `normalize` like data.load_alerts_in_ua
does). An unknown key raises UnmappedKey.

Joining the pinned table (`live_alerts`): the pinned data ends at
config.DATA_END_UTC and NEPTUN starts at the first recorded snapshot; nothing
is known in between. An alert open at the data end that the first snapshot
shows at the same location, level and start (`start_match`: within 1 s, or in
the same minute when NEPTUN gives a whole-minute start) is the same alert;
one that the first snapshot does not show is closed at it (or at the start of
a newer alert of that location, if earlier; at the data end when the gap is
longer than an hour) and listed as uncertain.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .regions import EXCLUDED_ALERTS_IN_UA, REGIONS, UA_TO_ID
from .translit import raion_key

MIN_POLL_SEC = 5
# An alert open at the data end and absent from the first snapshot is closed at that
# snapshot when the gap is at most this long, otherwise at the data end.
GAP_CLOSE_AT_SNAPSHOT_SEC = 3600
TIMEOUT_SEC = 30
USER_AGENT = (
    "ukraine-air-alert-forecast/0.1 (research forecast of air raid alerts by region; "
    f"REST poll every {config.NEPTUN_POLL_SEC} s by default; attribution shown with the data)"
)
ALERTS_URL = f"{config.NEPTUN_BASE_URL}/api/v1/alerts"
MESSAGES_URL = f"{config.NEPTUN_BASE_URL}/api/v1/messages"
GEOJSON_URLS = {
    "raions.geojson": f"{config.NEPTUN_BASE_URL}/raions.geojson",
    "oblasts.geojson": f"{config.NEPTUN_BASE_URL}/oblasts.geojson",
}
LIVE_DIR = config.NEPTUN_DIR
PINNED_DIR = LIVE_DIR / "pinned"
MESSAGES_FILE = "messages.jsonl"
MESSAGE_FETCHES_FILE = "messages-fetches.jsonl"

# alerts.in.ua column order (data/raw/alerts_in_ua.csv).
AIU_COLUMNS = ["id", "location_uid", "location_type", "location_title", "location_oblast", "location_raion",
               "started_at", "finished_at", "alert_type", "alert_level", "threats"]
# Extra columns of episodes(detail=True).
DETAIL_COLUMNS = ["region", "neptun_key", "neptun_since", "seen_at", "finish_seen_at", "finish_kind"]

CRIMEA, LUHANSK = "Автономна Республіка Крим", "Луганська область"
SEVASTOPOL = "м. Севастополь"
assert set(EXCLUDED_ALERTS_IN_UA) == {CRIMEA, LUHANSK}

# Consecutive snapshots further apart than this are a pause of the recorder:
# finishes inside it are late and alerts inside it are missing.
RECORDING_GAP_SEC = 300

# Two starts are the same alert when they differ by at most this much.
SAME_START_SEC = 1.0
# A NEPTUN start on a whole minute (no seconds, no fraction) carries minute
# precision: in the pinned snapshot the 2022 oblast-level alerts show
# 16:45:00 (Luhansk) where alerts.in.ua has 16:45:39. Such a start matches an
# alerts.in.ua start in the same minute.
MINUTE = 60.0


class UnmappedKey(ValueError):
    """A NEPTUN location key that has no model location."""


def _log(msg: str) -> None:
    print(f"[neptun {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", file=sys.stderr, flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# ------------------------------------------------------------------ fetching

def _get(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        if r.status != 200:
            raise urllib.error.HTTPError(url, r.status, f"HTTP {r.status}", r.headers, None)
        return r.read()


def _check_alerts(d: dict) -> dict:
    for k in ("updatedAt", "raions", "oblasts"):
        if k not in d:
            raise ValueError(f"NEPTUN alerts: missing field {k!r}")
    for e in d["raions"] + d["oblasts"]:
        for k in ("key", "since", "level"):
            if k not in e:
                raise ValueError(f"NEPTUN alerts: entry without {k!r}: {e}")
    return d


def fetch_alerts() -> dict:
    """One GET of /api/v1/alerts: {version, updatedAt, raions, oblasts}."""
    return _check_alerts(json.loads(_get(ALERTS_URL)))


def fetch_messages() -> dict:
    """One GET of /api/v1/messages: {messages: [{channel, text, date}]}."""
    d = json.loads(_get(MESSAGES_URL))
    if "messages" not in d:
        raise ValueError("NEPTUN messages: missing field 'messages'")
    return d


def snapshot_line(d: dict, fetched_at: str) -> dict:
    return {"fetched_at": fetched_at, "updatedAt": d.get("updatedAt"), "version": d.get("version"),
            "raions": d["raions"], "oblasts": d["oblasts"]}


def _msg_key(m: dict) -> str:
    return hashlib.sha1(json.dumps([m.get("channel"), m.get("date"), m.get("text")], ensure_ascii=False).encode()).hexdigest()


def _append(path: Path, rows) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def record(interval: float = config.NEPTUN_POLL_SEC, hours: float | None = None, out_dir: Path | None = None,
           messages_every: float = config.NEPTUN_MESSAGES_EVERY_SEC, *, _fetch_alerts=None, _fetch_messages=None,
           _sleep=time.sleep, _clock=time.monotonic, _max_polls: int | None = None) -> dict:
    """Poll the alerts every `interval` s and the messages every `messages_every`
    s for `hours` hours (None = until interrupted), appending JSON lines under
    out_dir (default data/live/neptun). Any two requests are at least
    MIN_POLL_SEC apart. Errors (network, HTTP, JSON) are logged and the next poll
    backs off exponentially (2x, 4x, ... the interval, at most 10 min). Returns
    counters."""
    if interval < MIN_POLL_SEC or messages_every < MIN_POLL_SEC:
        raise ValueError(f"NEPTUN terms: poll no more often than every {MIN_POLL_SEC} s (got {interval}, {messages_every})")
    fa = _fetch_alerts or fetch_alerts
    fm = _fetch_messages or fetch_messages
    out = Path(out_dir) if out_dir is not None else LIVE_DIR
    out.mkdir(parents=True, exist_ok=True)
    mpath = out / MESSAGES_FILE
    seen = set()
    if mpath.exists():
        with open(mpath, encoding="utf-8") as fh:
            seen = {_msg_key(json.loads(x)) for x in fh if x.strip()}
    stats = {"polls": 0, "snapshots": 0, "errors": 0, "message_fetches": 0, "new_messages": 0, "possible_message_gaps": 0}
    t_start = _clock()
    deadline = None if hours is None else t_start + hours * 3600
    last_request = [None]

    def polite(fn):
        if last_request[0] is not None:
            wait = MIN_POLL_SEC - (_clock() - last_request[0])
            if wait > 0:
                _sleep(wait)
        last_request[0] = _clock()
        return fn()

    next_msg = t_start
    failures = 0
    had_messages = False
    _log(f"recording to {out} every {interval:g} s" + (f" for {hours:g} h" if hours else " until interrupted"))
    try:
        while (deadline is None or _clock() < deadline) and (_max_polls is None or stats["polls"] < _max_polls):
            cycle = _clock()
            stats["polls"] += 1
            ok = True
            try:
                d = polite(fa)
                fetched = _now_iso()
                _append(out / f"alerts-{fetched[:10]}.jsonl", [snapshot_line(d, fetched)])
                stats["snapshots"] += 1
            except Exception as e:  # network, HTTP, JSON: log and keep going
                ok = False
                stats["errors"] += 1
                _log(f"alerts: {type(e).__name__}: {e}")
            if ok and cycle >= next_msg:
                try:
                    msgs = polite(fm)["messages"]
                    fetched = _now_iso()
                    new = []
                    for m in msgs:
                        k = _msg_key(m)
                        if k not in seen:
                            seen.add(k)
                            new.append({"fetched_at": fetched, "channel": m.get("channel"), "date": m.get("date"), "text": m.get("text")})
                    # No post seen before: posts may have been missed between the
                    # two fetches (the feed keeps only ~15 min).
                    gap = bool(had_messages and msgs and len(new) == len(msgs))
                    if gap:
                        stats["possible_message_gaps"] += 1
                        _log("messages: no overlap with the previous fetch - posts may be missing")
                    _append(mpath, new)
                    _append(out / MESSAGE_FETCHES_FILE, [{"fetched_at": fetched, "n": len(msgs), "n_new": len(new), "possible_gap": gap}])
                    stats["message_fetches"] += 1
                    stats["new_messages"] += len(new)
                    had_messages = had_messages or bool(msgs)
                    next_msg = cycle + messages_every
                except Exception as e:
                    ok = False
                    stats["errors"] += 1
                    _log(f"messages: {type(e).__name__}: {e}")
            failures = 0 if ok else failures + 1
            wait = interval if failures == 0 else min(interval * 2 ** failures, max(600.0, interval))
            wake = cycle + wait
            if deadline is not None and wake >= deadline:
                break
            _sleep(max(0.0, wake - _clock()))
    except KeyboardInterrupt:
        _log("interrupted")
    _log(f"done: {stats}")
    return stats


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pin(out_dir: Path | None = None) -> dict:
    """Fetch the alerts, the messages and both GeoJSON files once and save them
    with a manifest of sha256 and fetch times (4 requests, >= 5 s apart)."""
    out = Path(out_dir) if out_dir is not None else PINNED_DIR
    out.mkdir(parents=True, exist_ok=True)
    jobs = [("alerts.json", ALERTS_URL, "application/json"), ("messages.json", MESSAGES_URL, "application/json")]
    jobs += [(name, url, "application/geo+json, application/json") for name, url in GEOJSON_URLS.items()]
    manifest = {"source": config.NEPTUN_BASE_URL, "terms": f"{config.NEPTUN_BASE_URL}/api-terms",
                "attribution": config.NEPTUN_ATTRIBUTION, "files": {}}
    for i, (name, url, accept) in enumerate(jobs):
        if i:
            time.sleep(MIN_POLL_SEC)
        b = _get(url, accept)
        fetched = _now_iso()
        obj = json.loads(b)
        if name == "alerts.json":
            _check_alerts(obj)
        (out / name).write_bytes(b)
        manifest["files"][name] = {"url": url, "fetched_at": fetched, "sha256": sha256_file(out / name), "bytes": len(b)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def load_pinned(out_dir: Path | None = None) -> tuple[dict, dict]:
    """(pinned alerts.json as a snapshot line, manifest); checks the sha256."""
    d = Path(out_dir) if out_dir is not None else PINNED_DIR
    man = json.loads((d / "manifest.json").read_text())
    for name, e in man["files"].items():
        if sha256_file(d / name) != e["sha256"]:
            raise ValueError(f"{d / name}: sha256 differs from manifest.json")
    a = json.loads((d / "alerts.json").read_text())
    return snapshot_line(a, man["files"]["alerts.json"]["fetched_at"]), man


def load_snapshots(live_dir: Path | None = None) -> list[dict]:
    """Every recorded snapshot line, oldest first."""
    d = Path(live_dir) if live_dir is not None else LIVE_DIR
    lines = []
    for p in sorted(d.glob("alerts-*.jsonl")):
        with open(p, encoding="utf-8") as fh:
            lines += [json.loads(x) for x in fh if x.strip()]
    lines.sort(key=lambda x: _utc(x["fetched_at"]))
    return lines


def load_messages(live_dir: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(recorded posts, recorded message fetches)."""
    d = Path(live_dir) if live_dir is not None else LIVE_DIR
    out = []
    for name in (MESSAGES_FILE, MESSAGE_FETCHES_FILE):
        p = d / name
        rows = []
        if p.exists():
            with open(p, encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        out.append(rows)
    return out[0], out[1]


# ------------------------------------------------------------------ mapping

@dataclass(frozen=True)
class Location:
    neptun_key: str
    location_uid: str
    location_type: str  # "raion" | "oblast" (alerts.in.ua location_type)
    location_title: str
    location_oblast: str
    region: str | None  # model region id; None for the excluded regions


def _apos(s: str) -> str:
    return s.replace("’", "'").replace("ʼ", "'")


@lru_cache(maxsize=1)
def _aiu_catalog() -> tuple[dict, dict]:
    """From the pinned alerts.in.ua table: (oblast, raion key) -> (uid, title),
    and oblast-level title -> uid."""
    raw = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False,
                      usecols=["location_uid", "location_type", "location_title", "location_oblast"])
    raw = raw.drop_duplicates()
    raions = {}
    for uid, title, obl in raw.loc[raw["location_type"] == "raion", ["location_uid", "location_title", "location_oblast"]].itertuples(index=False):
        k = (obl, raion_key(title))
        if k in raions and raions[k] != (uid, title):
            raise ValueError(f"alerts.in.ua: two raions with key {k}: {raions[k]} and {(uid, title)}")
        raions[k] = (uid, title)
    oblasts = dict(raw.loc[raw["location_type"] == "oblast", ["location_title", "location_uid"]].itertuples(index=False))
    return raions, oblasts


def _oblast_key(title: str) -> str:
    return _apos(title).lower().replace(" область", "").strip()


# NEPTUN oblast-level key -> alerts.in.ua oblast title. Kyiv city and
# Sevastopol may come with or without "м.".
_OBLAST_TITLES = {_oblast_key(v[1]): v[1] for v in REGIONS.values()}
_OBLAST_TITLES.update({_oblast_key(t): t for t in EXCLUDED_ALERTS_IN_UA})
_OBLAST_TITLES.update({"київ": "м. Київ", "місто київ": "м. Київ", "севастополь": SEVASTOPOL, "м. севастополь": SEVASTOPOL,
                       "місто севастополь": SEVASTOPOL, "крим": CRIMEA})


def _region_of_oblast(title: str) -> str | None:
    if title in UA_TO_ID:
        return UA_TO_ID[title]
    if title in EXCLUDED_ALERTS_IN_UA or title == SEVASTOPOL:
        return None
    raise UnmappedKey(f"unknown oblast {title!r}")


def _oblast_title(name: str) -> str:
    """NEPTUN 'oblast' field or oblast key -> alerts.in.ua oblast title."""
    t = _OBLAST_TITLES.get(_oblast_key(name))
    if t is None:
        raise UnmappedKey(f"NEPTUN oblast {name!r} has no model location")
    return t


def locate_oblast(key: str, oblast: str | None = None) -> Location:
    """NEPTUN oblast-level key (e.g. 'луганська', 'київ', 'севастополь')."""
    title = _OBLAST_TITLES.get(_oblast_key(key))
    if title is None:
        raise UnmappedKey(f"NEPTUN oblast key {key!r} has no model location")
    region = _region_of_oblast(title)
    if oblast:  # the entry's own 'oblast' field must agree
        parent = _oblast_title(oblast)
        if parent != title and not (title == SEVASTOPOL and parent == CRIMEA):
            raise UnmappedKey(f"NEPTUN oblast key {key!r} ({title}) listed under {oblast!r}")
    _, obl_uids = _aiu_catalog()
    loc_oblast = CRIMEA if title == SEVASTOPOL else title
    uid = obl_uids.get(title, f"neptun:{_oblast_key(key)}")
    return Location(_oblast_key(key), uid, "oblast", title, loc_oblast, region)


# Old raion names -> current ones, as raion keys (alerts.translit.raion_key).
# NEPTUN's raions.geojson still uses Novomoskovskyi (Samarivskyi since 2024) and
# Novohrad-Volynskyi (Zviahelskyi since 2022); Crimea's 2024 renames are folded
# too in case either spelling appears.
RAION_RENAMES = {"novomoskovskyi": "samarivskyi", "novohrad-volynskyi": "zviahelskyi",
                 "krasnohvardiiskyi": "kurmanskyi", "krasnoperekopskyi": "perekopskyi"}

# Raions of the excluded regions (2020 reform, current names), which
# alerts.in.ua does not list.
EXCLUDED_RAIONS = {
    LUHANSK: ("Алчевський", "Довжанський", "Луганський", "Ровеньківський", "Сватівський", "Сєвєродонецький",
              "Старобільський", "Щастинський"),
    CRIMEA: ("Бахчисарайський", "Білогірський", "Джанкойський", "Євпаторійський", "Керченський", "Курманський",
             "Перекопський", "Сімферопольський", "Феодосійський", "Ялтинський"),
}


def _raion_key(name: str) -> str:
    k = raion_key(_apos(name))
    return RAION_RENAMES.get(k, k)


@lru_cache(maxsize=1)
def _raion_index() -> dict[str, list[tuple[str, str, str]]]:
    """raion key -> [(oblast title, uid, title)] over every oblast."""
    raions, _ = _aiu_catalog()
    idx: dict[str, list] = {}
    for (obl, rk), (uid, title) in raions.items():
        idx.setdefault(rk, []).append((obl, uid, title))
    for obl, names in EXCLUDED_RAIONS.items():
        for n in names:
            rk = raion_key(n)
            idx.setdefault(rk, []).append((obl, f"neptun:{_oblast_key(obl)}/{rk}", f"{n} район"))
    return idx


def locate_raion(key: str, oblast: str | None = None) -> Location:
    """NEPTUN raion key -> alerts.in.ua raion location. Raion keys are unique in
    Ukraine (raions.geojson has 136 distinct keys and no oblast); when the entry
    names its oblast it must agree."""
    rk = _raion_key(key)
    cands = _raion_index().get(rk, [])
    if oblast:
        obl_title = _oblast_title(oblast)
        cands = [c for c in cands if c[0] == obl_title]
    if len(cands) != 1:
        where = f" in {oblast!r}" if oblast else ""
        raise UnmappedKey(f"NEPTUN raion {key!r} ({rk}){where}: {len(cands)} candidate locations {cands}")
    obl_title, uid, title = cands[0]
    return Location(_apos(key), uid, "raion", title, obl_title, _region_of_oblast(obl_title))


def locate(entry: dict, kind: str) -> Location:
    """kind: 'raions' or 'oblasts' (the list the entry came from)."""
    if kind == "raions":
        return locate_raion(entry["key"], entry.get("oblast"))
    if kind == "oblasts":
        return locate_oblast(entry["key"], entry.get("oblast"))
    raise ValueError(kind)


def geojson_keys(path: Path) -> list[tuple[str, str | None]]:
    """(key, parent name or None) of every feature of a NEPTUN GeoJSON file:
    raions.geojson has {key, rayon}, oblasts.geojson {key, region} or {key, name}."""
    g = json.loads(Path(path).read_text())
    out = []
    for f in g["features"]:
        p = f.get("properties") or {}
        if not p.get("key"):
            raise ValueError(f"{path}: feature without key: {p}")
        out.append((p["key"], p.get("oblast") or p.get("region") or p.get("name")))
    return out


# ------------------------------------------------------------------ episodes

def _since(entry: dict) -> pd.Timestamp:
    return _utc(entry["since"])


def _snapshot_entries(line: dict) -> dict:
    """(location uid, level, since) -> (Location, entry) of one snapshot."""
    out = {}
    for kind in ("oblasts", "raions"):
        for e in line.get(kind) or []:
            loc = locate(e, kind)
            lvl = e["level"]
            if lvl not in ("red", "yellow"):
                raise ValueError(f"NEPTUN: unknown level {lvl!r} for {e['key']!r}")
            out[(loc.location_uid, lvl, _since(e))] = (loc, e)
    return out


def episodes(snapshot_lines: list[dict], detail: bool = False) -> pd.DataFrame:
    """Alert rows in the alerts.in.ua schema from snapshot lines (see the module
    docstring for the rules). detail=True adds DETAIL_COLUMNS: region, NEPTUN key
    and since, the fetch time at which the row was first seen and at which its
    finish became known, and how the finish was set ('absent' | 'handover')."""
    lines = sorted(snapshot_lines, key=lambda x: _utc(x["fetched_at"]))
    rows: dict[tuple, dict] = {}  # (location uid, level, since) -> row
    by_loc: dict[str, list] = {}  # location uid -> its keys
    open_keys: set = set()
    prev_t = None
    for line in lines:
        t = _utc(line["fetched_at"])
        if prev_t is not None and t <= prev_t:
            continue  # the same poll recorded twice
        cur = _snapshot_entries(line)
        new = [k for k in cur if k not in open_keys]
        gone = [k for k in open_keys if k not in cur]
        # Close what disappeared: at the hand-over moment when the location shows
        # a new alert that started between the two polls, else at this fetch.
        for k in gone:
            r = rows[k]
            hand = [nk[2] for nk in new if nk[0] == k[0] and prev_t < nk[2] <= t and nk[2] >= r["started_at"]]
            r["finished_at"] = min(hand) if hand else t
            r["finish_kind"] = "handover" if hand else "absent"
            r["finish_seen_at"] = t
            open_keys.discard(k)
        for k in new:
            loc, e = cur[k]
            if k in rows:  # same location, level and start again: a gap in the feed
                rows[k].update(finished_at=pd.NaT, finish_kind="", finish_seen_at=pd.NaT)
                open_keys.add(k)
                continue
            start = min(k[2], t)  # a start "after" the fetch is clock skew
            # Rows of one location never overlap: a start before the end of the
            # location's previous row moves to that end.
            ends = [rows[kk]["finished_at"] for kk in by_loc.get(k[0], []) if kk not in open_keys]
            ends = [x for x in ends if pd.notna(x) and start < x <= t]
            if ends:
                start = max(ends)
            rows[k] = {
                "location_uid": loc.location_uid, "location_type": loc.location_type, "location_title": loc.location_title,
                "location_oblast": loc.location_oblast, "location_raion": "", "started_at": start, "finished_at": pd.NaT,
                "alert_type": "air_raid", "alert_level": k[1], "threats": "",
                "region": loc.region, "neptun_key": loc.neptun_key, "neptun_since": k[2], "seen_at": t,
                "finish_seen_at": pd.NaT, "finish_kind": "",
            }
            by_loc.setdefault(k[0], []).append(k)
            open_keys.add(k)
        prev_t = t
    cols = AIU_COLUMNS + (DETAIL_COLUMNS if detail else [])
    if not rows:
        df = pd.DataFrame({c: pd.Series(dtype=object) for c in cols})
        for c in ("started_at", "finished_at") + (("neptun_since", "seen_at", "finish_seen_at") if detail else ()):
            df[c] = pd.Series(dtype="datetime64[us, UTC]")
        return df
    df = pd.DataFrame(list(rows.values()))
    df["id"] = [f"neptun-{hashlib.sha1(f'{u}|{lv}|{s.isoformat()}'.encode()).hexdigest()[:12]}"
                for u, lv, s in zip(df["location_uid"], df["alert_level"], df["neptun_since"])]
    for c in ("started_at", "finished_at", "neptun_since", "seen_at", "finish_seen_at"):
        df[c] = pd.to_datetime(df[c], utc=True).dt.as_unit("us")
    df = df.sort_values(["started_at", "location_uid", "alert_level"], kind="stable").reset_index(drop=True)
    return df[cols]


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """alerts.in.ua-schema rows -> the normalised table of alerts.data (the same
    transformation as data.load_alerts_in_ua, for a frame instead of the CSV)."""
    from .data import COLUMNS

    raw = raw[raw["alert_type"] == "air_raid"]
    raw = raw[~raw["location_oblast"].isin(EXCLUDED_ALERTS_IN_UA)]
    unknown = set(raw["location_oblast"]) - set(UA_TO_ID)
    if unknown:
        raise ValueError(f"unmapped oblasts {sorted(unknown)}")
    level = raw["location_type"]

    def utc(s):
        if not pd.api.types.is_datetime64_any_dtype(s):
            s = pd.to_datetime(s.replace("", None), utc=True, format="ISO8601")
        return pd.to_datetime(s, utc=True).dt.floor("s")

    out = pd.DataFrame(
        {
            "region": raw["location_oblast"].map(UA_TO_ID),
            "unit_level": level,
            "unit": raw["location_title"].where(level != "oblast", ""),
            "raion": raw["location_title"].where(level == "raion", raw["location_raion"]),
            "started_at": utc(raw["started_at"]),
            "finished_at": utc(raw["finished_at"]),
            "level": raw["alert_level"],
            "source": "neptun",
        }
    )
    return out[COLUMNS].reset_index(drop=True)


# ------------------------------------------------------------------ live merge

def start_match(neptun_start, other_start) -> str | None:
    """'exact' when the starts differ by <= SAME_START_SEC, 'minute' when the
    NEPTUN start has minute precision and the other start lies in that minute,
    else None."""
    a, b = _utc(neptun_start), _utc(other_start)
    d = (b - a).total_seconds()
    if abs(d) <= SAME_START_SEC:
        return "exact"
    if a.second == 0 and a.microsecond == 0 and a.nanosecond == 0 and 0 <= d < MINUTE:
        return "minute"
    return None


def _unit_key(level: str, unit: str) -> str:
    return raion_key(unit) if level == "raion" else unit


def live_alerts(snapshot_lines: list[dict] | None = None) -> tuple[pd.DataFrame, dict]:
    """The pinned alert table as of its data end, continued with NEPTUN episodes.
    Returns (normalised table, report). An alert open at the data end that is
    present in the first snapshot with the same location, level and start is
    the same alert and takes the NEPTUN finish; one that is absent from the
    first snapshot finished somewhere in the gap and is closed at the first
    snapshot if the gap is at most GAP_CLOSE_AT_SNAPSHOT_SEC, else at the data end
    (uncertain, listed in the report). Alerts that started and ended
    inside the gap are unknown."""
    from . import stitch
    from . import timeline as T

    lines = load_snapshots() if snapshot_lines is None else sorted(snapshot_lines, key=lambda x: _utc(x["fetched_at"]))
    if not lines:
        raise ValueError("no NEPTUN snapshots: run `alerts neptun-record` first")
    first, last = _utc(lines[0]["fetched_at"]), _utc(lines[-1]["fetched_at"])
    if first <= config.DATA_END_UTC:
        raise ValueError(f"first snapshot {first} is not after the data end {config.DATA_END_UTC}")
    pinned = T.as_of(stitch.load_all(), config.DATA_END_UTC).reset_index(drop=True)
    ep = episodes(lines, detail=True)
    epn = normalize(ep[AIU_COLUMNS])
    keep = ~ep["location_oblast"].isin(EXCLUDED_ALERTS_IN_UA).to_numpy() if len(ep) else np.zeros(0, bool)
    epd = ep[keep].reset_index(drop=True)  # detail rows aligned with epn
    assert len(epd) == len(epn)

    gap_sec = (first - config.DATA_END_UTC).total_seconds()
    in_first = (epd["seen_at"] == first).to_numpy() if len(epd) else np.zeros(0, bool)
    is_open = pinned["finished_at"].isna().to_numpy()
    used = np.zeros(len(epn), bool)
    continued, closed_region, closed_unit, matches = [], [], [], {"exact": 0, "minute": 0}
    fin = pinned["finished_at"].copy()
    for i in np.nonzero(is_open)[0]:
        p = pinned.iloc[i]
        hit = None
        if p["unit_level"] in ("oblast", "raion"):
            pk = _unit_key(p["unit_level"], p["unit"])
            for j in np.nonzero(in_first & ~used)[0]:
                q = epn.iloc[j]
                if q["region"] != p["region"] or q["unit_level"] != p["unit_level"] or q["level"] != p["level"]:
                    continue
                if _unit_key(q["unit_level"], q["unit"]) != pk:
                    continue
                m = start_match(epd.loc[j, "neptun_since"], p["started_at"])
                if m:
                    hit = (j, m)
                    break
        desc = f"{p['region']} {p['unit_level']} {p['unit'] or '(oblast)'} {p['level']} since {p['started_at']}"
        if hit:
            j, m = hit
            used[j] = True
            matches[m] += 1
            fin.iloc[i] = epn.loc[j, "finished_at"]
            continued.append(desc)
            continue
        # Absent: it ended somewhere in the gap - by the first snapshot, or by the
        # start of a newer alert of the same location seen in the first snapshot
        # (rows of one location do not overlap). Over a long gap, closing at the
        # first snapshot would invent hours of alert; close at the data end instead
        # (an undercount of the gap, which is unknown anyway).
        end = first if gap_sec <= GAP_CLOSE_AT_SNAPSHOT_SEC else config.DATA_END_UTC
        if p["unit_level"] in ("oblast", "raion"):
            pk = _unit_key(p["unit_level"], p["unit"])
            for j in np.nonzero(in_first)[0]:
                q = epn.iloc[j]
                if (q["region"], q["unit_level"]) == (p["region"], p["unit_level"]) and _unit_key(q["unit_level"], q["unit"]) == pk \
                        and config.DATA_END_UTC < q["started_at"] < end:
                    end = q["started_at"]
        if end < config.DATA_END_UTC:
            end = config.DATA_END_UTC
        fin.iloc[i] = end
        (closed_region if p["unit_level"] in ("oblast", "raion") else closed_unit).append(
            desc + (f", closed at the next alert's start {end}" if end != first else ""))
    merged_pinned = pinned.assign(finished_at=fin)
    # NEPTUN rows that claim a start before the data end but continue nothing.
    pre = (~used) & (epn["started_at"] < config.DATA_END_UTC).to_numpy()
    conflicts = [f"{r.region} {r.unit_level} {r.unit or '(oblast)'} {r.level} since {r.started_at}" for r in epn[pre].itertuples()]
    rest = epn[~used].reset_index(drop=True)
    out = pd.concat([merged_pinned, rest], ignore_index=True)
    out = out.sort_values(["region", "started_at", "unit_level", "unit"], kind="stable").reset_index(drop=True)
    gap_h = (first - config.DATA_END_UTC).total_seconds() / 3600
    times = [_utc(x["fetched_at"]) for x in lines]
    pauses = [(str(a), str(b), round((b - a).total_seconds() / 60, 1)) for a, b in zip(times, times[1:])
              if (b - a).total_seconds() > RECORDING_GAP_SEC]
    report = {
        "data_end_utc": str(config.DATA_END_UTC),
        "first_snapshot_utc": str(first),
        "last_snapshot_utc": str(last),
        "n_snapshots": len(lines),
        "gap_h": round(gap_h, 2),
        "open_at_data_end": int(is_open.sum()),
        "continued": continued,
        "continued_matches": matches,
        "closed_in_gap_region_level": closed_region,
        "closed_in_gap_hromada_city": closed_unit,
        "conflicts": conflicts,
        "neptun_rows": int(len(rest)),
        "neptun_rows_excluded_regions": int((~keep).sum()) if len(ep) else 0,
        "recording_gaps": pauses,
    }
    return out, report


# ------------------------------------------------------------------ posts

# NEPTUN channel names that are not a repo channel's @handle.
CHANNEL_ALIASES = {"повітряні сили зсу": "kpszsu", "повітряні сили збройних сил україни": "kpszsu"}


def channel_key(name: str | None) -> str | None:
    """NEPTUN 'channel' field -> alerts.telegram channel key (None if not in the repo)."""
    from . import telegram

    if not name:
        return None
    n = name.strip().lower()
    for k, c in telegram.CHANNELS.items():
        if n in (c.handle.lower(), c.handle.lower().lstrip("@"), c.title.lower()):
            return k
    return CHANNEL_ALIASES.get(n)


def _parse_posts(df: pd.DataFrame, channel: str) -> pd.DataFrame:
    """Parse with the parser of alerts.telegram as it is at run time."""
    from . import telegram

    params = inspect.signature(telegram.parse_frame).parameters
    kw = {}
    for name in ("channel", "key"):
        if name in params:
            kw[name] = channel
    return telegram.parse_frame(df, **kw)


def live_posts(messages: list[dict] | None = None, fetches: list[dict] | None = None, until=None):
    """telegram.Posts for config.TEXT_CHANNELS: the pinned exports, continued with
    NEPTUN posts published after each export's last post (and by `until`).
    Returns (Posts or None, report)."""
    from . import datasets, telegram

    if messages is None:
        messages, f2 = load_messages()
        fetches = f2 if fetches is None else fetches
    fetches = fetches or []
    base = datasets.default_posts()
    until_s = None if until is None else int(_utc(until).value // 10**9)
    by_key: dict[str, list] = {}
    feed_names: dict[str, set] = {}
    for m in messages:
        k = channel_key(m.get("channel"))
        if k is None or not m.get("date"):
            continue
        if until is not None and _utc(m.get("fetched_at", m["date"])) > _utc(until):
            continue  # not yet recorded at `until`
        feed_names.setdefault(k, set()).add(m["channel"])
        by_key.setdefault(k, []).append(m)
    report = {"channels": list(base.frames) if base is not None else [], "in_feed": {}, "missing_from_feed": {},
              "possible_message_gaps": sum(bool(f.get("possible_gap")) for f in fetches
                                           if until is None or _utc(f["fetched_at"]) <= _utc(until)),
              "message_fetches": len(fetches)}
    if base is None:
        return None, report
    frames = {}
    for c, df0 in base.frames.items():
        last_ts = int(df0["ts"].max()) if len(df0) else -1
        last_str = str(pd.Timestamp(last_ts, unit="s", tz="UTC")) if last_ts >= 0 else None
        ms = by_key.get(c, [])
        if c not in feed_names:
            report["missing_from_feed"][c] = {"export_last_post_utc": last_str}
            frames[c] = df0
            continue
        rows = []
        for m in ms:
            ts = int(_utc(m["date"]).value // 10**9)
            if ts > last_ts and (until_s is None or ts <= until_s):
                rows.append((ts, m["text"] or ""))
        rows = sorted(set(rows))
        if rows:
            ts = np.array([r[0] for r in rows], np.int64)
            local = pd.to_datetime(ts, unit="s", utc=True).tz_convert(telegram.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
            df = pd.DataFrame({"id": -np.arange(1, len(rows) + 1), "ts_local": local, "edited": False, "fwd": False,
                               "text": [r[1] for r in rows], "ts": ts})
            parsed = _parse_posts(df, c)
            missing = [col for col in df0.columns if col not in parsed.columns]
            if missing:
                raise ValueError(f"parsed NEPTUN posts lack columns {missing} of the {c} export frame")
            new = parsed[list(df0.columns)]
            frames[c] = pd.concat([df0, new], ignore_index=True)
            first_new = str(pd.Timestamp(int(ts[0]), unit="s", tz="UTC"))
        else:
            frames[c] = df0
            first_new = None
        report["in_feed"][c] = {"feed_names": sorted(feed_names[c]), "export_last_post_utc": last_str,
                                "neptun_posts_added": len(rows), "first_added_utc": first_new}
    return telegram.Posts(frames), report
