"""Gold set for the Telegram parser: a random sample of posts per channel,
labelled by hand against the guide below, and the parser's accuracy on it.

The sample is drawn with a fixed seed from posts with text published from
2023-01-01 (the modelling period), excluding the posts looked at while the
parser was being written (gold/dev_ids.json). Labels hold post ids and labels
only; the texts stay in the local exports.

Who labelled: the agent (Claude) labelled every post without seeing the
parser's output (rules v1). Yehor Hunia reviewed 250 of the 800 labels
(gold/review_<channel>.json: all of @kpszsu, 50 of @war_monitor) and set the
rules v2 below; the other 550 were not reviewed. The reviewed posts, relabelled
under the rules v2 (gold/dev_v2.json, scripts/build_dev_v2.py), were used to write
parser v2, so the parser's accuracy on them is a development figure.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config, telegram

GOLD_DIR = config.ROOT / "gold"
DEV_V2_PATH = GOLD_DIR / "dev_v2.json"
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

Rules v2 (Yehor Hunia, 2026-10-12, after reviewing 250 posts):
  macro-regions  "на півдні", "по Півдню", "південних областей" and the like refer to
             every oblast of the macro-region: north = Kyiv oblast, Kyiv city, Chernihiv,
             Sumy, Zhytomyr, Poltava, Cherkasy; east = Kharkiv, Donetsk, Luhansk,
             Dnipropetrovsk, Zaporizhzhia; south = Odesa, Mykolaiv, Kherson, Vinnytsia,
             Kirovohrad, Zaporizhzhia, Crimea; west = Volyn, Rivne, Lviv, Ternopil,
             Khmelnytskyi, Ivano-Frankivsk, Zakarpattia, Chernivtsi; centre = Vinnytsia,
             Cherkasy, Kirovohrad, Poltava, Dnipropetrovsk. A direction inside a named
             oblast ("на півночі Сумщини") or of movement ("з півночі") is not one.
  summary    a report after an attack or an overview of the night or the day
             ("ЗБИТО/ПОДАВЛЕНО", "Обстановка станом на", "За минулу добу", strike
             reports) is marked as a summary; its places and types are kept.
  inactive   a type mentioned only as inactive or absent is not a type of the post
             ("Стратегічна авіація не активна", "флот без активності", "ракетоносії
             заведені в порти", a NATO reconnaissance drone).
  context    a post that names a place but no type takes the threat type from the
             channel's previous posts ("Деміївка.", "1х у Павлоградському районі",
             "комбінована атака").
  markers    "вихід" is a ballistic or cruise missile launch; "група" is drones.
  Kyiv       "передмістя Києва" and the Kyiv reservoir give both Kyiv city and Kyiv
             oblast; Лук'янівка is Kyiv city, Козин is Kyiv oblast.
  Bandarol   the "Бандероль" loitering munition stays a drone.
"""

REGION_IDS = tuple(telegram.OBLASTS)


def CHANNELS_WITH_GOLD():  # noqa: N802
    return tuple(c for c in telegram.CHANNELS if labels_path(c).exists())


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


def dev_metrics() -> dict:
    """Parser v1 (per post) and parser v2 (with context) on the reviewed posts under the
    rules v2, per channel and pooled. A development figure: v2 was written on these posts."""
    from . import parser_v1

    rows = json.loads(DEV_V2_PATH.read_text())["posts"]
    frames = {c: telegram.load_channel(c).set_index("id") for c in sorted({r["channel"] for r in rows})}

    def score(rs, pred) -> dict:
        o = [0, 0, 0]
        t = [0, 0, 0]
        s = [0, 0, 0]
        exact = 0
        for r in rs:
            po, pt, ps = pred(r)
            go, gt, gs = set(r["oblasts"]), set(r["types"]), r["summary"]
            o = [o[0] + len(po & go), o[1] + len(po - go), o[2] + len(go - po)]
            t = [t[0] + len(pt & gt), t[1] + len(pt - gt), t[2] + len(gt - pt)]
            s = [s[0] + (ps and gs), s[1] + (ps and not gs), s[2] + (gs and not ps)]
            exact += po == go and pt == gt and ps == gs
        return {"n_posts": len(rs), "oblasts": _prf(*o), "types_micro": _prf(*t), "summary": _prf(*s), "exact_post_match": exact / len(rs)}

    def v1(r):
        o, t = parser_v1.parse_text(frames[r["channel"]].at[r["id"], "text"])
        return set(o), set(t), False

    def v2(r):
        x = frames[r["channel"]].loc[r["id"]]
        return set(x["oblasts"]), set(x["types"]), bool(x["summary"])

    out = {"guide": "rules v2", "parser_v2_digest": telegram.parser_digest(), "channels": {}}
    for c in list(frames) + ["all"]:
        rs = [r for r in rows if c == "all" or r["channel"] == c]
        out["channels"][c] = {"v1": score(rs, v1), "v2": score(rs, v2)}
    return out


