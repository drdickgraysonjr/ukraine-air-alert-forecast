"""Gate: every report that depends on the Telegram parser was computed with the current
parser (same digest), and the parser v1 results are kept for comparison."""

import json
import sys

from alerts import config, telegram

R = config.REPORTS_DIR
V1 = R / "parser_v1"


def main() -> int:
    err = []
    cur = telegram.parser_digest()
    v = json.loads((R / "validation.json").read_text())
    checks = {
        "parsers.json": json.loads((R / "parsers.json").read_text()).get("parser_digest"),
        "parsers.json dev_v2": json.loads((R / "parsers.json").read_text()).get("dev_v2", {}).get("parser_v2_digest"),
        "validation.json text": v.get("text", {}).get("parser_digest"),
        "validation.json edited": v.get("edited", {}).get("parser_digest"),
        "tuning.json": json.loads((R / "tuning.json").read_text()).get("parser_digest"),
        "metrics_phase1.json": json.loads((R / "metrics_phase1.json").read_text()).get("parser_digest"),
        "metrics_phase2.json": json.loads((R / "metrics_phase2.json").read_text()).get("parser_digest"),
        "edited_ablation.json": json.loads((R / "edited_ablation.json").read_text()).get("parser_digest"),
        "crosscheck.json": json.loads((R / "crosscheck.json").read_text()).get("parser_digest"),
    }
    for name, d in checks.items():
        if d != cur:
            err.append(f"{name}: parser digest {d} != current {cur}")
    for f in ("metrics_phase1.json", "metrics_phase2.json", "validation.json", "tuning.json", "edited_ablation.json"):
        p = V1 / f
        if not p.exists():
            err.append(f"parser v1 copy missing: {p}")
        elif json.loads(p.read_text()).get("parser_digest") == cur:
            err.append(f"{p} carries the current digest, not v1")
    if err:
        print("\n".join(err))
        return 1
    print(f"parser {cur}: {len(checks)} reports; v1 copies kept in reports/parser_v1/")
    print("REPORTS ON CURRENT PARSER")
    return 0


if __name__ == "__main__":
    sys.exit(main())
