"""Download speechocean762 (OpenSLR 101, CC BY 4.0) into data/speechocean762 and verify it. Setup-time only.

    python3 benchmarks/pronunciation/fetch_speechocean762.py

Source: the corpus GitHub repository at a pinned commit. OpenSLR 101 recommends the GitHub copy as the latest
version; it carries the 2024-07-16 fix of illegal phoneme symbols in resource/scores.json ("mispronunciations"
blocks). OpenSLR publishes no checksum, so each extracted file is checked against the git blob SHA-1 listed in
the GitHub tree of the pinned commit, and a digest of that list is written to data/speechocean762/VERIFIED.json.
Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = "jimbozhang/speechocean762"
COMMIT = "613968e3b0b789fc33936fb5eba1973176ba7d11"  # 2025-10-26, README-only change after eef083d (phoneme fix)
ARCHIVE_URL = f"https://codeload.github.com/{REPO}/tar.gz/{COMMIT}"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{COMMIT}?recursive=1"
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DEST = DATA / "speechocean762"


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def download(url: str, out: Path) -> None:
    tmp = out.with_suffix(out.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    tmp.rename(out)


def extract(archive: Path, dest: Path) -> None:
    prefix = f"speechocean762-{COMMIT}/"
    tmp = dest.with_name(dest.name + ".extracting")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.name.startswith(prefix) or not (m.isfile() or m.isdir()):
                continue
            m.name = m.name[len(prefix):]
            if m.name:
                tar.extract(m, tmp, filter="data")
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)


def verify(dest: Path) -> dict:
    with urllib.request.urlopen(TREE_URL, timeout=60) as r:
        tree = json.load(r)
    if tree.get("truncated"):
        raise SystemExit("GitHub tree listing is truncated; cannot verify every file")
    blobs = {t["path"]: t["sha"] for t in tree["tree"] if t["type"] == "blob"}
    bad = [p for p, sha in blobs.items() if not (dest / p).is_file() or git_blob_sha1(dest / p) != sha]
    if bad:
        raise SystemExit(f"{len(bad)} of {len(blobs)} files missing or with a wrong hash, e.g. {bad[:3]}")
    listing = "".join(f"{blobs[p]}  {p}\n" for p in sorted(blobs))
    return {
        "source": f"https://github.com/{REPO}",
        "commit": COMMIT,
        "license": "CC BY 4.0 (OpenSLR 101)",
        "files_verified": len(blobs),
        "blob_list_sha256": hashlib.sha256(listing.encode()).hexdigest(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", type=Path, default=DATA / f"speechocean762-{COMMIT[:7]}.tar.gz")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    if not args.archive.is_file():
        print(f"downloading {ARCHIVE_URL}", file=sys.stderr)
        download(ARCHIVE_URL, args.archive)
    print(f"archive sha256 {hashlib.sha256(args.archive.read_bytes()).hexdigest()}", file=sys.stderr)
    extract(args.archive, DEST)
    info = verify(DEST)
    (DEST / "VERIFIED.json").write_text(json.dumps(info, indent=2) + "\n")
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
