"""Gate G10: no raw data file is tracked by git (alerts.in.ua data has no licence
to redistribute). Positive control: the same check must flag a scratch repo in
which a CSV is tracked."""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAD_SUFFIXES = (".csv", ".csv.gz", ".parquet", ".jsonl", ".zip")
MAX_BYTES = 1_000_000


def offenders(repo: Path) -> list[str]:
    files = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    bad = []
    for f in files:
        p = repo / f
        if f.startswith(("data/", "artifacts/")) or f.endswith(BAD_SUFFIXES) or (p.exists() and p.stat().st_size > MAX_BYTES):
            bad.append(f)
    return bad


def control() -> bool:
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "data" / "raw").mkdir(parents=True)
        (repo / "data" / "raw" / "alerts_in_ua.csv").write_text("id,started_at\n1,2026-01-01\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        return offenders(repo) == ["data/raw/alerts_in_ua.csv"]


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
