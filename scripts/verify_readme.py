"""Gate G9: README numbers come from the metric files, and the README explains
the data decisions, the leakage policy, how to run it and its limitations."""

import json
import re
import sys

from alerts import config, report

REQUIRED = ["## Data", "## No look-ahead", "## How to run", "## Limitations", "## Results",
            "### Zones of regular alerts", "### History start", "### Telegram channels", "## Choices made on the validation window"]
# Decisions the README must state in words (lower-cased fragments).
DECISIONS = {
    "no gap merging": "separated by any gap",
    "hromada alerts longer than 24 h": "longer than 24 hours",
    "2022 history compared": "history from 2022-04-13",
    "free text before 2022-03-15": "before 2022-03-15",
    "edited posts kept": "edited posts are kept",
    "who labelled the gold set": "yehor hunia reviewed every label",
}


def main() -> int:
    text = report.README.read_text()
    err = [f"missing section {h!r}" for h in REQUIRED if h not in text]
    m1 = json.loads((config.REPORTS_DIR / "metrics_phase1.json").read_text())
    m2 = json.loads((config.REPORTS_DIR / "metrics_phase2.json").read_text())
    low = text.lower()
    err += [f"decision not described: {k}" for k, frag in DECISIONS.items() if frag not in low]
    want = report.results_markdown(m1, m2)
    choices = report.choices_markdown(*(json.loads((config.REPORTS_DIR / f).read_text()) for f in ("validation.json", "parsers.json", "edited_ablation.json", "crosscheck.json")))
    gc = re.search(re.escape(report.CSTART) + r"\n(.*?)\n" + re.escape(report.CEND), text, re.S)
    if not gc or gc.group(1) != choices:
        err.append("choices block differs from reports/*.json (run `alerts report`)")
    got = re.search(re.escape(report.START) + r"\n(.*?)\n" + re.escape(report.END), text, re.S)
    if not got:
        err.append("results markers missing")
    elif got.group(1) != want:
        err.append("results block differs from reports/*.json (run `alerts report`)")
    for fig in ("reports/fig_daily_hours.png", "reports/fig_reliability.png", "reports/fig_zones.png"):
        if fig not in text or not (config.ROOT / fig).exists():
            err.append(f"figure {fig} not referenced or missing")
    for src in config.SOURCES:
        if src.commit[:10] not in text:
            err.append(f"pinned commit of {src.name} not stated")
    if err:
        print("\n".join(err))
        return 1
    print(f"results block: {len(want.splitlines())} lines match the metric files")
    print("README CONSISTENT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
