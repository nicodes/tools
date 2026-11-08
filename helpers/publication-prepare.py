"""Create a closed credential-free publication ZIP from a tested native packet."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_packet import Packet, regular
from oci_registry import canonical
from oci_archive import OCIArchive

MAX_ARCHIVE = 4 << 30
MAX_JSON = 65536
SHA = re.compile(r'[a-f0-9]{40}\Z')


def identity(environment, git):
    repository = environment.get('GITHUB_REPOSITORY', '')
    revision = environment.get('GITHUB_SHA', '')
    if (environment.get('GITHUB_REF') != 'refs/heads/main'
            or environment.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository)
            or not SHA.fullmatch(revision)):
        raise ValueError('publication requires an exact main workflow identity')
    if git(['rev-parse', 'HEAD']) != revision or git(['status', '--porcelain', '--untracked-files=no']):
        raise ValueError('publication requires the unchanged tested checkout')
    tree = git(['rev-parse', 'HEAD^{tree}'])
    if not SHA.fullmatch(tree):
        raise ValueError('publication has no immutable source tree')
    values = [environment.get(key, '') for key in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT']]
    if any(not re.fullmatch('[1-9][0-9]{0,19}', value) for value in values):
        raise ValueError('publication requires exact workflow run and attempt')
    return {'version': 1, 'repository': repository, 'revision': revision, 'tree': tree,
            'run_id': int(values[0]), 'attempt': int(values[1])}


def prepare(root, environment, *, packet_directory=None, output=None, git=None, timeout=600):
    root = Path(root).resolve()
    deadline = time.monotonic() + timeout
    def source_metadata(arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('publication preparation exceeded its deadline')
        result = subprocess.run(['git', '-C', str(root), *arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=min(30, remaining), check=True)
        if len(result.stdout) > MAX_JSON:
            raise ValueError('publication source metadata exceeds its bound')
        return result.stdout.decode().strip()
    source = identity(environment, git or source_metadata)
    packet = Packet(packet_directory or root / '.artifacts/release', environment=environment, timeout=timeout)
    if packet.descriptor['source']['repository'] != source['repository'] or packet.descriptor['source']['revision'] != source['revision']:
        raise ValueError('publication packet belongs to another source')
    # This preparation process has no registry authority and never loads an engine.
    for name, artifact in packet.descriptor['artifacts'].items():
        if artifact['kind'] == 'image':
            with OCIArchive(packet.root / ('images/' + name + '.oci.tar'), expected_config=artifact['config_digest']) as archive:
                if archive.metadata['manifest_digest'] != artifact['digest'] or archive.metadata['platform'] != artifact['platform']:
                    raise ValueError('OCI archive differs from its tested descriptor')
    packet.unchanged()
    target = Path(output or root / '.artifacts/release-publish.zip')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValueError('publication ZIP requires a fresh ordinary destination')
    try:
        with target.open('xb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.writestr('publication-source.json', canonical(source))
            for name in [*packet.files, 'inventory.json']:
                if time.monotonic() >= deadline:
                    raise ValueError('publication preparation exceeded its deadline')
                with regular(packet.root / name) as (payload, _), archive.open(name, 'w', force_zip64=True) as destination:
                    while True:
                        block = payload.read(1 << 20)
                        if not block: break
                        destination.write(block)
                        if stream.tell() > MAX_ARCHIVE or time.monotonic() >= deadline:
                            raise ValueError('publication ZIP exceeds its size or time bound')
            packet.unchanged()
        if target.stat().st_size > MAX_ARCHIVE:
            raise ValueError('publication ZIP exceeds its bound')
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--packet', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print(prepare(args.root, os.environ, packet_directory=args.packet, output=args.output))


if __name__ == '__main__': main()
