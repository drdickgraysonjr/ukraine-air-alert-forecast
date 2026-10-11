"""Gate G1: raw data is pinned to source commits and matches the manifest."""

import json
import re
import sys

from alerts import config
from alerts.data import sha256


def main() -> int:
    errors = []
    manifest = json.loads(config.MANIFEST_PATH.read_text())["sources"]
    for src in config.SOURCES:
        m = manifest.get(src.name)
        if m is None:
            errors.append(f"{src.name}: missing from manifest")
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", m["commit"]) or m["commit"] != src.commit:
            errors.append(f"{src.name}: manifest commit {m['commit']} != pinned {src.commit}")
        if src.commit not in m["url"]:
            errors.append(f"{src.name}: url not pinned to commit")
        if not src.local_path.exists():
            errors.append(f"{src.name}: {src.local_path} missing (run `alerts download`)")
            continue
        digest = sha256(src.local_path)
        if digest != m["sha256"]:
            errors.append(f"{src.name}: sha256 {digest} != manifest {m['sha256']}")
        else:
            print(f"{src.name}: {m['commit'][:10]} sha256 {digest[:12]} ok")
    if errors:
        print("\n".join(errors))
        return 1
    print("DATA VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