TYPE_NAMES_UK = {
    "alert": "тривога", "all_clear": "відбій", "drone": "БпЛА", "missile": "ракети",
    "ballistic": "балістика", "kab": "КАБ", "aviation": "авіація", "artillery": "артилерія",
}


def review_html(channels, out_path, prior=None) -> int:
    """A local page for reviewing every gold label (texts included, never committed).
    `prior`: an earlier downloaded gold_review.json whose verdicts the page starts from
    (browser progress lives per address, so it can be lost)."""
    from .regions import REGIONS

    items = []
    for c in channels:
        lab = json.loads(labels_path(c).read_text())
        df = telegram.load_channel(c).set_index("id")
        for x in lab["labels"]:
            i = int(x["id"])
            items.append({"channel": c, "handle": telegram.CHANNELS[c].handle, "id": i,
                          "local": df.at[i, "ts_local"], "text": df.at[i, "text"],
                          "oblasts": sorted(x["oblasts"]), "types": [t for t in TYPES if t in x["types"]], "note": x.get("note", "")})
    # Posts the agent flagged with a note come first within each channel (stable, so ids stay ascending).
    items.sort(key=lambda it: (channels.index(it["channel"]), not it["note"]))
    regions = [[r, REGIONS[r][1].replace(" область", "")] for r in REGION_IDS]
    types = [[t, TYPE_NAMES_UK[t]] for t in TYPES]
    keys = {f"{it['channel']}:{it['id']}" for it in items}
    seed = {}
    if prior:
        for x in json.loads(open(prior, encoding="utf-8").read())["items"]:
            key = f"{x['channel']}:{x['id']}"
            if x.get("verdict") in ("ok", "fix") and key in keys:
                seed[key] = {"verdict": x["verdict"], "comment": x.get("comment", "")}
                if x["verdict"] == "fix":
                    seed[key] |= {"oblasts": x["oblasts"], "types": x["types"]}
    page = _REVIEW_PAGE.replace("__ITEMS__", json.dumps(items, ensure_ascii=False)).replace("__PRIOR__", json.dumps(seed, ensure_ascii=False)).replace(
        "__REGIONS__", json.dumps(regions, ensure_ascii=False)).replace("__TYPES__", json.dumps(types, ensure_ascii=False)).replace(
        "__GUIDE__", json.dumps(GUIDE, ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return len(items)


def import_review(path, reviewer: str) -> dict:
    """Split a downloaded review file into gold/review_<channel>.json."""
    data = json.loads(open(path, encoding="utf-8").read())
    by = {}
    for x in data["items"]:
        by.setdefault(x["channel"], []).append({k: x[k] for k in ("id", "verdict", "oblasts", "types", "comment") if k in x})
    for c, xs in by.items():
        review_path(c).write_text(json.dumps({"channel": c, "reviewer": reviewer, "exported_at": data.get("exported_at"), "items": sorted(xs, key=lambda x: x["id"])}, ensure_ascii=False, indent=1) + "\n")
    return {c: len(xs) for c, xs in by.items()}


_REVIEW_PAGE = r"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Перевірка еталону</title>
<style>
:root{--bg:#fafaf8;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--ok:#1f7a4d;--okbg:#e7f4ec;--fix:#a8461c;--fixbg:#fbeee6;--chip:#efeee9;--acc:#2b5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--mute:#a09f98;--line:#33332f;--ok:#5cc58e;--okbg:#17301f;--fix:#f0956a;--fixbg:#3a2117;--chip:#2c2c29;--acc:#7da2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
header b{font-size:16px}.bar{flex:1;min-width:120px;height:6px;background:var(--line);border-radius:3px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok)}
button,select,input{font:inherit}button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:5px 11px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
main{max-width:860px;margin:0 auto;padding:12px 16px 80px}
details.guide{margin:4px 0 14px;color:var(--mute)}details.guide pre{white-space:pre-wrap;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 12px}
.card.cur{outline:2px solid var(--acc)}.card.ok{border-left:5px solid var(--ok)}.card.fix{border-left:5px solid var(--fix)}
.meta{color:var(--mute);font-size:13px;margin-bottom:6px}.text{white-space:pre-wrap;margin:0 0 10px;padding:8px 10px;background:var(--chip);border-radius:7px}
.lab{margin:4px 0}.lab span.k{color:var(--mute);font-size:13px;margin-right:6px}.chip{display:inline-block;background:var(--chip);border-radius:12px;padding:1px 9px;margin:2px 3px 2px 0;font-size:13px}
.note{color:var(--mute);font-size:13px;font-style:italic}.acts{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.acts .okb.on{background:var(--okbg);border-color:var(--ok);color:var(--ok)}.acts .fixb.on{background:var(--fixbg);border-color:var(--fix);color:var(--fix)}
.edit{display:none;margin-top:10px;border-top:1px dashed var(--line);padding-top:8px}.card.fix .edit{display:block}
.grid{display:flex;flex-wrap:wrap;gap:4px 12px;margin:4px 0 8px}.grid label{font-size:13px;white-space:nowrap}
.edit input[type=text]{width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
.keys{color:var(--mute);font-size:12px}
</style></head><body>
<header><b>Еталон парсера</b><span id="cnt"></span><div class="bar"><i id="prog"></i></div>
<select id="ch"></select><label><input type="checkbox" id="todo"> лише неперевірені</label>
<input id="who" placeholder="Хто перевіряє" style="width:150px;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink)">
<button class="primary" id="dl">Завантажити JSON</button></header>
<main><details class="guide"><summary>Правила розмітки</summary><pre id="guide"></pre></details>
<div class="keys">Клавіші: J / K — наступний / попередній, O — правильно, F — виправити.</div><div id="list"></div></main>
<script>
const ITEMS=__ITEMS__, REGIONS=__REGIONS__, TYPES=__TYPES__, GUIDE=__GUIDE__;
const RN=Object.fromEntries(REGIONS), TN=Object.fromEntries(TYPES);
const KEY='gold-review-v1';let st={};try{st=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}};
const PRIOR=__PRIOR__;for(const [key,v] of Object.entries(PRIOR))if(!st[key])st[key]=v;save();
const k=it=>it.channel+':'+it.id;let cur=0;
document.getElementById('guide').textContent=GUIDE;
const chs=[...new Set(ITEMS.map(i=>i.channel))];const sel=document.getElementById('ch');
sel.innerHTML='<option value="">усі канали</option>'+chs.map(c=>`<option value="${c}">${ITEMS.find(i=>i.channel==c).handle}</option>`).join('');
try{document.getElementById('who').value=localStorage.getItem(KEY+'-who')||''}catch(e){}
document.getElementById('who').oninput=e=>{try{localStorage.setItem(KEY+'-who',e.target.value)}catch(x){}};
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function visible(){const c=sel.value,t=document.getElementById('todo').checked;return ITEMS.filter(i=>(!c||i.channel==c)&&(!t||!st[k(i)]))}
function chips(a,names){return a.length?a.map(x=>`<span class="chip">${esc(names[x]||x)}</span>`).join(''):'<span class="chip">—</span>'}
function render(){const v=visible();const L=document.getElementById('list');
 L.innerHTML=v.map((it,n)=>{const s=st[k(it)]||{};const o=s.oblasts||it.oblasts,t=s.types||it.types;
 return `<div class="card ${s.verdict||''}" data-k="${k(it)}" data-n="${n}"><div class="meta">${it.handle} · #${it.id} · ${it.local} (Варшава)</div>
 <div class="text">${esc(it.text)}</div>
 <div class="lab"><span class="k">Області</span>${chips(it.oblasts,RN)}</div><div class="lab"><span class="k">Типи</span>${chips(it.types,TN)}</div>
 ${it.note?`<div class="note">Примітка агента: ${esc(it.note)}</div>`:''}
 <div class="acts"><button class="okb ${s.verdict=='ok'?'on':''}">Правильно</button><button class="fixb ${s.verdict=='fix'?'on':''}">Виправити</button></div>
 <div class="edit"><div class="k">Області</div><div class="grid">${REGIONS.map(([r,nm])=>`<label><input type="checkbox" data-r="${r}" ${o.includes(r)?'checked':''}> ${esc(nm)}</label>`).join('')}</div>
 <div class="k">Типи</div><div class="grid">${TYPES.map(([x,nm])=>`<label><input type="checkbox" data-t="${x}" ${t.includes(x)?'checked':''}> ${esc(nm)}</label>`).join('')}</div>
 <input type="text" class="cm" placeholder="Коментар (необовʼязково)" value="${esc(s.comment||'')}"></div></div>`}).join('');
 L.querySelectorAll('.card').forEach(el=>wire(el));stats();mark()}
function item(key){return ITEMS.find(i=>k(i)==key)}
function wire(el){const key=el.dataset.k,it=item(key);
 el.querySelector('.okb').onclick=()=>{st[key]={verdict:'ok'};save();refresh(el)};
 el.querySelector('.fixb').onclick=()=>{st[key]={verdict:'fix',oblasts:[...it.oblasts],types:[...it.types],comment:''};save();refresh(el)};
 el.querySelectorAll('input[type=checkbox]').forEach(cb=>cb.onchange=()=>{const s=st[key];if(!s)return;
  s.oblasts=[...el.querySelectorAll('input[data-r]:checked')].map(x=>x.dataset.r);s.types=[...el.querySelectorAll('input[data-t]:checked')].map(x=>x.dataset.t);save()});
 el.querySelector('.cm').oninput=e=>{if(st[key]){st[key].comment=e.target.value;save()}};
 el.onclick=()=>{cur=+el.dataset.n;mark()}}
function refresh(el){const s=st[el.dataset.k]||{};el.className='card '+(s.verdict||'')+(+el.dataset.n==cur?' cur':'');
 el.querySelector('.okb').classList.toggle('on',s.verdict=='ok');el.querySelector('.fixb').classList.toggle('on',s.verdict=='fix');stats()}
function stats(){const d=ITEMS.filter(i=>st[k(i)]).length;document.getElementById('cnt').textContent=`${d} з ${ITEMS.length}`;document.getElementById('prog').style.width=(100*d/ITEMS.length)+'%'}
function mark(){document.querySelectorAll('.card').forEach(c=>c.classList.toggle('cur',+c.dataset.n==cur))}
function go(d){const cs=document.querySelectorAll('.card');cur=Math.max(0,Math.min(cs.length-1,cur+d));mark();cs[cur]&&cs[cur].scrollIntoView({block:'center',behavior:'smooth'})}
document.addEventListener('keydown',e=>{if(e.target.tagName=='INPUT')return;const c=document.querySelectorAll('.card')[cur];
 if(e.key=='j')go(1);else if(e.key=='k')go(-1);else if(e.key=='o'&&c){c.querySelector('.okb').click();go(1)}else if(e.key=='f'&&c)c.querySelector('.fixb').click()});
sel.onchange=()=>{cur=0;render()};document.getElementById('todo').onchange=()=>{cur=0;render()};
document.getElementById('dl').onclick=()=>{const out={reviewer:document.getElementById('who').value,exported_at:new Date().toISOString(),
 items:ITEMS.map(i=>{const s=st[k(i)];return s?{channel:i.channel,id:i.id,verdict:s.verdict,oblasts:s.verdict=='fix'?s.oblasts:i.oblasts,types:s.verdict=='fix'?s.types:i.types,comment:s.comment||''}:{channel:i.channel,id:i.id,verdict:null}})};
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:'application/json'}));a.download='gold_review.json';a.click()};
render();
</script></body></html>
"""


def report(channels) -> dict:
    """reports/parsers.json: parser accuracy per channel on the (reviewed) gold set."""
    out = {"guide": GUIDE, "seed": SEED, "n_per_channel": N_PER_CHANNEL, "from_utc": str(FROM_UTC),
           "parser_digest": telegram.parser_digest(), "channels": {}}
    for c in channels:
        lab = json.loads(labels_path(c).read_text())
        rev = json.loads(review_path(c).read_text()) if review_path(c).exists() else None
        out["channels"][c] = {
            "annotator": lab["annotator"],
            "reviewer": rev["reviewer"] if rev else None,
            "reviewed": bool(rev) and all(x.get("verdict") in ("ok", "fix") for x in rev["items"]) and len(rev["items"]) == len(lab["labels"]),
            "corrections": sum(1 for x in rev["items"] if x.get("verdict") == "fix") if rev else None,
            **metrics(c, reviewed=True),
        }
    if DEV_V2_PATH.exists():
        out["dev_v2"] = dev_metrics()
    (config.REPORTS_DIR / "parsers.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return out
