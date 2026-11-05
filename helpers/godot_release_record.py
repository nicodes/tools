"""Validate the trusted CD record used to select immutable rollback artifacts."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = os.environ.get('GITHUB_REPOSITORY', '')
SHA = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def validate_run(run):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', REPO):
        raise ValueError('Exact caller repository identity is required')
    if (run.get('repository', {}).get('full_name') != REPO or run.get('head_repository', {}).get('full_name') != REPO
            or run.get('path') != '.github/workflows/cd.yml' or run.get('head_branch') != 'main'
            or run.get('event') not in ('push', 'workflow_dispatch') or run.get('conclusion') != 'success'
            or run.get('status') != 'completed' or not SHA.fullmatch(run.get('head_sha', ''))):
        raise ValueError('Rollback requires a successful main-branch CD run from this repository')
    return run['head_sha']


def validate_record(record, run):
    sha = validate_run(run)
    if set(record) != {'repository', 'sha', 'run_id', 'release_id', 'gate_ref', 'config_ref'}:
        raise ValueError('Unexpected release record fields')
    if record.get('repository') != REPO or record.get('sha') != sha or record.get('run_id') != str(run['id']):
        raise ValueError('Release record does not match its trusted workflow run')
    for kind in ('gate', 'config'):
        prefix = f'ghcr.io/{REPO}-{kind}@'
        ref = record.get(kind + '_ref', '')
        if not ref.startswith(prefix) or not DIGEST.fullmatch(ref[len(prefix):]):
            raise ValueError('Invalid immutable image reference')
    if not re.fullmatch(r'[0-9a-f]{64}', record.get('release_id', '')):
        raise ValueError('Missing versioned release identity')
    return record


def write_record():
    sha = os.environ['GITHUB_SHA']
    if os.environ['GITHUB_REPOSITORY'] != REPO or not SHA.fullmatch(sha):
        raise ValueError('Unexpected publication identity')
    manifest = json.loads(Path('release/release-manifest.json').read_text())
    if manifest.get('source_commit') != sha:
        raise ValueError('Verified manifest does not identify the published source revision')
    record = {'repository': REPO, 'sha': sha, 'run_id': os.environ['GITHUB_RUN_ID'],
              'release_id': manifest['release_id']}
    for kind in ('gate', 'config'):
        ref = f'ghcr.io/{REPO}-{kind}:{sha}'
        digest_ref = subprocess.check_output(['docker', 'image', 'inspect', ref,
            '--format', '{{index .RepoDigests 0}}'], text=True).strip()
        record[kind + '_ref'] = digest_ref
    synthetic_run = {'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO},
        'path': '.github/workflows/cd.yml', 'head_branch': 'main', 'event': 'push',
        'status': 'completed', 'conclusion': 'success', 'head_sha': sha, 'id': int(record['run_id'])}
    validate_record(record, synthetic_run)
    Path('release-record.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    if len(sys.argv) == 1:
        write_record()
    else:
        run = json.loads(Path(sys.argv[1]).read_text())
        sha = validate_run(run)
        values = {'sha': sha}
        if len(sys.argv) > 2:
            values.update(validate_record(json.loads(Path(sys.argv[2]).read_text()), run))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key, value in values.items():
                output.write(f'{key}={value}\n')
