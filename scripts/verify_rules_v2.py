"""Gate: the rules v2 are written down and the parser carries them.

The macro-regions are checked against Yehor Hunia's own list (12.10.2026), written
here independently of the code; then the guide (gold.GUIDE) and README must
describe every rule, and the parser must apply each one on a fixed example.
"""

import sys

from alerts import config, gold, telegram

# Yehor: "Північ охоплює Київську, Чернігівську, Сумську, Житомирську, Полтавську та Черкаську
# області, а також місто Київ. До Сходу входять Харківська, Донецька, Луганська,
# Дніпропетровська та Запорізька. Південь - Одеська, Миколаївська, Херсонська, Вінницька,
# Кіровоградська та Запорізька разом з АР Крим. Захід - вісім областей ... До Центру -
# Вінницька, Черкаська, Кіровоградська, Полтавська та Дніпропетровська."
# Luhansk oblast and Crimea are not modelled regions.
SPEC = {
    "north": {"kyiv", "chernihiv", "sumy", "zhytomyr", "poltava", "cherkasy", "kyiv_city"},
    "east": {"kharkiv", "donetsk", "dnipropetrovsk", "zaporizhzhia"},
    "south": {"odesa", "mykolaiv", "kherson", "vinnytsia", "kirovohrad", "zaporizhzhia"},
    "west": {"volyn", "rivne", "lviv", "ternopil", "khmelnytskyi", "ivano_frankivsk", "zakarpattia", "chernivtsi"},
    "centre": {"vinnytsia", "cherkasy", "kirovohrad", "poltava", "dnipropetrovsk"},
}
TWO = {"vinnytsia", "kirovohrad", "cherkasy", "poltava", "dnipropetrovsk", "zaporizhzhia"}

EXAMPLES = [  # text, expected oblasts (None = not checked), expected types (None = not checked), summary
    ("⚪️ Відбій на півдні.", SPEC["south"], {"all_clear"}, False),
    ("Відбій західні, центральні області", SPEC["west"] | SPEC["centre"], {"all_clear"}, False),
    ("🛵 БпЛА на півночі Сумщини ➡️ курсом на південь.", {"sumy"}, {"drone"}, False),
    ("🛵 БпЛА з півночі курсом на Київ!", {"kyiv_city"}, {"drone"}, False),
    ("📡 Обстановка станом на 00:00 28.02.25\n\n— Стратегічна авіація:\nНе активна;\n\n— БпЛА:\nТриває загроза ударних БпЛА;\n\n— Флот:\nРакетоносії заведені в порти базування.", set(), {"drone"}, True),
    ("⚡️ ЗБИТО/ПОДАВЛЕНО 115 ВОРОЖИХ БПЛА", None, {"drone"}, True),
    ("БпЛА-розвідник НАТО RQ-4BGH над Чорним морем зараз.", set(), set(), False),
    ("❗️ Вихід у напрямку Павлоград", {"dnipropetrovsk"}, {"ballistic", "missile"}, False),
    ("1 група блукає Тернопільщиною", {"ternopil"}, {"drone"}, False),
    ("Без змін, 3х БпЛА над київським водосховищем.", {"kyiv", "kyiv_city"}, {"drone"}, False),
    ("⚠️ Останній 1х БпЛА на Зазим'є, передмістя Києва.", {"kyiv", "kyiv_city"}, {"drone"}, False),
    ("🏍 Лук'янівка!", {"kyiv_city"}, {"drone"}, False),
    ("🏍 Реактивний БпЛА на Козин", {"kyiv"}, {"drone"}, False),
    ("❗️ Баражуючі боєприпаси \"Бандероль\" через Сумщину у напрямку Полтавщини.", {"sumy", "poltava"}, None, False),
]
# Bandarol stays a drone: it must not become a new type.
README_PHRASES = ["Rules v2", "macro-region", "summar", "inactive", "context", "Bandarol"]


def main() -> int:
    err = []
    got = {k: set(v) for k, v in telegram.MACRO.items()}
    if got != SPEC:
        err.append(f"macro-regions differ from Yehor's list: {got} vs {SPEC}")
    count = {}
    for v in SPEC.values():
        for r in v:
            count[r] = count.get(r, 0) + 1
    if {r for r, n in count.items() if n == 2} != TWO:
        err.append("the six two-region oblasts differ")
    if set(telegram.TYPES) != set(gold.TYPES):
        err.append("parser types differ from the gold types (a new type was added?)")
    for word in ("Rules v2", "macro-regions", "summary", "inactive", "context", "Bandarol", "передмістя Києва"):
        if word not in gold.GUIDE:
            err.append(f"guide misses {word!r}")
    readme = (config.ROOT / "README.md").read_text()
    for word in README_PHRASES:
        if word not in readme:
            err.append(f"README misses {word!r}")
    if telegram.parse_post("❗️ Бандероль на Полтавщину")[1] not in ((), ("drone",)):
        err.append("Bandarol parsed as something other than a drone or nothing")
    for text, obl, typ, summ in EXAMPLES:
        o, t, s = telegram.parse_post(text)
        if obl is not None and set(o) != obl:
            err.append(f"{text[:40]!r}: oblasts {sorted(o)} != {sorted(obl)}")
        if typ is not None and set(t) != typ:
            err.append(f"{text[:40]!r}: types {t} != {sorted(typ)}")
        if s != summ:
            err.append(f"{text[:40]!r}: summary {s} != {summ}")
    if err:
        print("\n".join(err))
        return 1
    print(f"macro-regions as listed; {len(EXAMPLES)} rule examples parsed as expected")
    print("RULES V2 VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
