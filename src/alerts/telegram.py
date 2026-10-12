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
    "alert": _rx(r"повітрян\w* тривог", r"(?<!відбій )\bтривога\b", r"(жовт|червон)\w* рів\w* тривог", r"🚨"),
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
    "kyiv": _rx(r"київщин", r"киевщин", r"київськ\w* обл", r"київськ\w* област", r"киевск\w* обл", r"бровар", r"бориспіл|борисполь|бориспол", r"біл\w* церкв|бел\w* церкв", r"обухів|обухов", r"вишгород", r"ірпін|ирпен", r"\bбуч[аіи]\b", r"фастів|фастов", r"василь?ків", r"бородянк", r"макарів", r"переяслав", r"яготин", r"кагарлик", r"сквир", r"чорнобил|чернобыл", r"гостомел", r"(?<!ново)українк", r"богуслав", r"миронівк", r"славутич", r"узин", r"боярк", r"лютіж", r"бузов", r"ржищ", r"гатне", r"баришівк", r"ворзел", r"димер", r"кодр", r"згурівк", r"березан", r"білогород", r"вишнев", r"ставищ"),
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
    "volyn": _rx(r"волин", r"волын", r"\bлуцьк|\bлуцк", r"ковел", r"володимир-волин|\bволодимир(?!ськ)", r"нововолинськ", r"любомл"),
    "lviv": _rx(r"льв[іо]в", r"львов", r"дрогоб", r"червоноград|шептицьк", r"стрий|стрию", r"самбір", r"яворів", r"броди\b"),
    "ternopil": _rx(r"тернопіл", r"тернопол", r"чортків", r"кременец", r"бережан"),
    "ivano_frankivsk": _rx(r"івано-франків|ивано-франков", r"франківщ|франківськ", r"калуш", r"коломи", r"бурштин", r"прикарпатт"),
    "zakarpattia": _rx(r"закарпат", r"ужгород", r"мукачев", r"\bберегов(е|ого|ому)\b", r"хуст"),
    "chernivtsi": _rx(r"чернівец|черновц", r"чернівеччин", r"буковин", r"хотин", r"новодністровськ"),
}
assert set(OBLASTS) == set(REGION_IDS)


# Channel signatures repeated under posts ("✅ єРадар | Повітряна тривога").
_SIGNATURE = re.compile(r"^.*єрадар\s*\|.*$", re.M)

# ---- rules v2 (Yehor Hunia's review of 250 gold posts, 2026-10-12)

# Macro-regions as Yehor defined them; six oblasts belong to two regions.
# Crimea and Luhansk oblast are not among the model's regions.
MACRO: dict[str, tuple[str, ...]] = {
    "north": ("kyiv", "kyiv_city", "chernihiv", "sumy", "zhytomyr", "poltava", "cherkasy"),
    "east": ("kharkiv", "donetsk", "dnipropetrovsk", "zaporizhzhia"),
    "south": ("odesa", "mykolaiv", "kherson", "vinnytsia", "kirovohrad", "zaporizhzhia"),
    "west": ("volyn", "rivne", "lviv", "ternopil", "khmelnytskyi", "ivano_frankivsk", "zakarpattia", "chernivtsi"),
    "centre": ("vinnytsia", "cherkasy", "kirovohrad", "poltava", "dnipropetrovsk"),
}
_MACRO_ADJ = {"північн": "north", "східн": "east", "південн": "south", "західн": "west", "центральн": "centre"}
_MACRO_NOUN = {"півноч": "north", "півн": "north", "сход": "east", "півдн": "south", "заход": "west", "центр": "centre"}
# "західні, центральні області", "для південних областей", "у центральних регіонах"
_MACRO_ADJ_RX = re.compile(r"(?<![-\w])((?:північн|східн|південн|західн|центральн)(?:і|их|ими|ім)(?:\s*(?:,|та|і|й)\s*(?:північн|східн|південн|західн|центральн)(?:і|их|ими|ім))*)\s+(?:област|регіон)")
# "на півдні", "по Півдню", "на півдні та сході" -- but not "на півночі Сумщини" or "з півночі"
_MACRO_NOUN_RX = re.compile(r"\b(?:на|по)\s+((?:півдн|півноч|сход|заход|центр)(?:і|у|ю|ові)\b(?:\s*(?:,|та|і|й)\s*(?:півдн|півноч|сход|заход|центр)(?:і|у|ю)\b)*)")
_NOT_MACRO_NEXT = re.compile(r"^(від|м\.|міст|област|район|сумщ|черніг|харків|київ|полтав|житомир|одещ|миколаїв|херсон|запоріж|дніпр|донеч|луган|черкащ|кіровоград|віннич|хмельнич|рівн|волин|львів|тернопіл|франків|закарпат|буковин|криму|\w+щин)")

