"""Telegram channel exports: reading, parsing, and text features.

The exports are Telegram Desktop HTML exports ("Export chat history", HTML,
no media), copied to data/raw/telegram/<channel>/ and pinned by sha256 in
data_manifest.json. They are never committed: the posts belong to the channels.

Time. The export writes each post's local time on the exporting machine
(Europe/Warsaw, checked against t.me/s) without an offset. Warsaw and Kyiv
switch daylight saving time at the same instant, so the conversion to UTC is
exact except in the repeated hour of the autumn switch; there the order of post
ids decides which of the two hours a post belongs to.

Edits. The export keeps only the final text of an edited post and a flag that
it was edited, not when. Edited posts are kept (the text is all we have), the
flag is never a feature (it may be set after the forecast moment), and
`alerts validate edited` measures what dropping them changes.
"""

from __future__ import annotations

import glob
import hashlib
import html
import inspect
import os
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config
from .regions import NEIGHBORS, REGION_IDS

LOCAL_TZ = "Europe/Warsaw"
TELEGRAM_DIR = config.RAW_DIR / "telegram"
DERIVED_DIR = config.DATA_DIR / "derived" / "telegram"


@dataclass(frozen=True)
class Channel:
    key: str
    handle: str
    title: str


CHANNELS = {
    "kpszsu": Channel("kpszsu", "@kpszsu", "Air Force of the Armed Forces of Ukraine"),
    "war_monitor": Channel("war_monitor", "@war_monitor", "monitor"),
    "eradar": Channel("eradar", "@eRadarrua", "eRadar"),
    "alarm_signal": Channel("alarm_signal", "@UkraineAlarmSignal", "eTryvoha"),
}

MONTHS = {"січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6, "липня": 7,
          "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12}
_MSG = re.compile(r'<div class="message default clearfix( joined)?" id="message(-?\d+)">')
_DATE = re.compile(r'class="pull_right date details" title="(\d+) (\S+) (\d{4}), (\d\d:\d\d:\d\d)(?: UTC([+-]\d\d:\d\d))?">(edited )?')
_TEXT = re.compile(r'<div class="text">(.*?)</div>', re.S)


def export_files(path) -> list[str]:
    files = glob.glob(os.path.join(path, "messages*.html"))
    return sorted(files, key=lambda p: int(re.search(r"messages(\d*)\.html", p).group(1) or 1))


def export_sha256(path) -> tuple[str, int]:
    """One digest over the message files in order, and their total size."""
    h = hashlib.sha256()
    total = 0
    for f in export_files(path):
        b = open(f, "rb").read()
        h.update(os.path.basename(f).encode() + b"\0" + b)
        total += len(b)
    return h.hexdigest(), total


def _clean(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def read_export(path) -> pd.DataFrame:
    """All posts of an export: id, ts_local (naive), ts (UTC seconds), edited, fwd, text.
    Raises if any message block lacks a date, so nothing is dropped silently."""
    rows = []
    blocks = 0
    for f in export_files(path):
        s = open(f, encoding="utf-8").read()
        starts = [m for m in _MSG.finditer(s)]
        blocks += len(starts)
        bounds = [m.start() for m in starts] + [len(s)]
        for m, end in zip(starts, bounds[1:]):
            body = s[m.end():end]
            nxt = body.find('<div class="message service')
            if nxt >= 0:
                body = body[:nxt]
            d = _DATE.search(body)
            if not d:
                raise ValueError(f"{f}: message {m.group(2)} has no date")
            local = f"{d.group(3)}-{MONTHS[d.group(2)]:02d}-{int(d.group(1)):02d} {d.group(4)}"
            tx = _TEXT.search(body)
            rows.append((int(m.group(2)), local, d.group(5) or "", bool(d.group(6)), "forwarded" in body[: body.find('<div class="text">') if tx else len(body)], _clean(tx.group(1)) if tx else ""))
    df = pd.DataFrame(rows, columns=["id", "ts_local", "offset", "edited", "fwd", "text"])
    if len(df) != blocks:
        raise ValueError(f"{path}: {blocks} message blocks, {len(df)} posts")
    if df["id"].duplicated().any():
        raise ValueError(f"{path}: duplicate message ids")
    df = df.sort_values("id", kind="stable").reset_index(drop=True)
    df["ts"] = to_utc_seconds(df["ts_local"], df["offset"])
    return df.drop(columns="offset")


def to_utc_seconds(local: pd.Series, offset: pd.Series | None = None) -> np.ndarray:
    """Naive Europe/Warsaw times (in post-id order) -> UTC epoch seconds.
    In the repeated autumn hour, posts before the first backward jump of the
    clock are taken as summer time."""
    naive = pd.to_datetime(local, format="%Y-%m-%d %H:%M:%S")
    probe = naive.dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="NaT")
    amb = probe.isna().to_numpy()
    dst = np.zeros(len(naive), bool)
    if amb.any():
        vals = naive.to_numpy()
        for day in np.unique(vals[amb].astype("datetime64[D]")):
            idx = np.nonzero(amb & (vals.astype("datetime64[D]") == day))[0]
            back = np.nonzero(np.diff(vals[idx]) < np.timedelta64(0, "s"))[0]
            cut = back[0] + 1 if len(back) else len(idx)
            dst[idx[:cut]] = True
    loc = naive.dt.tz_localize(LOCAL_TZ, ambiguous=dst, nonexistent="raise")
    out = pd.DatetimeIndex(loc).tz_convert("UTC").as_unit("s").asi8.astype(np.int64)
    if offset is not None:  # an explicit offset in the export wins
        has = offset.to_numpy() != ""
        if has.any():
            off = pd.to_timedelta(offset[has].map(lambda x: f"{x[0]}{x[1:3]}:{x[4:6]}:00"))
            out[has] = pd.DatetimeIndex(naive[has] - off).as_unit("s").asi8
    return out


# ------------------------------------------------------------------ parser

def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.I)


