import json

import pandas as pd
import pytest

from alerts import config, neptun as N

# A small alerts.in.ua catalog so the tests do not need the pinned CSV.
CATALOG = (
    {
        ("Донецька область", "bakhmutskyi"): ("101", "Бахмутський район"),
        ("Донецька область", "pokrovskyi"): ("102", "Покровський район"),
        ("Донецька область", "donetskyi"): ("103", "Донецький район"),
        ("Харківська область", "kupianskyi"): ("104", "Куп'янський район"),
        ("Дніпропетровська область", "samarivskyi"): ("43", "Самарівський район"),
    },
    {"м. Київ": "31", "Луганська область": "16"},
)


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    N._raion_index.cache_clear()
    monkeypatch.setattr(N, "_aiu_catalog", lambda: CATALOG)
    yield
    N._raion_index.cache_clear()


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def snap(t, raions=(), oblasts=()):
    """raions/oblasts: (key, oblast, level, since)."""
    return {
        "fetched_at": t,
        "updatedAt": t,
        "raions": [{"key": k, "name": k, "oblast": o, "since": s, "level": lv} for k, o, lv, s in raions],
        "oblasts": [{"key": k, "name": k, "oblast": o, "since": s, "level": lv} for k, o, lv, s in oblasts],
    }


B = ("бахмутський", "Донецька область")
P = ("покровський", "Донецька область")


def r(loc, level, since):
    return (loc[0], loc[1], level, since)


def rows_of(df, title):
    return df[df["location_title"] == title].reset_index(drop=True)


# ------------------------------------------------------------------ episodes

def test_disappearance_finishes_at_first_absent_fetch():
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00.123456Z")]),
        snap("2026-10-12T10:01:00Z", [r(B, "red", "2026-10-12T09:50:00.123456Z")]),
        snap("2026-10-12T10:02:00Z", []),
    ]
    e = N.episodes(lines)
    assert list(e.columns) == N.AIU_COLUMNS
    assert len(e) == 1
    row = e.iloc[0]
    assert row["started_at"] == ts("2026-10-12 09:50:00.123456")
    assert row["finished_at"] == ts("2026-10-12 10:02:00")
    assert (row["location_title"], row["location_type"], row["location_oblast"], row["location_uid"]) == ("Бахмутський район", "raion", "Донецька область", "101")
    assert row["alert_level"] == "red" and row["alert_type"] == "air_raid"


def test_still_present_is_open():
    e = N.episodes([snap("2026-10-12T10:00:00Z", [r(B, "yellow", "2026-10-12T09:00:00Z")])])
    assert len(e) == 1 and pd.isna(e.iloc[0]["finished_at"])


def test_level_change_with_new_since_is_a_handover():
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00Z")]),
        snap("2026-10-12T10:01:00Z", [r(B, "yellow", "2026-10-12T10:00:30.5Z")]),
    ]
    e = N.episodes(lines, detail=True)
    red, yellow = e[e["alert_level"] == "red"].iloc[0], e[e["alert_level"] == "yellow"].iloc[0]
    # Like alerts.in.ua after the seam: the old level finishes, the new one starts.
    assert red["finished_at"] == ts("2026-10-12 10:00:30.5") and red["finish_kind"] == "handover"
    assert yellow["started_at"] == ts("2026-10-12 10:00:30.5") and pd.isna(yellow["finished_at"])
    assert red["finish_seen_at"] == ts("2026-10-12 10:01:00")


def test_level_change_keeping_since_does_not_overlap():
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00Z")]),
        snap("2026-10-12T10:01:00Z", [r(B, "yellow", "2026-10-12T09:50:00Z")]),
    ]
    e = N.episodes(lines, detail=True)
    red, yellow = e[e["alert_level"] == "red"].iloc[0], e[e["alert_level"] == "yellow"].iloc[0]
    assert red["finished_at"] == ts("2026-10-12 10:01:00") and red["finish_kind"] == "absent"
    assert yellow["started_at"] == ts("2026-10-12 10:01:00")  # moved to the end of the red row
    assert yellow["neptun_since"] == ts("2026-10-12 09:50:00")


