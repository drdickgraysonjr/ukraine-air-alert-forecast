"""Gate G7: a fresh backtest run from the pinned data reproduces the committed
metric files byte for byte (the README tables are generated from them)."""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("metrics_phase1.json", "metrics_phase2.json")


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    committed = ROOT / "reports"
    with tempfile.TemporaryDirectory() as rep, tempfile.TemporaryDirectory() as art:
        tuning = committed / "tuning.json"
        (Path(rep) / "tuning.json").write_bytes(tuning.read_bytes())
        env = dict(os.environ, ALERTS_REPORTS_DIR=rep, ALERTS_ARTIFACTS_DIR=art)
        r = subprocess.run(["uv", "run", "alerts", "backtest"], cwd=ROOT, env=env, capture_output=True, text=True)
        if r.returncode:
            print(r.stdout[-2000:], r.stderr[-2000:])
            return 1
        bad = []
        for f in FILES:
            a, b = digest(committed / f), digest(Path(rep) / f)
            print(f"{f}: committed {a[:12]} fresh {b[:12]}")
            if a != b:
                bad.append(f)
    if bad:
        print(f"not reproduced: {bad}")
        return 1
    print("REPRODUCIBLE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