# Threat types. A post can have several.
TYPES: dict[str, re.Pattern] = {
    "all_clear": _rx(r"відбій", r"отбой", r"🟢"),
    "alert": _rx(r"повітрян\w* тривог", r"(?<!відбій )\bтривога\b", r"🚨"),
    "ballistic": _rx(r"баліст", r"баллист", r"іскандер", r"искандер", r"кинджал", r"кинжал", r"\bkn-?23", r"\bкн-?23", r"\bс-?[34]00\b", r"циркон"),
    "missile": _rx(r"ракет", r"крилат", r"крылат", r"калібр", r"калибр", r"онікс", r"\bх-?(101|555|22|59|69|31|35|47)\b", r"швидкісн", r"скоростн\w* цел", r"\bкр\b", r"\bкар\b"),
    "kab": _rx(r"\bкаб(и|ів|ами|ах|ом|у|а)?\b", r"\bkab\b", r"умпк", r"керован\w* авіа\w* ?бомб", r"управляем\w* авиа\w* ?бомб"),
    "aviation": _rx(r"\bміг-?31", r"\bмиг-?31", r"\bту-?(95|160|22)", r"\bсу-?(24|25|30|34|35)", r"тактичн\w* авіац", r"стратегічн\w* авіац", r"\bзліт\b", r"злет(ів|іли)", r"\bборт(и|ів)\b"),
    "drone": _rx(r"бпла", r"\bбла\b", r"безпілот", r"беспилот", r"шахед", r"шахід", r"shahed", r"герань", r"гербер", r"мопед", r"\bдрон", r"реактив", r"🛵", r"🛸", r"🏍"),
    "artillery": _rx(r"обстріл", r"обстрел", r"артилер", r"рсзв"),
}
THREATS = ("drone", "missile", "ballistic", "kab", "aviation", "artillery")