def test_reappearance_with_new_start_is_a_new_alert():
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00Z")]),
        snap("2026-10-12T10:01:00Z", []),
        snap("2026-10-12T10:02:00Z", [r(B, "red", "2026-10-12T10:01:40Z")]),
    ]
    e = N.episodes(lines)
    assert len(e) == 2
    assert list(e["started_at"]) == [ts("2026-10-12 09:50"), ts("2026-10-12 10:01:40")]
    assert e.iloc[0]["finished_at"] == ts("2026-10-12 10:01") and pd.isna(e.iloc[1]["finished_at"])


def test_reappearance_with_same_start_reopens_the_alert():
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00Z")]),
        snap("2026-10-12T10:01:00Z", []),
        snap("2026-10-12T10:02:00Z", [r(B, "red", "2026-10-12T09:50:00Z")]),
    ]
    e = N.episodes(lines)
    assert len(e) == 1 and pd.isna(e.iloc[0]["finished_at"])


def test_episodes_use_no_later_snapshot():
    """The table from the first k snapshots agrees with the full table on every
    finish already known at snapshot k, and leaves the rest open."""
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00Z"), r(P, "yellow", "2026-10-12T09:55:00Z")]),
        snap("2026-10-12T10:01:00Z", [r(B, "yellow", "2026-10-12T10:00:20Z"), r(P, "yellow", "2026-10-12T09:55:00Z")]),
        snap("2026-10-12T10:02:00Z", [r(B, "yellow", "2026-10-12T10:00:20Z")]),
        snap("2026-10-12T10:03:00Z", [r(B, "red", "2026-10-12T10:02:10Z"), r(P, "red", "2026-10-12T10:02:50Z")]),
        snap("2026-10-12T10:04:00Z", []),
    ]
    full = N.episodes(lines, detail=True).set_index("id")
    for k in range(1, len(lines) + 1):
        t = ts(lines[k - 1]["fetched_at"])
        pre = N.episodes(lines[:k], detail=True).set_index("id")
        assert set(pre.index) == set(full.index[full["seen_at"] <= t])
        for i, row in pre.iterrows():
            f = full.loc[i]
            assert row["started_at"] == f["started_at"]
            if pd.notna(f["finish_seen_at"]) and f["finish_seen_at"] <= t:
                assert row["finished_at"] == f["finished_at"]
            else:
                assert pd.isna(row["finished_at"])
            assert pd.isna(row["finished_at"]) or row["finished_at"] <= t


def test_rows_of_one_location_do_not_overlap():
    lines = [
        snap("2026-10-12T10:00:00Z", [r(B, "red", "2026-10-12T09:50:00Z")]),
        snap("2026-10-12T10:01:00Z", [r(B, "yellow", "2026-10-12T09:59:00Z")]),  # since before the previous poll
        snap("2026-10-12T10:02:00Z", [r(B, "red", "2026-10-12T10:01:30Z")]),
    ]
    e = N.episodes(lines).sort_values("started_at")
    ends = e["finished_at"].fillna(ts("2100-01-01")).to_numpy()
    assert (e["started_at"].to_numpy()[1:] >= ends[:-1]).all()


def test_oblast_level_and_kyiv_city():
    e = N.episodes([snap("2026-10-12T10:00:00Z", oblasts=[("м. київ", "м. Київ", "red", "2026-10-12T09:00:00Z"),
                                                          ("луганська", "Луганська область", "red", "2022-04-04T16:45:00Z")])])
    kyiv = rows_of(e, "м. Київ").iloc[0]
    assert (kyiv["location_type"], kyiv["location_oblast"], kyiv["location_uid"]) == ("oblast", "м. Київ", "31")
    n = N.normalize(e)
    assert list(n["region"]) == ["kyiv_city"]  # Luhansk is an excluded region, dropped like in data.load_alerts_in_ua
    assert n.iloc[0]["unit_level"] == "oblast" and n.iloc[0]["unit"] == ""


# ------------------------------------------------------------------ mapping

