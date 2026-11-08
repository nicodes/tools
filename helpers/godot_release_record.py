"""Validate a source-bound immutable release record from a successful trusted publication."""
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oci_registry import digest
from release_packet import document, release, read

REPO = os.environ.get('GITHUB_REPOSITORY', '')
SHA = re.compile(r'[a-f0-9]{40}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')


def validate_run(run):
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', REPO):
        raise ValueError('exact caller repository identity is required')
    if (run.get('repository', {}).get('full_name') != REPO or run.get('head_repository', {}).get('full_name') != REPO
            or run.get('path') != '.github/workflows/cd.yml' or run.get('head_branch') != 'main'
            or run.get('event') not in {'push', 'workflow_dispatch'} or run.get('conclusion') != 'success'
            or run.get('status') != 'completed' or not SHA.fullmatch(run.get('head_sha', ''))):
        raise ValueError('release selection requires a successful exact main CD run')
    return run['head_sha']


def validate_record(record, run):
    source = validate_run(run)
    fields = {'kind', 'version', 'repository', 'source_revision', 'run_id', 'run_attempt', 'release_digest'}
    if (not isinstance(record, dict) or set(record) != fields or record.get('kind') != 'komizo.release-record'
            or type(record.get('version')) is not int or record['version'] != 1
            or record['repository'] != REPO or record['source_revision'] != source
            or record['run_id'] != str(run['id']) or record['run_attempt'] != str(run['run_attempt'])
            or not isinstance(record['release_digest'], str) or not DIGEST.fullmatch(record['release_digest'])):
        raise ValueError('release record differs from its trusted source run and attempt')
    return record


def write_record(directory=Path('.artifacts/komizo-release'), output=Path('release-record.json')):
    descriptor = document(directory / 'release.json')
    raw = read(directory / 'release.json')
    if raw != release(descriptor): raise ValueError('release record requires exact canonical descriptor bytes')
    identity = digest(raw)
    publication = document(directory / 'publication.json')
    context = publication.get('execution_context', {})
    if (descriptor['source']['repository'] != REPO or descriptor['source']['revision'] != os.environ['GITHUB_SHA']
            or publication.get('release_digest') != identity
            or any(context.get(key) != os.environ.get(key) for key in ['GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'])):
        raise ValueError('release record requires exact accepted publication source and bytes')
    record = {'kind': 'komizo.release-record', 'version': 1, 'repository': REPO,
              'source_revision': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'],
              'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'release_digest': identity}
    with output.open('x') as stream:
        os.fchmod(stream.fileno(), 0o600); json.dump(record, stream, sort_keys=True); stream.write('\n')
    return record


if __name__ == '__main__':
    if len(sys.argv) == 1: write_record()
    else:
        record = validate_record(document(Path(sys.argv[2])), document(Path(sys.argv[1])))
        print(json.dumps(record, sort_keys=True))
