#!/usr/bin/env python3
"""Build and exercise the actual archive for an immutable release candidate."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--output-directory', type=Path, default=Path('.artifacts/consumers'))
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', args.revision+'^{commit}'], text=True).strip()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    archive = args.output_directory/'engineering.tar.gz'
    digest = subprocess.check_output([sys.executable, str(ROOT/'helpers/engineering-artifact.py'),
        '--revision', revision, '--output', str(archive)], text=True).strip()
    with tarfile.open(archive, 'r:gz') as source:
        source_digest = hashlib.sha256(source.extractfile('SOURCE.json').read()).hexdigest()
    command = [sys.executable, str(ROOT/'helpers/consumer-check.py'), '--archive', str(archive.resolve()),
               '--revision', revision, '--archive-sha256', digest, '--source-sha256', source_digest,
               '--output', str(args.output_directory/'consumer-check.json')]
    if args.postgres:
        command.append('--postgres')
    subprocess.run(command, check=True)
    print(json.dumps({'revision': revision, 'archive_sha256': digest, 'source_sha256': source_digest}))


if __name__ == '__main__':
    main()