def test_unknown_keys_raise():
    with pytest.raises(N.UnmappedKey):
        N.episodes([snap("2026-10-12T10:00:00Z", [("неіснуючий", "Донецька область", "red", "2026-10-12T09:00:00Z")])])
    with pytest.raises(N.UnmappedKey):
        N.locate_raion("бахмутський", "Харківська область")  # raion of another oblast
    with pytest.raises(N.UnmappedKey):
        N.locate_oblast("атлантида")
    with pytest.raises(N.UnmappedKey):
        N.locate_raion("бахмутський", "Атлантидська область")


def test_mapping_handles_apostrophes_renames_and_excluded_regions():
    assert N.locate_raion("куп'янський").location_title == "Куп'янський район"
    assert N.locate_raion("куп’янський", "Харківська область").location_uid == "104"
    assert N.locate_raion("новомосковський").location_title == "Самарівський район"
    crimea = N.locate_raion("курманський")
    assert crimea.region is None and crimea.location_oblast == N.CRIMEA
    sev = N.locate_oblast("севастополь", "Автономна Республіка Крим")
    assert sev.region is None and sev.location_title == N.SEVASTOPOL
    assert N.locate_oblast("луганська").location_uid == "16"


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        N.episodes([snap("2026-10-12T10:00:00Z", [r(B, "purple", "2026-10-12T09:00:00Z")])])


# ------------------------------------------------------------------ polling

def test_min_poll_interval_enforced(tmp_path):
    assert N.MIN_POLL_SEC >= 5
    with pytest.raises(ValueError):
        N.record(interval=4, hours=0.01, out_dir=tmp_path)
    with pytest.raises(ValueError):
        N.record(interval=60, messages_every=4, hours=0.01, out_dir=tmp_path)


def test_recorder_survives_errors_spaces_requests_and_dedupes(tmp_path):
    clock = [0.0]
    calls = []
    alerts_n = [0]

    def sleep(s):
        assert s >= 0
        clock[0] += s

    def fa():
        calls.append(("a", clock[0]))
        alerts_n[0] += 1
        if alerts_n[0] == 2:
            raise OSError("network down")
        return {"updatedAt": "x", "version": 1, "raions": [], "oblasts": []}

    def fm():
        calls.append(("m", clock[0]))
        return {"messages": [{"channel": "@UkraineAlarmSignal", "date": "2026-10-12T10:00:00Z", "text": "тривога"}]}

    st = N.record(interval=5, hours=0.02, out_dir=tmp_path, messages_every=5, _fetch_alerts=fa, _fetch_messages=fm,
                  _sleep=sleep, _clock=lambda: clock[0])
    times = [c[1] for c in calls]
    assert all(b - a >= N.MIN_POLL_SEC for a, b in zip(times, times[1:]))
    assert st["errors"] == 1 and st["snapshots"] == alerts_n[0] - 1 >= 2
    assert st["new_messages"] == 1  # the same post fetched repeatedly is stored once
    lines = N.load_snapshots(tmp_path)
    assert len(lines) == st["snapshots"] and set(lines[0]) >= {"fetched_at", "updatedAt", "raions", "oblasts"}
    msgs, fetches = N.load_messages(tmp_path)
    assert len(msgs) == 1 and len(fetches) == st["message_fetches"]


# ------------------------------------------------------------------ live merge

def test_start_match():
    assert N.start_match("2026-10-12T10:00:00.4Z", "2026-10-12T10:00:00Z") == "exact"
    assert N.start_match("2022-04-04T16:45:00Z", "2022-04-04T16:45:39Z") == "minute"
    assert N.start_match("2026-10-12T10:00:00.5Z", "2026-10-12T10:00:39Z") is None
    assert N.start_match("2026-10-12T10:00:00Z", "2026-10-12T11:00:00Z") is None


def pinned_row(unit, start, level="red", unit_level="raion", region="donetsk", end=None):
    return {"region": region, "unit_level": unit_level, "unit": unit, "raion": unit if unit_level == "raion" else "",
            "started_at": ts(start), "finished_at": ts(end) if end else pd.NaT, "level": level, "source": "alerts_in_ua"}


