import pandas as pd

from alerts import telegram as TG

SOUTH = set(TG.MACRO["south"])


def test_macro_regions_only_as_macro_regions():
    assert set(TG.parse_post("⚪️ Відбій на півдні.")[0]) == SOUTH
    assert set(TG.parse_post("📢 Відбій загрози ударних БпЛА для південних областей.")[0]) == SOUTH
    assert TG.parse_post("🛵 БпЛА на півночі Сумщини ➡️ курсом на південь.")[0] == ("sumy",)
    assert TG.parse_post("🛵 БпЛА з півночі курсом на Київ!")[0] == ("kyiv_city",)
    assert TG.parse_post("Тривога у північно-східних областях")[0] == ()
    assert TG.parse_post("переміщення бортів в межах Західного регіону рф")[0] == ()


def test_inactive_types_dropped_and_summary_marked():
    o, t, s = TG.parse_post("📡 Обстановка станом на 00:00\n\n— Стратегічна авіація:\nНе активна;\n\n— БпЛА:\nТриває загроза ударних БпЛА;\n\n— Флот:\nРакетоносії заведені в порти базування.")
    assert t == ("drone",) and s
    assert TG.parse_post("БпЛА-розвідник НАТО над Чорним морем")[1] == ()
    assert TG.parse_post("Стратегічна авіація: Активна. Зліт Ту-95мс")[1] == ("aviation",)


def test_context_types_from_earlier_posts_only():
    df = pd.DataFrame({"id": [1, 2, 3, 4], "ts": [0, 600, 900, 5000],
                       "text": ["🛵 БпЛА на Київ", "Деміївка.", "🚨 Київ. Повітряна тривога", "Деміївка."]})
    p = TG.parse_frame(df)
    assert p["types"].tolist()[1] == ("drone",) and p["types_from_context"].tolist()[1]
    assert p["types"].tolist()[2] == ("alert",)  # an alert announcement keeps its own type
    assert p["types"].tolist()[3] == () and not p["types_from_context"].tolist()[3]  # more than an hour later
    first = TG.parse_frame(df.iloc[[1, 0]].reset_index(drop=True).assign(ts=[0, 600]))
    assert first["types"].tolist()[0] == ()  # a later typed post does not lend its type
