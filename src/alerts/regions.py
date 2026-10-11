"""The 24 modelled regions, their names in both sources, raion counts, borders."""

from __future__ import annotations

# id: (official English name, alerts.in.ua Ukrainian name, display name, raions)
# Raion counts follow the 2020 administrative reform; both sources list exactly
# these raions (checked by scripts/verify_stitch.py).
REGIONS: dict[str, tuple[str, str, str, int]] = {
    "cherkasy": ("Cherkaska oblast", "Черкаська область", "Cherkasy", 4),
    "chernihiv": ("Chernihivska oblast", "Чернігівська область", "Chernihiv", 5),
    "chernivtsi": ("Chernivetska oblast", "Чернівецька область", "Chernivtsi", 3),
    "dnipropetrovsk": ("Dnipropetrovska oblast", "Дніпропетровська область", "Dnipropetrovsk", 7),
    "donetsk": ("Donetska oblast", "Донецька область", "Donetsk", 8),
    "ivano_frankivsk": ("Ivano-Frankivska oblast", "Івано-Франківська область", "Ivano-Frankivsk", 6),
    "kharkiv": ("Kharkivska oblast", "Харківська область", "Kharkiv", 7),
    "kherson": ("Khersonska oblast", "Херсонська область", "Kherson", 5),
    "khmelnytskyi": ("Khmelnytska oblast", "Хмельницька область", "Khmelnytskyi", 3),
    "kirovohrad": ("Kirovohradska oblast", "Кіровоградська область", "Kirovohrad", 4),
    "kyiv_city": ("Kyiv City", "м. Київ", "Kyiv City", 0),
    "kyiv": ("Kyivska oblast", "Київська область", "Kyiv oblast", 7),
    "lviv": ("Lvivska oblast", "Львівська область", "Lviv", 7),
    "mykolaiv": ("Mykolaivska oblast", "Миколаївська область", "Mykolaiv", 4),
    "odesa": ("Odeska oblast", "Одеська область", "Odesa", 7),
    "poltava": ("Poltavska oblast", "Полтавська область", "Poltava", 4),
    "rivne": ("Rivnenska oblast", "Рівненська область", "Rivne", 4),
    "sumy": ("Sumska oblast", "Сумська область", "Sumy", 5),
    "ternopil": ("Ternopilska oblast", "Тернопільська область", "Ternopil", 3),
    "vinnytsia": ("Vinnytska oblast", "Вінницька область", "Vinnytsia", 6),
    "volyn": ("Volynska oblast", "Волинська область", "Volyn", 4),
    "zakarpattia": ("Zakarpatska oblast", "Закарпатська область", "Zakarpattia", 6),
    "zaporizhzhia": ("Zaporizka oblast", "Запорізька область", "Zaporizhzhia", 5),
    "zhytomyr": ("Zhytomyrska oblast", "Житомирська область", "Zhytomyr", 4),
}

REGION_IDS: tuple[str, ...] = tuple(REGIONS)

# Not modelled: a single uninterrupted alert since 2022 in both regions.
EXCLUDED_OFFICIAL = ("Luhanska oblast",)
EXCLUDED_ALERTS_IN_UA = ("Луганська область", "Автономна Республіка Крим")

OFFICIAL_TO_ID = {v[0]: k for k, v in REGIONS.items()}
UA_TO_ID = {v[1]: k for k, v in REGIONS.items()}


def raion_count(region: str) -> int:
    return REGIONS[region][3]


# Land borders between modelled regions (one direction listed; made symmetric).
_BORDERS = {
    "vinnytsia": ["zhytomyr", "kyiv", "cherkasy", "kirovohrad", "odesa", "khmelnytskyi", "chernivtsi"],
    "volyn": ["rivne", "lviv"],
    "dnipropetrovsk": ["poltava", "kharkiv", "donetsk", "zaporizhzhia", "kherson", "mykolaiv", "kirovohrad"],
    "donetsk": ["kharkiv", "zaporizhzhia"],
    "zhytomyr": ["rivne", "khmelnytskyi", "kyiv"],
    "zakarpattia": ["lviv", "ivano_frankivsk"],
    "zaporizhzhia": ["kherson"],
    "ivano_frankivsk": ["lviv", "ternopil", "chernivtsi"],
    "kyiv": ["cherkasy", "poltava", "chernihiv", "kyiv_city"],
    "kirovohrad": ["cherkasy", "poltava", "mykolaiv", "odesa"],
    "lviv": ["rivne", "ternopil"],
    "mykolaiv": ["odesa", "kherson"],
    "poltava": ["chernihiv", "sumy", "kharkiv", "cherkasy"],
    "rivne": ["ternopil", "khmelnytskyi"],
    "sumy": ["chernihiv", "kharkiv"],
    "ternopil": ["chernivtsi", "khmelnytskyi"],
    "khmelnytskyi": ["chernivtsi"],
}


def _symmetric(borders: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    out: dict[str, set[str]] = {r: set() for r in REGION_IDS}
    for a, bs in borders.items():
        for b in bs:
            out[a].add(b)
            out[b].add(a)
    return {r: tuple(sorted(n)) for r, n in out.items()}


NEIGHBORS: dict[str, tuple[str, ...]] = _symmetric(_BORDERS)
