#!/usr/bin/env python3
"""Package popnow/ as a Blender extension zip in dist/.

The zip layout matches `blender --command extension build`: blender_manifest.toml sits at the root of the archive.
It builds without Blender (no bpy needed). File times are fixed, so unchanged sources produce a byte-identical zip,
which makes releases easy to compare.

Usage (from the repository root):
  python3 scripts/build_zip.py            # writes dist/popnow-blender-<version>.zip
  python3 scripts/build_zip.py --out DIR  # writes the zip into DIR instead
  python3 scripts/build_zip.py --check    # builds in memory and checks the archive, writes nothing
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "popnow"
DIST = ROOT / "dist"
FIXED_TIME = (2026, 10, 16, 0, 0, 0)
EXCLUDE = ["__pycache__", "*.pyc", ".DS_Store", "*.zip", "*.part"]


def manifest() -> dict:
    with open(SRC / "blender_manifest.toml", "rb") as f:
        return tomllib.load(f)


def files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = sorted(d for d in dirnames if not any(fnmatch.fnmatch(d, p) for p in EXCLUDE))
        for name in sorted(filenames):
            if any(fnmatch.fnmatch(name, p) for p in EXCLUDE):
                continue
            out.append(Path(dirpath) / name)
    return out


def build_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files():
            info = zipfile.ZipInfo(p.relative_to(SRC).as_posix(), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def zip_name() -> str:
    return f"popnow-blender-{manifest()['version']}.zip"


def check(data: bytes) -> list[str]:
    """Problems with a built archive (an empty list when it is fine)"""
    problems = []
    if data != build_bytes():
        problems.append("two builds of the same sources differ")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
    for required in ("blender_manifest.toml", "__init__.py", "core/models.json", "licenses/AI-Render-MIT.txt"):
        if required not in names:
            problems.append(f"missing {required}")
    for n in names:
        if any(fnmatch.fnmatch(part, p) for part in n.split("/") for p in EXCLUDE):
            problems.append(f"excluded file packed: {n}")
    return problems


def main(argv: list[str]) -> int:
    data = build_bytes()
    if "--check" in argv:
        problems = check(data)
        for p in problems:
            print(p)
        if not problems:
            print(f"{zip_name()} builds cleanly ({len(data):,} bytes)")
        return 1 if problems else 0
    out_dir = DIST
    if "--out" in argv:
        out_dir = Path(argv[argv.index("--out") + 1]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / zip_name()
    target.write_bytes(data)
    print(f"{target}  {len(data):,} bytes  sha256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
