"""Ukrainian -> Latin transliteration (Cabinet of Ministers resolution No 55, 2010).

Used to give raions one key across sources: the official feed names them in
English ("Konotopskyi raion"), alerts.in.ua in Ukrainian ("Конотопський район").
"""

from __future__ import annotations

_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "iu", "я": "ia", "'": "", "’": "", "ʼ": "",
}
# At the start of a word these letters take a different form.
_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def transliterate(text: str) -> str:
    out = []
    prev = ""
    for ch in text:
        low = ch.lower()
        if low in _MAP:
            word_start = prev == "" or (not prev.isalpha() and prev not in "'’ʼ")
            if word_start and low in _INITIAL:
                lat = _INITIAL[low]
            elif low == "г" and prev.lower() == "з":
                lat = "gh"  # "зг" -> "zgh"
            else:
                lat = _MAP[low]
            if ch.isupper() and lat:
                lat = lat[0].upper() + lat[1:]
            out.append(lat)
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


# Raions renamed in 2024; the official feed keeps the old names.
RENAMED = {
    "krasnohradskyi": "berestynskyi",
    "chervonohradskyi": "sheptytskyi",
    "volodymyr-volynskyi": "volodymyrskyi",
}


def _is_cyrillic(c: str) -> bool:
    return "Ѐ" <= c <= "ӿ"


def raion_key(name: str) -> str:
    """Canonical raion key from either source's spelling."""
    latin = transliterate(name) if any(_is_cyrillic(c) for c in name) else name
    key = latin.replace(" raion", "").replace(" Raion", "").strip().lower()
    return RENAMED.get(key, key)