# A type mentioned as inactive or absent is not a type of the post ("Стратегічна авіація не активна",
# "Флот без активності", "Ракетоносії заведені в порти", the NATO reconnaissance drone).
_NEGATION = re.compile(r"не\s+активн|неактивн|без\s+активност|заведен\w*\s+(?:в|у)\s+(?:порт|пункт)|не\s+перебува|не\s+фіксув|відсутн|\bнато\b")
# "Вихід" announces a ballistic or cruise missile launch; "група(и)" are drones.
_IMPLIED = (
    (re.compile(r"\bвих(?:ід|оди|оду)\b"), ("ballistic", "missile")),
    (re.compile(r"\bгруп(?:а|и|у|ою)\b"), ("drone",)),
)
# Reservoir and suburbs: both Kyiv city and Kyiv oblast.
_KYIV_BOTH = re.compile(r"київськ\w*\s+водосховищ|передмісті?\w*\s+києва|києв\w*\s+(?:та|і)\s+передміст|київ\s+(?:та|і)\s+передміст|передмістя")
# Kyiv neighbourhoods and ambiguous names Yehor resolved: Лук'янівка is Kyiv, Козин is Kyiv oblast.
_KYIV_CITY_EXTRA = re.compile(r"лук'янівк|караваєв\w*\s+дач|пріорк|сирц|шулявк|пущ\w*[-\s]водиц|чоколівк|куренівк|відрадн\w*|нов\w*\s+забудов|рибальськ|дврз|гнідин(?!.*київщин)")
_KYIV_OBL_EXTRA = re.compile(r"\bкозин|зазим|гаврилівк|глевах|пирогів(?!.*києв)|погреб")
# Summaries after an attack or about the day: not a live signal (Yehor: "це самарі").
_SUMMARY = re.compile(
    r"збито\s*/\s*подавлено|обстановка станом на|за минулу добу|оцінка діяльності|у ніч на \d+ \w+ \(з 18:00|"
    r"станом на \d\d:\d\d,? (?:протиповітряною|ппо)|знищено (?:шість|\d+|два|три|чотири|п.ять)|\bстратком|"
    r"\bураження\b.*(?:аеродром|гелікоптер|літак)|пропустив\w*\s+\w*\s*(?:повітрян\w* )?тривог|знищено ворож\w* бпла|збито\s+\d+"
)
CONTEXT_WINDOW_SEC = 3600  # a post without a type takes the types of the channel's last typed post within an hour


def normalise(text: str) -> str:
    t = re.sub(r"[’ʼ`´]", "'", text).lower()
    return _SIGNATURE.sub("", t)


def _macro(t: str) -> set[str]:
    out: set[str] = set()
    for m in _MACRO_ADJ_RX.finditer(t):
        for stem, name in _MACRO_ADJ.items():
            if stem in m.group(1):
                out |= set(MACRO[name])
    for m in _MACRO_NOUN_RX.finditer(t):
        # "на півночі Сумщини", "на півночі та в центрі Сумщини", "на Півдні та Сході (Суми, Харків...)":
        # a direction inside a named oblast or a list of named cities is not a macro-region
        after = t[m.end(1):m.end(1) + 60].split()[:4]
        if after and (after[0].startswith("(") or any(_NOT_MACRO_NEXT.match(w) or any(rx.match(w) for rx in OBLASTS.values()) for w in after)):
            continue
        for stem, name in _MACRO_NOUN.items():
            if re.search(r"\b" + stem, m.group(1)):
                out |= set(MACRO[name])
    return out


