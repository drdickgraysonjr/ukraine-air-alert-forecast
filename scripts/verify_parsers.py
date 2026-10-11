"""Gates: the parser's accuracy is measured on a random gold set and reproduced.

Without flags: each of the 4 channels has a gold file whose post ids are exactly
the seeded random draw (recomputed here), at least 200 posts, valid labels; the
accuracy recomputed from the gold set equals reports/parsers.json.
With --require-review: every gold post also carries a review verdict (ok/fix)
by a named reviewer, and reports/parsers.json was computed on the reviewed set.
"""

import json
import sys

from alerts import config, gold, telegram


def main() -> int:
    need_review = "--require-review" in sys.argv
    err = []
    rep = json.loads((config.REPORTS_DIR / "parsers.json").read_text())["channels"]
    for c in telegram.CHANNELS:
        if not gold.labels_path(c).exists():
            err.append(f"{c}: no gold labels")
            continue
        lab = json.loads(gold.labels_path(c).read_text())["labels"]
        ids = [int(x["id"]) for x in lab]
        draw = gold.sample_ids(c)
        if sorted(ids) != draw or len(set(ids)) != len(ids):
            err.append(f"{c}: gold ids are not the seeded random draw")
        if len(ids) < 200:
            err.append(f"{c}: {len(ids)} gold posts < 200")
        for x in lab:
            if set(x["oblasts"]) - set(gold.REGION_IDS) or set(x["types"]) - set(gold.TYPES):
                err.append(f"{c}/{x['id']}: unknown label")
        m = gold.metrics(c, reviewed=True)
        r = rep.get(c)
        if r is None:
            err.append(f"{c}: missing from reports/parsers.json")
            continue
        for part in ("oblasts", "types_micro"):
            if any(m[part][k] != r[part][k] for k in ("tp", "fp", "fn")):
                err.append(f"{c}: {part} counts differ from reports/parsers.json")
        print(f"{c}: {len(ids)} gold posts; oblasts P {m['oblasts']['precision']:.3f} R {m['oblasts']['recall']:.3f}; types P {m['types_micro']['precision']:.3f} R {m['types_micro']['recall']:.3f}; reviewed: {r['reviewed']}")
        if need_review:
            p = gold.review_path(c)
            if not p.exists():
                err.append(f"{c}: no review file")
                continue
            rv = json.loads(p.read_text())
            got = {int(x["id"]): x.get("verdict") for x in rv["items"]}
            if set(got) != set(ids) or any(v not in ("ok", "fix") for v in got.values()):
                err.append(f"{c}: review covers {sum(v in ('ok', 'fix') for v in got.values())} of {len(ids)} posts")
            if not (rv.get("reviewer") or "").strip():
                err.append(f"{c}: reviewer not named")
            if not r["reviewed"]:
                err.append(f"{c}: reports/parsers.json not computed on the reviewed gold set")
    if err:
        print("\n".join(err))
        return 1
    print("GOLD REVIEWED" if need_review else "PARSER METRICS REPRODUCED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
