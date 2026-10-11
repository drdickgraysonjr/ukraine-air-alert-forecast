import numpy as np
import pandas as pd
import pytest

from alerts import telegram as TG
from alerts import zones as Z


def alert(level, unit, raion, start, end, region="dnipropetrovsk"):
    return {"region": region, "unit_level": level, "unit": unit, "raion": raion,
            "started_at": pd.Timestamp(start, tz="UTC"), "finished_at": pd.Timestamp(end, tz="UTC") if end else pd.NaT,
            "level": "single", "source": "test"}


def test_hromada_key_same_in_both_sources():
    assert Z.hromada_key("m. Nikopol ta Nikopolska terytorialna hromada") == Z.hromada_key("Нікопольська територіальна громада") == "nikopolska"
    assert Z.hromada_key("Червоногригорівська територіальна громада") == Z.hromada_key("Chervonohryhorivska terytorialna hromada")


def test_city_row_repeating_hromada_alert_is_dropped():
    a = pd.DataFrame([
        alert("hromada", "Криворізька територіальна громада", "Криворізький район", "2026-09-10 10:00:00", "2026-09-10 11:00"),
        alert("city", "м. Кривий Ріг", "", "2026-09-10 10:00:30", "2026-09-10 11:00"),
        alert("city", "м. Дніпро", "", "2026-09-10 15:00", "2026-09-10 16:00"),  # no hromada twin: kept
    ])
    u = Z.unit_alerts(a)
    assert len(u) == 2 and set(u["unit_key"]) == {"kryvorizkyi/kryvorizka", "city/m. dnipro"}


def test_membership_uses_only_previous_days():
    rows = [alert("hromada", "Нікопольська територіальна громада", "Нікопольський район", f"2026-06-{d:02d} 09:00", f"2026-06-{d:02d} 10:00") for d in range(1, 11)]
    a = Z.activity(Z.unit_alerts(pd.DataFrame(rows)), "2026-06-01", "2026-06-20")
    m = Z.membership(a, 7, 0.9)[0]
    day = list(a.days["day"])
    # Alerts on 06-01..06-10: member from 06-08 (7 full days before) through 06-11.
    assert not m[day.index("2026-06-07")]
    assert m[day.index("2026-06-08")] and m[day.index("2026-06-11")]
    assert not m[day.index("2026-06-12")]  # only 6 of the last 7 days


def test_autumn_repeated_hour_resolved_by_post_order():
    local = pd.Series(["2025-10-26 02:30:00", "2025-10-26 02:50:00", "2025-10-26 02:10:00", "2025-10-26 03:10:00"])
    got = [str(pd.Timestamp(x, unit="s", tz="UTC")) for x in TG.to_utc_seconds(local)]
    assert got == ["2025-10-26 00:30:00+00:00", "2025-10-26 00:50:00+00:00", "2025-10-26 01:10:00+00:00", "2025-10-26 02:10:00+00:00"]
    assert TG.to_utc_seconds(pd.Series(["2026-07-01 12:00:00"]))[0] == pd.Timestamp("2026-07-01 10:00", tz="UTC").timestamp()


EXPORT = """<div class="history">
<div class="message service" id="message-1"><div class="body details">11 жовтня 2026</div></div>
<div class="message default clearfix" id="message5">
<div class="body"><div class="pull_right date details" title="11 жовтня 2026, 22:04:11">22:04</div>
<div class="text">🛵 БпЛА над <strong>Кривим Рогом</strong>.<br>Увага!</div></div></div>
<div class="message default clearfix joined" id="message6">
<div class="body"><div class="pull_right date details" title="11 жовтня 2026, 22:05:00">edited 22:05</div>
<div class="text">🚀КАБи на Сумщину.</div></div></div>
</div>"""


def test_read_export_and_parse(tmp_path):
    (tmp_path / "messages.html").write_text(EXPORT, encoding="utf-8")
    df = TG.read_export(tmp_path)
    assert df["id"].tolist() == [5, 6]
    assert df["text"].iloc[0] == "🛵 БпЛА над Кривим Рогом.\nУвага!"
    assert df["edited"].tolist() == [False, True]
    assert df["ts"].iloc[0] == pd.Timestamp("2026-10-11 20:04:11", tz="UTC").timestamp()
    p = TG.parse_frame(df)
    assert p["oblasts"].tolist() == [("dnipropetrovsk",), ("sumy",)]
    assert p["types"].tolist() == [("drone",), ("kab",)]


def test_read_export_refuses_a_post_without_date(tmp_path):
    (tmp_path / "messages.html").write_text(EXPORT.replace('title="11 жовтня 2026, 22:05:00"', ""), encoding="utf-8")
    with pytest.raises(ValueError):
        TG.read_export(tmp_path)


def test_parser_signature_is_not_an_alert_and_south_is_not_odesa():
    assert TG.parse_text("Виліт Ту-95МС.\n\n✅ єРадар | Повітряна тривога") == ((), ("aviation",))
    obl, _ = TG.parse_text("🛵 Дніпро! Група дронів з південного напрямку")
    assert obl == ("dnipropetrovsk",)
    assert TG.parse_text("🟢 Донецька обл. | Відбій тривоги")[1] == ("all_clear",)


def test_text_features_use_posts_before_h_only():
    from alerts import features as F

    a = pd.DataFrame([alert("oblast", "", "", "2026-06-01 00:00", "2026-06-01 01:00", region="sumy")])
    t_post = int(pd.Timestamp("2026-06-05 10:30", tz="UTC").timestamp())
    posts = TG.Posts({"x": pd.DataFrame({"ts": [t_post], "oblasts": [("sumy",)], "types": [("drone",)]})})
    g = F.build_grid(a, pd.Timestamp("2026-06-10", tz="UTC"), start=pd.Timestamp("2026-05-20", tz="UTC"), posts=posts)
    f = F.feature_matrix(g)
    k = g.regions.index("sumy")
    col = {int(h): i for i, h in enumerate(g.hours)}
    at = lambda s: col[int(pd.Timestamp(s, tz="UTC").timestamp())]  # noqa: E731
    assert f["tg_threat_1h"][k, at("2026-06-05 10:00")] == 0
    assert f["tg_threat_1h"][k, at("2026-06-05 11:00")] == 1
    assert f["tg_threat_3h"][k, at("2026-06-05 13:00")] == 1 and f["tg_threat_3h"][k, at("2026-06-05 14:00")] == 0
    assert np.all(f["tg_threat_24h"][g.regions.index("lviv")] == 0)
