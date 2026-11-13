"""Checksummed gzip parts for Git; local working files stay at their usual paths."""
from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zlib


def iter_uncompressed(manifest: dict, read_part):
    if manifest.get('format') != 'gzip-parts-v1' or not manifest.get('parts'):
        raise ValueError('Invalid data-parts manifest')
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    digest = hashlib.sha256()
    size = 0
    seen = set()
    for part in manifest['parts']:
        name = part['name']
        if not re.fullmatch(r'part-[0-9a-f-]+', name) or name in seen:
            raise ValueError('Invalid or duplicate part name')
        seen.add(name)
        chunk = read_part(name)
        if len(chunk) != part['size'] or hashlib.sha256(chunk).hexdigest() != part['sha256']:
            raise ValueError(f'Data part checksum mismatch: {name}')
        body = decoder.decompress(chunk)
        if decoder.unused_data:
            raise ValueError('Unexpected data after gzip stream')
        size += len(body)
        digest.update(body)
        yield body
    body = decoder.flush()
    size += len(body)
    digest.update(body)
    yield body
    if not decoder.eof or size != manifest['size'] or digest.hexdigest() != manifest['sha256']:
        raise ValueError('Restored data checksum mismatch or truncated gzip stream')


def pack_file(source: Path, part_size: int = 16 * 1024 * 1024) -> Path:
    if part_size < 1:
        raise ValueError('part_size must be positive')
    source = Path(source)
    directory = source.with_name(source.name + '.gz.parts')
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='name-data-pack-') as scratch:
        input_path = source
        if source.suffix == '.db':
            input_path = Path(scratch) / 'snapshot.db'
            with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                with closing(sqlite3.connect(input_path)) as snapshot:
                    db.backup(snapshot)
        digest = hashlib.sha256()
        size = 0
        compressed = Path(scratch) / 'compressed.gz'
        with input_path.open('rb') as incoming, compressed.open('wb') as output:
            with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as archive:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    archive.write(chunk)
        parts = []
        with compressed.open('rb') as incoming:
            while chunk := incoming.read(part_size):
                checksum = hashlib.sha256(chunk).hexdigest()
                name = f'part-{len(parts):05d}-{checksum[:16]}'
                destination = directory / name
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                    output.write(chunk)
                    temporary = Path(output.name)
                temporary.replace(destination)
                parts.append({'name': name, 'size': len(chunk), 'sha256': checksum})
        manifest = {'format': 'gzip-parts-v1', 'size': size, 'sha256': digest.hexdigest(), 'parts': parts}
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.write(chr(10))
            temporary = Path(output.name)
        path = directory / 'manifest.json'
        temporary.replace(path)
        # Delete obsolete parts only after publishing the new manifest.
        active = {part['name'] for part in parts}
        for old in directory.glob('part-*'):
            if old.name not in active:
                old.unlink()
    return path


def restore_parts(manifest_path: Path, destination: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            for chunk in iter_uncompressed(manifest, lambda name: (manifest_path.parent / name).read_bytes()):
                output.write(chunk)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', type=Path, nargs='+')
    parser.add_argument('--part-size-mib', type=int, default=16)
    args = parser.parse_args()
    for path in args.files:
        print(pack_file(path, args.part_size_mib * 1024 * 1024))


if __name__ == '__main__':
    main()
