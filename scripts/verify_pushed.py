"""Gate G11: the working tree is clean and HEAD is on origin/main."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def main() -> int:
    git("fetch", "-q", "origin")
    dirty = git("status", "--porcelain")
    head, remote = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    print(f"HEAD {head[:10]} origin/main {remote[:10]} dirty: {bool(dirty)}")
    if dirty:
        print(dirty)
    if dirty or head != remote:
        return 1
    print("PUSHED CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