def test_live_merge_continues_open_alerts_without_duplicates(monkeypatch):
    from alerts import stitch

    end = ts("2026-10-12 09:00")
    monkeypatch.setattr(config, "DATA_END_UTC", end)
    pinned = pd.DataFrame([
        pinned_row("Бахмутський район", "2026-10-12 08:00:00"),                 # still on in NEPTUN: continue
        pinned_row("Покровський район", "2026-10-12 08:30:00"),                 # absent: closed at the first snapshot
        pinned_row("Донецький район", "2026-10-12 08:40:00"),                   # absent, newer alert there: closed at its start
        pinned_row("Донецький район", "2026-10-12 07:00:00", end="2026-10-12 07:30:00"),
    ])
    monkeypatch.setattr(stitch, "load_all", lambda: pinned)
    D = ("донецький", "Донецька область")
    lines = [
        snap("2026-10-12T09:10:00Z", [r(B, "red", "2026-10-12T08:00:00.300Z"), r(D, "red", "2026-10-12T09:05:00Z")]),
        snap("2026-10-12T09:11:00Z", [r(D, "red", "2026-10-12T09:05:00Z")]),
    ]
    out, rep = N.live_alerts(lines)
    bakh = out[out["unit"] == "Бахмутський район"]
    assert len(bakh) == 1  # not duplicated
    assert bakh.iloc[0]["source"] == "alerts_in_ua" and bakh.iloc[0]["finished_at"] == ts("2026-10-12 09:11")
    pok = out[out["unit"] == "Покровський район"].iloc[0]
    assert pok["finished_at"] == ts("2026-10-12 09:10")
    don = out[out["unit"] == "Донецький район"].sort_values("started_at")
    assert list(don["finished_at"].iloc[:2]) == [ts("2026-10-12 07:30"), ts("2026-10-12 09:05")]
    assert don.iloc[2]["source"] == "neptun" and pd.isna(don.iloc[2]["finished_at"])
    assert rep["continued_matches"] == {"exact": 1, "minute": 0}
    assert len(rep["continued"]) == 1 and len(rep["closed_in_gap_region_level"]) == 2
    assert rep["gap_h"] == round(10 / 60, 2) and rep["conflicts"] == []
    # Control: a NEPTUN start shifted by an hour does not continue the alert.
    shifted = [snap("2026-10-12T09:10:00Z", [r(B, "red", "2026-10-12T09:00:00.300Z")])]
    out2, rep2 = N.live_alerts(shifted)
    assert len(out2[out2["unit"] == "Бахмутський район"]) == 2 and rep2["continued"] == []


def test_live_merge_refuses_snapshots_before_data_end(monkeypatch):
    monkeypatch.setattr(config, "DATA_END_UTC", ts("2026-10-12 09:00"))
    with pytest.raises(ValueError):
        N.live_alerts([snap("2026-10-12T08:00:00Z")])


# ------------------------------------------------------------------ posts

def test_channel_keys():
    assert N.channel_key("@UkraineAlarmSignal") == "alarm_signal"
    assert N.channel_key("@eRadarrua") == "eradar"
    assert N.channel_key("Повітряні Сили ЗСУ") == "kpszsu"
    assert N.channel_key("@pivden_varta") is None


@pytest.mark.skipif(not config.ALERTS_IN_UA.local_path.exists(), reason="pinned alerts.in.ua CSV not downloaded")
def test_normalize_matches_data_loader():
    from alerts import data

    raw = pd.read_csv(config.ALERTS_IN_UA.local_path, dtype=str, keep_default_na=False)
    a = N.normalize(raw).drop(columns="source")
    b = data.load_alerts_in_ua().drop(columns="source")
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_snapshot_line_roundtrip(tmp_path):
    line = N.snapshot_line({"updatedAt": "u", "version": 3, "raions": [], "oblasts": []}, "2026-10-12T10:00:00Z")
    (tmp_path / "alerts-2026-10-12.jsonl").write_text(json.dumps(line) + "\n")
    assert N.load_snapshots(tmp_path) == [line]
