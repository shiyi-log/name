#!/usr/bin/env python3
"""Restore repository data files stored in gzip form for local use."""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import tempfile

if __package__:
    from .data_parts import restore_parts
else:
    from data_parts import restore_parts


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOTS = (ROOT / "data", ROOT / "参考文献" / "字义数据")


def restore(root: Path = ROOT) -> int:
    restored = 0
    for data_root in (root / "data", root / "参考文献" / "字义数据"):
        if not data_root.exists():
            continue
        for manifest in sorted(data_root.rglob('*.gz.parts/manifest.json')):
            destination = manifest.parent.with_name(manifest.parent.name[:-9])
            if not destination.exists():
                restore_parts(manifest, destination)
                restored += 1
        for source in sorted(data_root.rglob("*.gz")):
            destination = source.with_suffix("")
            if destination.exists():
                continue
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                with gzip.open(source, "rb") as compressed:
                    while chunk := compressed.read(1024 * 1024):
                        temporary.write(chunk)
            temporary_path.replace(destination)
            restored += 1
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore compressed repository data files")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(f"Restored {restore(args.root.resolve())} compressed data files.")


if __name__ == "__main__":
    main()
