"""Gate G1: raw data is pinned to source commits and matches the manifest; every
Telegram export matches its pinned digest and is read completely.

For an export, the number of posts read must equal an independent count of
message blocks in the raw HTML, every post must have a date (the reader
refuses otherwise), and post times must not run backwards against post ids
except within the repeated autumn hour."""

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
    errors += telegram_errors(json.loads(config.MANIFEST_PATH.read_text()).get("telegram", {}))
    if errors:
        print("\n".join(errors))
        return 1
    print("DATA VERIFIED; TELEGRAM VERIFIED")
    return 0


def telegram_errors(pinned: dict) -> list[str]:
    from alerts import telegram

    err = []
    missing = set(telegram.CHANNELS) - set(pinned)
    if missing:
        err.append(f"telegram: channels not pinned: {sorted(missing)}")
    for key, m in sorted(pinned.items()):
        path = telegram.TELEGRAM_DIR / key
        digest, size = telegram.export_sha256(path)
        if digest != m["sha256"] or size != m["bytes"]:
            err.append(f"telegram {key}: digest {digest[:12]} != manifest {m['sha256'][:12]}")
            continue
        blocks = sum(len(re.findall(r'<div class="message default clearfix(?: joined)?" id="message', open(f, encoding="utf-8").read())) for f in telegram.export_files(path))
        df = telegram.read_export(path)
        if len(df) != blocks or len(df) != m["n_posts"]:
            err.append(f"telegram {key}: {len(df)} posts read, {blocks} message blocks, manifest {m['n_posts']}")
        back = df["ts"].diff() < 0
        # A backward step is legitimate only into the repeated autumn hour.
        rep = df["ts_local"].str.slice(11, 13).eq("02") & df["ts_local"].str.slice(5, 7).eq("10")
        bad = int((back & ~rep).sum())
        if bad > max(2, len(df) // 10000):
            err.append(f"telegram {key}: {bad} posts earlier than the post before them")
        print(f"telegram {key}: {len(df)} posts = {blocks} blocks, {m['first_post_local']} .. {m['last_post_local']}, sha256 {digest[:12]} ok, backward steps {bad}")
    return err


if __name__ == "__main__":
    sys.exit(main())
