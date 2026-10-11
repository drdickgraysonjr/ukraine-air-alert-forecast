"""Gate: the run without edited posts was made and recorded honestly.

The number of dropped posts per channel must equal an independent count of the
"edited" marks in the raw HTML exports; reports/edited_ablation.json must hold
the headline numbers with edited posts equal to the current metric files and a
full set without them; the validation-window comparison must be in
reports/validation.json.
"""

import json
import re
import sys

from alerts import ablation, config, telegram


def main() -> int:
    err = []
    a = json.loads((config.REPORTS_DIR / "edited_ablation.json").read_text())
    if a["channels"] != list(config.TEXT_CHANNELS):
        err.append(f"ablation channels {a['channels']} != config {config.TEXT_CHANNELS}")
    for c in config.TEXT_CHANNELS:
        n = 0
        for f in telegram.export_files(telegram.TELEGRAM_DIR / c):
            n += len(re.findall(r'class="pull_right date details" title="[^"]*">edited ', open(f, encoding="utf-8").read()))
        print(f"{c}: edited marks in raw HTML {n}, dropped in the run {a['dropped_posts'].get(c)}")
        if a["dropped_posts"].get(c) != n:
            err.append(f"{c}: dropped {a['dropped_posts'].get(c)} != {n} edited marks")
    cur = ablation.headline(json.loads((config.REPORTS_DIR / "metrics_phase1.json").read_text()),
                            json.loads((config.REPORTS_DIR / "metrics_phase2.json").read_text()))
    if a["with_edited"] != cur:
        err.append("headline numbers with edited posts differ from the current metric files")
    if set(a["without_edited"]) != set(cur) or set(a["without_text"]) != set(cur):
        err.append("the runs without edited posts / without text do not cover every window")
    v = json.loads((config.REPORTS_DIR / "validation.json").read_text()).get("edited")
    if not v or len(v["trials"]) != 2 or v["dropped_posts"] != a["dropped_posts"]:
        err.append("validation-window comparison missing or on other posts")
    for k in sorted(cur):
        w, wo = a["with_edited"][k], a["without_edited"].get(k, {})
        print(f"{k}: phase 1 skill (hours) {w['phase1_skill_vs_best_naive']['hours']:+.3f} -> {wo.get('phase1_skill_vs_best_naive', {}).get('hours', float('nan')):+.3f}; "
              f"phase 2 BSS 1h {w['phase2_bss_vs_climatology']['1']:+.3f} -> {wo.get('phase2_bss_vs_climatology', {}).get('1', float('nan')):+.3f}")
    if err:
        print("\n".join(err))
        return 1
    print("EDITED ABLATION VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
