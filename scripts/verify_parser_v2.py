"""Gate: parser v2 on the 250 reviewed posts (gold/dev_v2.json, a development set).

1. The dev set is exactly the reviewed posts (verdict ok/fix, named reviewer) and is the
   output of scripts/build_dev_v2.py.
2. Parser v2 reproduces every label change Yehor made in his review (added labels present,
   removed absent), except where his later rule supersedes it (listed in build_dev_v2), and
   every macro-region, inactive-type and summary case of the dev set.
3. The v1 and v2 metrics recomputed here equal reports/parsers.json["dev_v2"].
4. Control: parser v1 misses at least one case of each kind.
"""

import json
import sys

sys.path.insert(0, "scripts")

from alerts import config, gold, parser_v1, telegram  # noqa: E402
from build_dev_v2 import CHANGES, SUMMARIES, build  # noqa: E402


def main() -> int:
    err = []
    dev = json.loads(gold.DEV_V2_PATH.read_text())["posts"]
    if dev != build():
        err.append("gold/dev_v2.json is not the output of build_dev_v2.py")
    reviewed = {}
    for ch in ("kpszsu", "war_monitor"):
        rv = json.loads(gold.review_path(ch).read_text())
        if not rv.get("reviewer", "").strip():
            err.append(f"{ch}: reviewer not named")
        lab = {x["id"]: x for x in json.loads(gold.labels_path(ch).read_text())["labels"]}
        for x in rv["items"]:
            if x.get("verdict") in ("ok", "fix"):
                reviewed[(ch, x["id"])] = (x, lab[x["id"]])
    if {(r["channel"], r["id"]) for r in dev} != set(reviewed) or len(dev) != 250:
        err.append(f"dev set ({len(dev)}) is not the {len(reviewed)} reviewed posts")

    frames = {c: telegram.load_channel(c).set_index("id") for c in ("kpszsu", "war_monitor")}

    def v2(key):
        x = frames[key[0]].loc[key[1]]
        return set(x["oblasts"]), set(x["types"]), bool(x["summary"])

    def v1(key):
        o, t = parser_v1.parse_text(frames[key[0]].at[key[1], "text"])
        return set(o), set(t), False

    cases = []  # (kind, key, check(pred) -> bool)
    for key, (x, a) in reviewed.items():
        if x["verdict"] != "fix":
            continue
        ao, ro = set(x["oblasts"]) - set(a["oblasts"]), set(a["oblasts"]) - set(x["oblasts"])
        at, rt = set(x["types"]) - set(a["types"]), set(a["types"]) - set(x["types"])
        sup = CHANGES.get(key, (set(), set(), set(), set(), ""))
        at -= sup[3]  # an added type his later rule removes
        if ao or ro or at or rt:
            cases.append(("review", key, lambda p, ao=ao, ro=ro, at=at, rt=rt: ao <= p[0] and not ro & p[0] and at <= p[1] and not rt & p[1]))
    for key, (ao, ro, at, rt, why) in CHANGES.items():
        if why.startswith("macro"):
            cases.append(("macro", key, lambda p, ao=ao: ao <= p[0]))
        elif why.startswith("inactive"):
            cases.append(("inactive", key, lambda p, rt=rt: not rt & p[1]))
    for key in SUMMARIES:
        cases.append(("summary", key, lambda p: p[2]))

    kinds = sorted({k for k, _, _ in cases})
    miss2 = [(k, key) for k, key, ok in cases if not ok(v2(key))]
    miss1 = {k: sum(1 for kk, key, ok in cases if kk == k and not ok(v1(key))) for k in kinds}
    for k, key in miss2:
        err.append(f"parser v2 misses {k} case {key}")
    for k in kinds:
        if miss1[k] == 0:
            err.append(f"control: parser v1 does not miss any {k} case")

    rep = json.loads((config.REPORTS_DIR / "parsers.json").read_text()).get("dev_v2")
    now = gold.dev_metrics()
    if rep is None:
        err.append("reports/parsers.json has no dev_v2")
    else:
        for c, v in now["channels"].items():
            for ver in ("v1", "v2"):
                for part in ("oblasts", "types_micro", "summary"):
                    if any(v[ver][part][k] != rep["channels"][c][ver][part][k] for k in ("tp", "fp", "fn")):
                        err.append(f"{c} {ver} {part}: counts differ from reports/parsers.json")
    if err:
        print("\n".join(err))
        return 1
    n = {k: sum(1 for kk, _, _ in cases if kk == k) for k in kinds}
    a = now["channels"]["all"]
    print("cases: " + ", ".join(f"{k} {n[k]} (v1 misses {miss1[k]})" for k in kinds))
    print(f"dev set 250 posts, exact match v1 {a['v1']['exact_post_match']:.2f} -> v2 {a['v2']['exact_post_match']:.2f}")
    print("PARSER V2 VERIFIED; CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
