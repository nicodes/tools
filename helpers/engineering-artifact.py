#!/usr/bin/env python3
"""Build the release artifact that products install instead of vendoring.

This is the publish half of what vendor-snapshot.py does by copying. It must
select exactly the same files, or a product that installs the artifact would
be running a different set from a product that still vendors, and the two
could not be compared. It therefore imports vendor-snapshot's own selection
rather than restating the rules: one answer to "what is the snapshot".

The tarball is built to be byte-reproducible from a commit -- sorted entries,
no mtimes, no uid/gid/uname, fixed permissions taken from the tree. Two builds
of one revision give one sha256, which is what lets .mise.toml pin a checksum
and what lets anybody recompute it from the tag.
"""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import io
import sys
import tarfile

HERE = Path(__file__).resolve().parent


def _vendor_snapshot():
    """Import the sibling helper whose filename is not a Python identifier."""
    spec = importlib.util.spec_from_file_location('vendor_snapshot', HERE / 'vendor-snapshot.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(repository, revision, output):
    vendor = _vendor_snapshot()
    files, source = vendor.snapshot(repository, revision)
    buffer = io.BytesIO()
    # gzip with mtime=0; tarfile would otherwise stamp the current time into
    # the gzip header and no two builds would agree.
    with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.PAX_FORMAT,
                      compresslevel=9) as archive:
        archive.gzip = True
        for name, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            archive.addfile(info, io.BytesIO(data))
        manifest = (vendor.json.dumps(source, indent=2) + '\n').encode()
        info = tarfile.TarInfo('SOURCE.json')
        info.size, info.mode, info.mtime = len(manifest), 0o644, 0
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        archive.addfile(info, io.BytesIO(manifest))
    payload = _degzip_deterministic(buffer.getvalue())
    Path(output).write_bytes(payload)
    return hashlib.sha256(payload).hexdigest(), sorted(files)


def _degzip_deterministic(payload):
    """Zero the gzip header's mtime byte range, which tarfile sets from now()."""
    if payload[:2] != b'\x1f\x8b':
        raise ValueError('not a gzip stream')
    return payload[:4] + b'\x00\x00\x00\x00' + payload[8:]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=str(HERE.parent))
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    digest, names = build(args.repository, args.revision, args.output)
    print(f'{len(names)} files', file=sys.stderr)
    print(digest)
