#!/usr/bin/env python3
"""Exercise a checksum-pinned engineering archive from an isolated installation."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


SCENARIOS = {
    'bootstrap': ['test_engineering_bootstrap'],
    'publication': ['test_release_binding'],
    'backup-transport': ['test_snapshot', 'test_receive_backup', 'test_upload_backup'],
    'recovery': ['test_restore_drill', 'test_postgres_recovery.PostgreSQLArchiveBoundaries'],
}
RUNNER = '''import json, sys, unittest
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
result = unittest.TextTestRunner(verbosity=1).run(suite)
print(json.dumps({'tests': result.testsRun, 'skipped': len(result.skipped),
                  'success': result.wasSuccessful()}))
raise SystemExit(not result.wasSuccessful() or bool(result.skipped) or result.testsRun == 0)
'''


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install(archive, destination, revision, archive_sha256, source_sha256):
    """Validate the independent pins and every member before executing any code."""
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('consumer source requires a full commit')
    if any(not re.fullmatch(r'[a-f0-9]{64}', value) for value in (archive_sha256, source_sha256)):
        raise ValueError('consumer archive and manifest require independent SHA256 pins')
    if archive.is_symlink() or checksum(archive) != archive_sha256:
        raise ValueError('consumer archive checksum differs')
    with tarfile.open(archive, 'r:gz') as incoming:
        members = incoming.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or 'SOURCE.json' not in names or len(names) > 10000:
            raise ValueError('consumer archive inventory is incomplete or duplicated')
        if sum(member.size for member in members) > 128 * 1024**2:
            raise ValueError('consumer archive exceeds its expansion bound')
        for member in members:
            if (not member.isfile() or member.size < 0 or
                    not re.fullmatch(r'SOURCE\.json|(?:helpers|tests)/[\w.-]+', member.name, re.ASCII)):
                raise ValueError('consumer archive contains an unsafe member')
        raw = incoming.extractfile('SOURCE.json').read()
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise ValueError('consumer source manifest checksum differs')
        source = json.loads(raw)
        if (source.get('revision') != revision or source.get('repository') != 'https://github.com/nicodes/tools'
                or not isinstance(source.get('files'), dict) or not source['files']
                or set(names) != {'SOURCE.json', *source['files']}):
            raise ValueError('consumer source identity or inventory differs')
        destination.mkdir(mode=0o700)
        for member in members:
            data = incoming.extractfile(member).read()
            if member.name != 'SOURCE.json' and hashlib.sha256(data).hexdigest() != source['files'][member.name]:
                raise ValueError('consumer helper differs from the reviewed manifest')
            target = destination/member.name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def check(archive, revision, archive_sha256, source_sha256, postgres=False):
    report = {'version': 1, 'revision': revision, 'archive_sha256': archive_sha256,
              'source_sha256': source_sha256, 'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scenarios': {}, 'success': False,
              'scope': 'Installed helper consumers with disposable fixtures; registry and object storage use test transports. No production acceptance.'}
    with tempfile.TemporaryDirectory(prefix='engineering-consumer-') as directory:
        root = Path(directory)/'installed'
        install(archive, root, revision, archive_sha256, source_sha256)
        scenarios = dict(SCENARIOS)
        if postgres:
            scenarios['postgres-restore'] = ['test_postgres_recovery.PostgreSQLRestoreIntegration']
        # No source-checkout imports, credentials, or inherited GitHub context.
        env = {key: value for key, value in os.environ.items()
               if key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SYSTEMROOT')}
        env.update(PYTHONPATH=str(root/'tests'), PYTHONDONTWRITEBYTECODE='1',
                   CICD_TEST_POSTGRES='1' if postgres else '0')
        for name, modules in scenarios.items():
            result = subprocess.run([sys.executable, '-c', RUNNER, *modules], cwd=root,
                                    env=env, capture_output=True, text=True, timeout=600)
            try:
                outcome = json.loads(result.stdout.splitlines()[-1])
            except (ValueError, IndexError):
                outcome = {'tests': 0, 'skipped': 0, 'success': False}
            outcome['success'] = (result.returncode == 0 and outcome.get('success') is True
                                  and outcome.get('tests', 0) > 0 and outcome.get('skipped') == 0)
            report['scenarios'][name] = outcome
            if not outcome['success']:
                print(result.stderr[-12000:], file=sys.stderr)
        report['success'] = all(value['success'] for value in report['scenarios'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--archive-sha256', required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--postgres', action='store_true', help='require real isolated PostgreSQL restoration')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = check(args.archive, args.revision, args.archive_sha256, args.source_sha256, args.postgres)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