def _types(t: str) -> list[str]:
    """Types of the text, dropping those that occur only in clauses that call them inactive or absent."""
    clauses = [c for c in re.split(r"[\n;.!]+", t) if c.strip()]
    keep: set[str] = set()
    negated: set[str] = set()
    for c in clauses:
        found = {k for k, rx in TYPES.items() if rx.search(c)}
        for rx, implied in _IMPLIED:
            if rx.search(c):
                found |= set(implied)
        if "🚀" in c and "kab" not in found:
            found.add("missile")
        if "флот" in c or "ракетоносі" in c:
            fleet = {"missile"}
        else:
            fleet = set()
        if _NEGATION.search(c):
            negated |= found | fleet
        else:
            keep |= found
    # a section header ("— Стратегічна авіація:") followed by "Не активна;" negates the header's type
    for head, nxt in zip(clauses, clauses[1:]):
        if head.rstrip().endswith(":") and _NEGATION.search(nxt):
            hk = {k for k, rx in TYPES.items() if rx.search(head)}
            if "флот" in head:
                hk.add("missile")
            keep -= hk
            negated |= hk
    return [k for k in TYPES if k in keep]


def parse_post(text: str) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """(oblast region ids referred to, threat/event types, is a summary) of one post on its own."""
    if not text:
        return (), (), False
    t = normalise(text)
    obl = {r for r, rx in OBLASTS.items() if rx.search(t)}
    obl |= _macro(t)
    if _KYIV_BOTH.search(t):
        obl |= {"kyiv", "kyiv_city"}
    if _KYIV_CITY_EXTRA.search(t):
        obl.add("kyiv_city")
    if _KYIV_OBL_EXTRA.search(t):
        obl.add("kyiv")
    typ = _types(t)
    return tuple(r for r in OBLASTS if r in obl), tuple(typ), bool(_SUMMARY.search(t))


