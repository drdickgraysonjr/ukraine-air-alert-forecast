"""Gold set for the Telegram parser: a random sample of posts per channel,
labelled by hand against the guide below, and the parser's accuracy on it.

The sample is drawn with a fixed seed from posts with text published from
2023-01-01 (the modelling period), excluding the posts looked at while the
parser was being written (gold/dev_ids.json). Labels hold post ids and labels
only; the texts stay in the local exports.

Who labelled: the agent (Claude) labelled every post without seeing the
parser's output; Yehor Hunia reviewed every label (gold/review_<channel>.json),
and his corrections win.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config, telegram

GOLD_DIR = config.ROOT / "gold"
N_PER_CHANNEL = 200
SEED = 20261012
FROM_UTC = pd.Timestamp("2023-01-01", tz="UTC")
TYPES = ("alert", "all_clear", "drone", "missile", "ballistic", "kab", "aviation", "artillery")

GUIDE = """\
oblasts: every modelled region the post refers to: by oblast name or adjective
("Сумська", "Сумщина"), by its centre or by another settlement, raion or
landmark in that oblast (only when its location is certain), including a source,
transit or destination of a threat. Kyiv city (kyiv_city) is separate from Kyiv
oblast (kyiv). Crimea, Luhansk oblast and places outside Ukraine are ignored; a
river or a sea is not an oblast. An ambiguous place name that the context does
not resolve is not labelled.

types (any number, or none):
  alert      an air-raid alert is declared ("тривога" as an announcement; a channel
             signature like "єРадар | Повітряна тривога" is not one)
  all_clear  an alert or a threat is over ("відбій")
  drone      any UAV: attack drones (Shahed/Geran), jet drones, reconnaissance UAVs
  missile    cruise or unspecified missiles, "ракетна небезпека", "швидкісна ціль",
             guided air-launched missiles (Kh-59/69, "КАР"), "КР", Onyx
  ballistic  ballistic missiles or threat: Iskander, Kinzhal, KN-23, S-300/400 on ground targets
  kab        guided aerial bombs (КАБ, УМПК)
  aviation   enemy aircraft activity: tactical aviation, Tu-95/160/22, MiG-31K, take-offs, "борти"
  artillery  artillery or MLRS shelling, or its threat
Types apply whether the post warns, reports or summarises.
"""

REGION_IDS = tuple(telegram.OBLASTS)


def sample_ids(channel: str) -> list[int]:
    df = telegram.load_channel(channel)
    dev = json.loads((GOLD_DIR / "dev_ids.json").read_text()).get(channel, [])
    pool = df[(df["text"] != "") & (df["ts"] >= int(FROM_UTC.timestamp())) & ~df["id"].isin(dev)]
    rng = np.random.default_rng(SEED + sum(map(ord, channel)))
    pick = rng.choice(len(pool), N_PER_CHANNEL, replace=False)
    return sorted(int(i) for i in pool["id"].to_numpy()[pick])


def labels_path(channel: str):
    return GOLD_DIR / f"labels_{channel}.json"


def review_path(channel: str):
    return GOLD_DIR / f"review_{channel}.json"


def gold_labels(channel: str, reviewed: bool = True) -> dict[int, dict]:
    """Agent labels, with Yehor's corrections applied when `reviewed`."""
    lab = json.loads(labels_path(channel).read_text())
    out = {int(x["id"]): {"oblasts": set(x["oblasts"]), "types": set(x["types"])} for x in lab["labels"]}
    if reviewed and review_path(channel).exists():
        for x in json.loads(review_path(channel).read_text())["items"]:
            if x.get("verdict") == "fix":
                out[int(x["id"])] = {"oblasts": set(x["oblasts"]), "types": set(x["types"])}
    return out


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def metrics(channel: str, reviewed: bool = True) -> dict:
    """Micro precision/recall of the parser on the gold posts: oblast mentions
    (post x oblast pairs) and each type."""
    gold = gold_labels(channel, reviewed)
    df = telegram.load_channel(channel).set_index("id")
    pred = {i: telegram.parse_text(df.at[i, "text"]) for i in gold}
    tp = fp = fn = 0
    for i, g in gold.items():
        p = set(pred[i][0])
        tp += len(p & g["oblasts"])
        fp += len(p - g["oblasts"])
        fn += len(g["oblasts"] - p)
    out = {"n_posts": len(gold), "oblasts": _prf(tp, fp, fn), "types": {}}
    for t in TYPES:
        a = sum(1 for i, g in gold.items() if t in g["types"] and t in pred[i][1])
        b = sum(1 for i, g in gold.items() if t not in g["types"] and t in pred[i][1])
        c = sum(1 for i, g in gold.items() if t in g["types"] and t not in pred[i][1])
        out["types"][t] = {**_prf(a, b, c), "support": a + c}
    ta = sum(len(set(pred[i][1]) & g["types"]) for i, g in gold.items())
    tb = sum(len(set(pred[i][1]) - g["types"]) for i, g in gold.items())
    tc = sum(len(g["types"] - set(pred[i][1])) for i, g in gold.items())
    out["types_micro"] = _prf(ta, tb, tc)
    out["exact_post_match"] = sum(1 for i, g in gold.items() if set(pred[i][0]) == g["oblasts"] and set(pred[i][1]) == g["types"]) / len(gold)
    return out
