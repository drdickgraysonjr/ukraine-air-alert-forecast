"""Gate: no raw data file, Telegram export or post text is tracked by git
(alerts.in.ua data has no licence to redistribute; the posts belong to the
channels). Gold labels may be tracked, but only as ids and labels.
Positive controls: the same check must flag a scratch repo in which a CSV is
tracked, and one in which a gold file carries post texts."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAD_SUFFIXES = (".csv", ".csv.gz", ".parquet", ".jsonl", ".zip", ".html", ".pkl")
MAX_BYTES = 1_000_000


def offenders(repo: Path) -> list[str]:
    files = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    bad = []
    for f in files:
        p = repo / f
        if f.startswith(("data/", "artifacts/")) or f.endswith(BAD_SUFFIXES) or (p.exists() and p.stat().st_size > MAX_BYTES):
            bad.append(f)
        elif f.startswith("gold/") and p.exists() and _has_text(p):
            bad.append(f)
    return bad


def _has_text(p: Path) -> bool:
    """A gold file with any field named "text" (post bodies)."""
    def walk(x):
        if isinstance(x, dict):
            return "text" in x or any(walk(v) for v in x.values())
        if isinstance(x, list):
            return any(walk(v) for v in x)
        return False

    try:
        return walk(json.loads(p.read_text()))
    except ValueError:
        return True


def control() -> bool:
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "data" / "raw").mkdir(parents=True)
        (repo / "data" / "raw" / "alerts_in_ua.csv").write_text("id,started_at\n1,2026-01-01\n")
        (repo / "gold").mkdir()
        (repo / "gold" / "labels_x.json").write_text(json.dumps({"labels": [{"id": 1, "text": "post"}]}))
        (repo / "gold" / "labels_y.json").write_text(json.dumps({"labels": [{"id": 1, "oblasts": []}]}))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        return offenders(repo) == ["data/raw/alerts_in_ua.csv", "gold/labels_x.json"]


def main() -> int:
    bad = offenders(ROOT)
    detected = control()
    print(f"tracked offenders: {bad}; control detected: {detected}")
    if bad or not detected:
        return 1
    print("NO RAW DATA TRACKED; CONTROL DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