# Oblast references: name, adjective, "-shchyna" form, centre, large towns, raion
# centres often named in threat posts. Russian spellings for the 2022 posts.
OBLASTS: dict[str, re.Pattern] = {
    "kyiv_city": _rx(r"\bкиїв(?!щин|ськ)", r"\bкиєв", r"\bкиев(?!щин|ск)", r"столиц", r"жулян", r"оболон", r"троєщин", r"деміїв", r"позняк", r"осокорк", r"дарниц", r"голосіїв", r"печерськ", r"святошин|світошин", r"воскресенк", r"теремк", r"березняк", r"лісов\w* масив", r"виноградар", r"нивк", r"борщагівк", r"соломʼян|солом'ян|соломян", r"подільськ\w* район\w* києв"),
    "kyiv": _rx(r"київщин", r"киевщин", r"київськ\w* обл", r"київськ\w* област", r"киевск\w* обл", r"бровар", r"бориспіл|борисполь|бориспол", r"біл\w* церкв|бел\w* церкв", r"обухів|обухов", r"вишгород", r"ірпін|ирпен", r"\bбуч[аіи]\b", r"фастів|фастов", r"василь?ків", r"бородянк", r"макарів", r"переяслав", r"яготин", r"кагарлик", r"сквир", r"чорнобил|чернобыл", r"гостомел", r"українк", r"богуслав", r"миронівк", r"славутич", r"узин", r"боярк", r"лютіж", r"бузов", r"ржищ", r"гатне", r"баришівк", r"ворзел", r"димер", r"кодр", r"згурівк", r"березан", r"білогород", r"вишнев", r"ставищ"),
    "chernihiv": _rx(r"черніг", r"чернигов", r"ніжин", r"нежин", r"прилук", r"новгород-сівер", r"семенівк", r"бахмач", r"корюків", r"городн[яі]", r"мена\b|\bмени\b", r"любеч|любич"),
    "sumy": _rx(r"\bсум(и|ах|ам|ами|ськ|щин)", r"\bсуми\b", r"\bсумы\b", r"шостк", r"конотоп", r"охтирк|ахтырк", r"ромн", r"глухів|глухов", r"лебедин", r"тростян", r"путивл", r"кролевец|кролевц", r"білопіл|белопол", r"середин[аио]-буд"),
    "kharkiv": _rx(r"харків", r"харьков", r"ізюм|изюм", r"куп.янськ|купянск", r"чугуїв|чугуев", r"лозов", r"балакл", r"богодух", r"золочів|золочев", r"вовчанськ|волчанск", r"красноград|берестин", r"дергач", r"мерефа", r"люботин"),
    "poltava": _rx(r"полтав", r"кременчу", r"миргород", r"лубн", r"горішні плавні|горишни", r"гадяч", r"карлівк", r"пирятин", r"хорол"),
    "dnipropetrovsk": _rx(r"дніпропетров", r"днепропетров", r"дніпропетровщ", r"дніпровщин", r"\bдніпр(о|і|а|ом)\b", r"\bднепр(е|а|у|ом)?\b", r"крив\w+ р(і|о)г|крив\w+ розі|криворі|криворож|кривой рог", r"павлоград", r"нікопол|никопол", r"марганц|марганець", r"кам.янськ(?!-)|каменск", r"новомосковськ|самар(івськ)?\b", r"синельник", r"жовт\w* вод", r"покров\w* громад", r"червоногригорів"),
    "zaporizhzhia": _rx(r"запоріж", r"запорізьк", r"запорож", r"мелітоп|мелитоп", r"бердянськ|бердянск", r"енергодар|энергодар", r"оріхів|орехов", r"пологи", r"гуляйпол", r"вільнянськ"),
    "donetsk": _rx(r"донеч", r"донецьк", r"донецк", r"краматорськ|краматорск", r"слов.янськ|славянск", r"покровськ\b|покровська тг|покровськ(у|а|ом)", r"костянтинівк|константиновк", r"дружківк|дружковк", r"бахмут", r"добропіл|доброполь", r"лиман", r"мирноград"),
    "kherson": _rx(r"херсон", r"берислав", r"каховк", r"скадовськ", r"бериславськ"),
    "mykolaiv": _rx(r"миколаїв", r"николаев", r"первомайськ\b|первомайськ(а|у|ом)\b", r"вознесенськ", r"южноукраїнськ|южноукраинск", r"очаків|очаков", r"баштанк", r"снігурів", r"новий буг"),
    "odesa": _rx(r"одес", r"одещин", r"ізмаїл|измаил", r"білгород-дністров|белгород-днестров", r"подільськ\b|подільськ(а|у|ом)\b", r"чорноморськ|черноморск", r"\bрені\b|\bрени\b", r"затоків|затока|затоці", r"бессарабі", r"\bкілі[яї]", r"роздільн", r"балт(а|і)\b", r"арциз", r"болград"),
    "kirovohrad": _rx(r"кіровоград", r"кировоград", r"кропивниц", r"олександрі[яї]", r"александри", r"світловодськ|светловодск", r"знам.янк", r"голованівськ", r"новоукраїнк"),
    "cherkasy": _rx(r"черкас", r"черкащ", r"умань|умані|уман", r"сміл[аі]|смел[аы]", r"золотонош", r"канів|канев", r"звенигородк"),
    "vinnytsia": _rx(r"вінни", r"винниц", r"вінничч", r"жмерин", r"могилів-поділ", r"козятин", r"гайсин", r"ладижин", r"хмільник", r"тульчин"),
    "zhytomyr": _rx(r"житомир", r"коростен", r"бердичів|бердичев", r"новоград-волин|звягел", r"овруч", r"малин\b", r"коростишів", r"олевськ"),
    "khmelnytskyi": _rx(r"хмельниц", r"хмельничч", r"старокостянтинів|староконстантинов", r"старкон", r"кам.янець-поділ|каменец-подол", r"шепетівк", r"нетішин", r"славут(а|и|і)\b", r"ізяслав"),
    "rivne": _rx(r"рівне|рівного|рівному|рівнен", r"ровно|ровен", r"сарн[иа]", r"дубн", r"вараш|кузнецовськ", r"костопіл"),
    "volyn": _rx(r"волин", r"волын", r"луцьк|луцк", r"ковел", r"володимир-волин|\bволодимир(?!ськ)", r"нововолинськ", r"любомл"),
    "lviv": _rx(r"льв[іо]в", r"львов", r"дрогоб", r"червоноград|шептицьк", r"стрий|стрию", r"самбір", r"яворів", r"броди\b"),
    "ternopil": _rx(r"тернопіл", r"тернопол", r"чортків", r"кременец", r"бережан"),
    "ivano_frankivsk": _rx(r"івано-франків|ивано-франков", r"франківщ|франківськ", r"калуш", r"коломи", r"бурштин", r"прикарпатт"),
    "zakarpattia": _rx(r"закарпат", r"ужгород", r"мукачев", r"\bберегов(е|ого|ому)\b", r"хуст"),
    "chernivtsi": _rx(r"чернівец|черновц", r"чернівеччин", r"буковин", r"хотин", r"новодністровськ"),
}
assert set(OBLASTS) == set(REGION_IDS)


