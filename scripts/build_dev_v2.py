"""Build gold/dev_v2.json: the 250 gold posts Yehor Hunia reviewed (all 200 of @kpszsu and
50 of @war_monitor), labelled under the rules v2.

Start from the reviewed label (his corrected label for "fix", the agent's label for "ok"),
then apply the rules v2 he set on 2026-10-12 where his review predates them. Every such
change is listed below with its rule. These posts were used to write parser v2, so
accuracy on them is a development figure, not a held-out one.

    uv run python scripts/build_dev_v2.py
"""

import json

from alerts import gold
from alerts.telegram import MACRO

N, E, S, W, C = (set(MACRO[k]) for k in ("north", "east", "south", "west", "centre"))

# (channel, id): (oblasts to add, oblasts to remove, types to add, types to remove, rule)
CHANGES = {
    ("kpszsu", 3766): (W | C, set(), set(), set(), "macro: західні, центральні області"),
    ("war_monitor", 5283): (S, set(), set(), set(), "macro: відбій на півдні"),
    ("war_monitor", 6368): (S, set(), set(), set(), "macro: по Півдню"),
    ("kpszsu", 8251): (S | E, set(), set(), set(), "macro: на півдні та сході"),
    ("kpszsu", 8908): (E | S, set(), set(), set(), "macro: на сході та півдні"),
    ("kpszsu", 10289): (S, set(), set(), set(), "macro: для південних областей"),
    ("war_monitor", 7531): (S, set(), set(), set(), "macro: на Півдні"),
    ("kpszsu", 62415): (N | S | E, set(), set(), set(), "macro: на півночі, півдні та сході країни"),
    ("kpszsu", 66046): (N | S | E, set(), set(), set(), "macro: на півночі, півдні та сході країни"),
    ("war_monitor", 10487): (set(), set(), set(), {"aviation", "drone"}, "inactive: авіація не активна, запуск БпЛА не фіксувався"),
    ("war_monitor", 25642): (set(), set(), set(), {"missile"}, "inactive: ракетоносії в портах"),
    ("war_monitor", 35098): (set(), set(), set(), {"missile"}, "inactive: ракетоносії в портах"),
    ("war_monitor", 41827): (set(), set(), set(), {"missile"}, "inactive: ракетоносії в портах"),
    ("war_monitor", 33871): (set(), set(), set(), {"aviation", "missile"}, "inactive: авіація не активна, ракетоносії в пунктах базування"),
    ("kpszsu", 80344): ({"kyiv_city"}, set(), set(), set(), "Kyiv reservoir: city and oblast"),
    ("kpszsu", 54413): ({"kyiv_city"}, set(), set(), set(), "Kyiv reservoir: city and oblast"),
    ("war_monitor", 11676): (set(), set(), {"ballistic", "kab"}, set(), "context: Yehor - балістика або КАБ"),
    ("war_monitor", 28682): (set(), set(), {"ballistic"}, {"drone"}, "context (Yehor: for «комбінована атака» the type comes from the previous posts; balistics approached Kyiv a minute earlier) supersedes his added drone"),
}
# Types from the channel's previous posts (rule: a post without a type takes them from context).
CONTEXT = {
    ("kpszsu", 81366): {"drone"}, ("kpszsu", 83091): {"drone"}, ("kpszsu", 83693): {"drone"},
    ("kpszsu", 84885): {"drone"}, ("kpszsu", 16331): {"missile"}, ("kpszsu", 25893): {"drone"},
    ("kpszsu", 36343): {"missile"}, ("war_monitor", 5352): {"ballistic"}, ("war_monitor", 7258): {"drone"},
    ("war_monitor", 24945): {"drone"}, ("war_monitor", 30053): {"drone"}, ("war_monitor", 31505): {"drone"},
    ("war_monitor", 34133): {"drone"}, ("war_monitor", 34763): {"drone"}, ("war_monitor", 38797): {"drone"},
    ("war_monitor", 46786): {"drone"},
}
SUMMARIES = {
    ("kpszsu", 2578), ("kpszsu", 57337), ("kpszsu", 62415), ("kpszsu", 66046), ("kpszsu", 78956),
    ("war_monitor", 5646), ("war_monitor", 10487), ("war_monitor", 11118), ("war_monitor", 12864),
    ("war_monitor", 24931), ("war_monitor", 25642), ("war_monitor", 25824), ("war_monitor", 33871),
    ("war_monitor", 35098), ("war_monitor", 41827), ("war_monitor", 6997),
}


def build() -> list[dict]:
    out = []
    for ch in ("kpszsu", "war_monitor"):
        lab = {x["id"]: x for x in json.loads(gold.labels_path(ch).read_text())["labels"]}
        rev = json.loads(gold.review_path(ch).read_text())
        for x in rev["items"]:
            if x.get("verdict") not in ("ok", "fix"):
                continue
            key = (ch, x["id"])
            a = lab[x["id"]]
            obl, typ = (set(x["oblasts"]), set(x["types"])) if x["verdict"] == "fix" else (set(a["oblasts"]), set(a["types"]))
            rules = [f"review:{x['verdict']}"]
            if key in CHANGES:
                ao, ro, at, rt, why = CHANGES[key]
                obl, typ = (obl | ao) - ro, (typ | at) - rt
                rules.append(why)
            if key in CONTEXT:
                typ |= CONTEXT[key]
                rules.append("context")
            out.append({"channel": ch, "id": x["id"], "oblasts": sorted(obl), "types": sorted(typ),
                        "summary": key in SUMMARIES, "reviewer": rev["reviewer"], "rules": rules})
    keys = {(r["channel"], r["id"]) for r in out}
    assert set(CHANGES) | set(CONTEXT) | SUMMARIES <= keys, "a listed change is not a reviewed post"
    return out


if __name__ == "__main__":
    rows = build()
    gold.DEV_V2_PATH.write_text(json.dumps({"rules": "v2 (2026-10-12)", "posts": rows}, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(rows)} posts -> {gold.DEV_V2_PATH}")
