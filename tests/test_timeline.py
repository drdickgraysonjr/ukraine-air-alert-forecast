import numpy as np
import pandas as pd

from alerts import features as F
from alerts import timeline as T
from alerts.translit import raion_key, transliterate


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def rows(spec):
    """spec: list of (unit_level, unit, start, end-or-None)."""
    return pd.DataFrame(
        {
            "region": "sumy",
            "unit_level": [x[0] for x in spec],
            "unit": [x[1] for x in spec],
            "raion": [x[1] if x[0] == "raion" else "" for x in spec],
            "started_at": pd.to_datetime([x[2] for x in spec], utc=True),
            "finished_at": pd.to_datetime([x[3] for x in spec], utc=True),
            "level": "single",
            "source": "test",
        }
    )


def test_union_merges_overlaps_and_touching():
    s, e = T.union(np.array([10, 0, 30, 20]), np.array([25, 10, 40, 22]))
    assert s.tolist() == [0, 30] and e.tolist() == [25, 40]


def test_merge_gaps_is_inclusive_and_causal():
    s, e = T.merge_gaps(np.array([0, 100, 400]), np.array([50, 200, 500]), 50)
    assert s.tolist() == [0, 400] and e.tolist() == [200, 500]


def test_covered_before_partial_and_open():
    s, e = np.array([0, 100]), np.array([50, T.OPEN_END])
    got = T.covered_before(s, e, np.array([-5, 25, 75, 150]))
    assert got.tolist() == [0, 25, 50, 100]


def test_active_at_half_open():
    s, e = np.array([10]), np.array([20])
    assert T.active_at(s, e, np.array([9, 10, 19, 20])).tolist() == [False, True, True, False]


def test_three_states_raion_union_to_oblast():
    # sumy has 5 raions; all five active => whole; one active => part.
    names = ["Сумський район", "Конотопський район", "Шосткинський район", "Роменський район", "Охтирський район"]
    spec = [("raion", n, "2026-01-01 00:00", "2026-01-01 02:00") for n in names]
    spec.append(("raion", names[0], "2026-01-01 03:00", "2026-01-01 04:00"))
    spec.append(("oblast", "", "2026-01-01 05:00", "2026-01-01 06:00"))
    tl = T.build_timeline(rows(spec), "sumy", 120)
    x = np.array([T.to_sec(ts(t)) for t in ["2026-01-01 01:00", "2026-01-01 03:30", "2026-01-01 05:30", "2026-01-01 07:00"]])
    assert tl.state_at(x).tolist() == [T.WHOLE, T.PART, T.WHOLE, T.NONE]


def test_same_raion_in_both_languages_counts_once():
    spec = [("raion", "Konotopskyi raion", "2026-01-01 00:00", "2026-01-01 02:00"),
            ("raion", "Конотопський район", "2026-01-01 01:00", "2026-01-01 03:00")]
    tl = T.build_timeline(rows(spec), "sumy", 120)
    assert len(tl.raions) == 1


def test_as_of_hides_future_finish():
    a = rows([("oblast", "", "2026-01-01 00:00", "2026-01-01 05:00"), ("oblast", "", "2026-01-01 06:00", "2026-01-01 07:00")])
    k = T.as_of(a, ts("2026-01-01 01:00"))
    assert len(k) == 1 and pd.isna(k["finished_at"].iloc[0])


def _grid_for(spec, data_end, start):
    a = rows(spec)
    return F.build_grid(a, ts(data_end), start=ts(start))


def test_daily_hours_clipped_at_kyiv_midnight():
    # 2026-06-10 22:00 .. 06-11 03:00 Kyiv (UTC+3) => 19:00 .. 00:00 UTC
    g = _grid_for([("oblast", "", "2026-06-10 19:00", "2026-06-11 00:00")], "2026-06-13 00:00", "2026-06-01 00:00")
    d = F.daily_targets(g, "2026-06-10", "2026-06-11").query("region_id == 'sumy'").set_index("day")
    assert d.loc["2026-06-10", "hours"] == 2.0 and d.loc["2026-06-11", "hours"] == 3.0
    # counted once, on the day it started
    assert d.loc["2026-06-10", "count"] == 1 and d.loc["2026-06-11", "count"] == 0


def test_dst_days_have_23_and_25_hours():
    g = _grid_for([("oblast", "", "2026-03-28 00:00", "2026-03-30 00:00")], "2026-11-01 00:00", "2026-03-20 00:00")
    d = F.daily_targets(g, "2026-03-29", "2026-03-29").query("region_id == 'sumy'")
    assert d["day_hours"].iloc[0] == 23 and d["hours"].iloc[0] == 23
    g = _grid_for([("oblast", "", "2026-10-24 00:00", "2026-10-27 00:00")], "2026-11-01 00:00", "2026-10-20 00:00")
    d = F.daily_targets(g, "2026-10-25", "2026-10-25").query("region_id == 'sumy'")
    assert d["day_hours"].iloc[0] == 25 and d["hours"].iloc[0] == 25


def test_any_gap_starts_a_new_alert_touching_does_not():
    spec = [("oblast", "", "2026-06-10 06:00", "2026-06-10 07:00"),
            ("oblast", "", "2026-06-10 07:01", "2026-06-10 08:00"),  # 1 min gap: a new alert
            ("raion", "Сумський район", "2026-06-10 08:00", "2026-06-10 08:30"),  # touching: same alert
            ("oblast", "", "2026-06-10 09:00", "2026-06-10 10:00")]
    g = _grid_for(spec, "2026-06-13 00:00", "2026-06-01 00:00")
    d = F.daily_targets(g, "2026-06-10", "2026-06-10").query("region_id == 'sumy'")
    assert d["count"].iloc[0] == 3


def test_hourly_target_masks_active_and_labels_next_start():
    spec = [("oblast", "", "2026-06-10 06:30", "2026-06-10 07:30")]
    g = _grid_for(spec, "2026-06-13 00:00", "2026-06-01 00:00")
    y = F.hourly_targets(g)
    k = g.regions.index("sumy")
    col = {int(h): i for i, h in enumerate(g.hours)}

    def at(s):
        return col[int(T.to_sec(ts(s)))]

    assert y[1][k, at("2026-06-10 06:00")] == 1.0  # starts within the next hour
    assert y[1][k, at("2026-06-10 05:00")] == 0.0
    assert y[2][k, at("2026-06-10 05:00")] == 1.0
    assert np.isnan(y[1][k, at("2026-06-10 07:00")])  # alert already active: no forecast
    assert np.isnan(y[6][k, at("2026-06-12 20:00")])  # window runs past the data end


def test_transliteration_standard():
    assert transliterate("Яворівський") == "Yavorivskyi"
    assert transliterate("Згурівка") == "Zghurivka"
    assert transliterate("Новгород-Сіверський") == "Novhorod-Siverskyi"
    assert raion_key("Krasnohradskyi raion") == raion_key("Берестинський район")