# Channel signatures repeated under posts ("✅ єРадар | Повітряна тривога").
_SIGNATURE = re.compile(r"^.*єрадар\s*\|.*$", re.M)


def normalise(text: str) -> str:
    t = re.sub(r"[’ʼ`´]", "'", text).lower()
    return _SIGNATURE.sub("", t)


def parse_text(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(oblast region ids referred to, threat/event types)."""
    if not text:
        return (), ()
    t = normalise(text)
    obl = tuple(r for r, rx in OBLASTS.items() if rx.search(t))
    typ = [k for k, rx in TYPES.items() if rx.search(t)]
    # The rocket emoji marks missiles, but the Air Force also uses it for guided bombs.
    if "🚀" in t and "kab" not in typ:
        typ.append("missile")
    return obl, tuple(k for k in TYPES if k in typ)


def parse_frame(df: pd.DataFrame) -> pd.DataFrame:
    parsed = [parse_text(t) for t in df["text"]]
    out = df.copy()
    out["oblasts"] = [p[0] for p in parsed]
    out["types"] = [p[1] for p in parsed]
    return out


def parser_digest() -> str:
    spec = repr([(k, v.pattern) for k, v in {**TYPES, **OBLASTS}.items()]) + _SIGNATURE.pattern + inspect.getsource(parse_text)
    return hashlib.sha256(spec.encode()).hexdigest()[:12]


def load_channel(key: str) -> pd.DataFrame:
    """Posts of one channel, parsed. The read export is cached by its digest,
    the parse by the digest of the parser's rules."""
    path = TELEGRAM_DIR / key
    digest, _ = export_sha256(path)
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    raw = DERIVED_DIR / f"{key}-{digest[:16]}.pkl"
    parsed = DERIVED_DIR / f"{key}-{digest[:16]}-{parser_digest()}.pkl"
    if parsed.exists():
        return pd.read_pickle(parsed)
    df = pd.read_pickle(raw) if raw.exists() else read_export(path)
    if not raw.exists():
        df.to_pickle(raw)
    df = parse_frame(df)
    df.to_pickle(parsed)
    return df


# ------------------------------------------------------------------ features

@dataclass
class Posts:
    """Posts of the channels used for the text features, as known up to `known_until`."""

    frames: dict[str, pd.DataFrame]  # channel -> columns ts, oblasts, types

    @classmethod
    def load(cls, channels=None, drop_edited: bool = False) -> "Posts":
        channels = tuple(CHANNELS) if channels is None else tuple(channels)
        frames = {}
        for c in channels:
            df = load_channel(c)
            if drop_edited:
                df = df[~df["edited"]]
            frames[c] = df[["ts", "oblasts", "types"]].reset_index(drop=True)
        return cls(frames)

    def as_of(self, t_sec: int) -> "Posts":
        return Posts({c: df[df["ts"] <= t_sec].reset_index(drop=True) for c, df in self.frames.items()})


def _bin_counts(ts: np.ndarray, hours: np.ndarray, weights_rows: np.ndarray | None, R: int) -> np.ndarray:
    """Counts per (row, bin) where bin i holds posts with H[i]-1h <= ts < H[i]."""
    n = len(hours)
    i = (ts - hours[0]) // 3600 + 1
    ok = (i >= 0) & (i < n)
    out = np.zeros((R, n))
    if weights_rows is None:
        np.add.at(out[0], i[ok], 1.0)
    else:
        rows, pidx = np.nonzero(weights_rows[:, ok])
        np.add.at(out, (rows, i[ok][pidx]), 1.0)
    return out


def text_features(g) -> dict[str, np.ndarray]:
    """Counts of channel posts over the last hours, per region (mentions) and for
    the country, from posts published before H. Zones take their oblast's."""
    from .features import _rolling_sum

    R, n = len(g.regions), len(g.hours)
    ridx = {r: i for i, r in enumerate(g.regions)}
    reg = {k: np.zeros((R, n)) for k in ("threat", "drone", "missile", "kab", "clear")}
    ua = {k: np.zeros((1, n)) for k in ("threat", "drone", "missile", "aviation", "posts")}
    for df in g.posts.frames.values():
        if df.empty:
            continue
        ts = df["ts"].to_numpy(np.int64)
        types = df["types"].to_numpy()
        obl = df["oblasts"].to_numpy()
        has = {k: np.array([k in t for t in types]) for k in TYPES}
        threat = np.zeros(len(df), bool)
        for k in THREATS:
            threat |= has[k]
        mention = np.zeros((R, len(df)), bool)
        for j, o in enumerate(obl):
            for r in o:
                mention[ridx[r], j] = True
        for name, m in (("threat", threat), ("drone", has["drone"]), ("missile", has["missile"] | has["ballistic"]), ("kab", has["kab"]), ("clear", has["all_clear"])):
            reg[name] += _bin_counts(ts, g.hours, mention & m[None, :], R)
        for name, m in (("threat", threat), ("drone", has["drone"]), ("missile", has["missile"] | has["ballistic"]), ("aviation", has["aviation"])):
            ua[name] += _bin_counts(ts[m], g.hours, None, 1)
        ua["posts"] += _bin_counts(ts, g.hours, None, 1)
    f = {}
    for w in (1, 3, 6, 24):
        f[f"tg_threat_{w}h"] = _rolling_sum(reg["threat"], w)
        f[f"tg_ua_threat_{w}h"] = np.repeat(_rolling_sum(ua["threat"], w), R, axis=0)
    for k in ("drone", "missile", "kab"):
        f[f"tg_{k}_3h"] = _rolling_sum(reg[k], 3)
    f["tg_clear_1h"] = reg["clear"].copy()
    nb3 = np.zeros((R, n))
    t3 = f["tg_threat_3h"]
    for r, i in ridx.items():
        nb3[i] = t3[[ridx[x] for x in NEIGHBORS[r]]].sum(axis=0)
    f["tg_nb_threat_3h"] = nb3
    for k in ("drone", "missile", "aviation"):
        f[f"tg_ua_{k}_6h"] = np.repeat(_rolling_sum(ua[k], 6), R, axis=0)
    f["tg_posts_per_day_28d"] = np.repeat(_rolling_sum(ua["posts"], 672) / 28.0, R, axis=0)
    return {k: v[g.parent] for k, v in f.items()}