def parse_text(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(oblast region ids referred to, threat/event types) of one post on its own, without context."""
    obl, typ, _ = parse_post(text)
    return obl, typ


def parse_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Parse every post; a post that names a place but no type at all takes the threat types of the
    channel's last earlier non-summary post with a threat type within CONTEXT_WINDOW_SEC
    (earlier posts only, so no look-ahead). Needs columns text and ts."""
    parsed = [parse_post(t) for t in df["text"]]
    out = df.copy()
    out["oblasts"] = [p[0] for p in parsed]
    types = [p[1] for p in parsed]
    out["summary"] = [p[2] for p in parsed]
    ctx = [False] * len(df)
    if "ts" in df.columns:
        order = np.argsort(df["ts"].to_numpy(), kind="stable")
        ts = df["ts"].to_numpy()
        last_t, last_types = None, ()
        for j in order:
            own = [k for k in types[j] if k in THREATS]
            # only a post with no type at all: an alert or all-clear announcement is not a threat of unknown type
            if parsed[j][0] and not types[j] and not parsed[j][2]:
                if last_t is not None and ts[j] - last_t <= CONTEXT_WINDOW_SEC:
                    types[j] = tuple(k for k in TYPES if k in set(types[j]) | set(last_types))
                    ctx[j] = True
            elif own and not parsed[j][2]:
                last_t, last_types = ts[j], tuple(own)
    out["types"] = types
    out["types_from_context"] = ctx
    return out


def parser_digest() -> str:
    spec = repr([(k, v.pattern) for k, v in {**TYPES, **OBLASTS}.items()]) + repr(MACRO) + repr(_IMPLIED) + repr(CONTEXT_WINDOW_SEC)
    for rx in (_SIGNATURE, _MACRO_ADJ_RX, _MACRO_NOUN_RX, _NOT_MACRO_NEXT, _NEGATION, _KYIV_BOTH, _KYIV_CITY_EXTRA, _KYIV_OBL_EXTRA, _SUMMARY):
        spec += rx.pattern
    for fn in (parse_post, parse_frame, _types, _macro):
        spec += inspect.getsource(fn)
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

    frames: dict[str, pd.DataFrame]  # channel -> columns ts, oblasts, types, summary

    @classmethod
    def load(cls, channels=None, drop_edited: bool = False) -> "Posts":
        channels = tuple(CHANNELS) if channels is None else tuple(channels)
        frames = {}
        for c in channels:
            df = load_channel(c)
            if drop_edited:
                df = df[~df["edited"]]
            frames[c] = df[["ts", "oblasts", "types", "summary"]].reset_index(drop=True)
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
    the country, from posts published before H. Zones take their oblast's.
    Summaries (reports after an attack, daily overviews) are not live signals: they are
    left out of the threat counts and counted separately over the last 24 hours."""
    from .features import _rolling_sum

    R, n = len(g.regions), len(g.hours)
    ridx = {r: i for i, r in enumerate(g.regions)}
    reg = {k: np.zeros((R, n)) for k in ("threat", "drone", "missile", "kab", "clear", "summary")}
    ua = {k: np.zeros((1, n)) for k in ("threat", "drone", "missile", "aviation", "posts", "summary")}
    for df in g.posts.frames.values():
        if df.empty:
            continue
        ts = df["ts"].to_numpy(np.int64)
        types = df["types"].to_numpy()
        obl = df["oblasts"].to_numpy()
        summ = df["summary"].to_numpy(bool) if "summary" in df.columns else np.zeros(len(df), bool)
        live = ~summ
        has = {k: np.array([k in t for t in types]) & live for k in TYPES}
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
        reg["summary"] += _bin_counts(ts, g.hours, mention & summ[None, :], R)
        ua["summary"] += _bin_counts(ts[summ], g.hours, None, 1)
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
    f["tg_summary_24h"] = _rolling_sum(reg["summary"], 24)
    f["tg_ua_summary_24h"] = np.repeat(_rolling_sum(ua["summary"], 24), R, axis=0)
    f["tg_posts_per_day_28d"] = np.repeat(_rolling_sum(ua["posts"], 672) / 28.0, R, axis=0)
    return {k: v[g.parent] for k, v in f.items()}


# ------------------------------------------------------------------ pinning

def pin(key: str, export_dir) -> dict:
    """Copy an export's message files to data/raw/telegram/<key> and record its
    digest and post count in data_manifest.json."""
    import json
    import shutil

    if key not in CHANNELS:
        raise ValueError(f"unknown channel {key}")
    dst = TELEGRAM_DIR / key
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    files = export_files(export_dir)
    if not files:
        raise ValueError(f"{export_dir}: no messages*.html")
    for f in files:
        shutil.copy2(f, dst / os.path.basename(f))
    df = read_export(dst)
    digest, size = export_sha256(dst)
    entry = {
        "handle": CHANNELS[key].handle,
        "export": os.path.basename(os.path.normpath(str(export_dir))),
        "format": "Telegram Desktop HTML export, local time Europe/Warsaw",
        "sha256": digest,
        "bytes": size,
        "n_files": len(files),
        "n_posts": int(len(df)),
        "first_id": int(df["id"].min()),
        "last_id": int(df["id"].max()),
        "first_post_local": df["ts_local"].iloc[0],
        "last_post_local": df["ts_local"].iloc[-1],
        "edited_posts": int(df["edited"].sum()),
    }
    m = json.loads(config.MANIFEST_PATH.read_text())
    m.setdefault("telegram", {})[key] = entry
    config.MANIFEST_PATH.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    return entry
