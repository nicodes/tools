"""Publish an immutable, closed release payload only after checking uploaded bytes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


def run(*arguments, **options):
    return subprocess.check_output(arguments, text=True, **options).strip()


def tag(value):
    if not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v[0-9]+\.[0-9]+\.[0-9]+', value):
        raise ValueError('expected a stable semantic release tag')
    return value


def version(bump, prefix, tags, initial='first'):
    if bump not in {'patch', 'minor', 'major'} or initial not in {'first', 'bump'}:
        raise ValueError('invalid version bump or initial version policy')
    if not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v', prefix):
        raise ValueError('invalid tag prefix')
    versions = [tuple(map(int, value[len(prefix):].split('.'))) for value in tags
                if re.fullmatch(re.escape(prefix) + r'[0-9]+\.[0-9]+\.[0-9]+', value)]
    if not versions and initial == 'first':
        parts = (0, 0, 1)
    else:
        major, minor, patch = max(versions, default=(0, 0, 0))
        parts = {'patch': (major, minor, patch + 1),
                 'minor': (major, minor + 1, 0), 'major': (major + 1, 0, 0)}[bump]
    number = '.'.join(map(str, parts))
    return number, prefix + number


def absent(repo, endpoint):
    result = subprocess.run(['gh', 'api', '--include', f'repos/{repo}/{endpoint}'],
                            text=True, capture_output=True, check=False)
    status = re.search(r'^HTTP/\S+ (\d+)', result.stdout, re.M)
    if result.returncode == 0:
        raise ValueError('release tags and releases must be new; existing objects are immutable')
    if not status or status[1] != '404':
        raise ValueError('could not establish that the release identity is unused')


def validate_request(repo, source, release_tag, environment=None):
    environment = environment or os.environ
    tag(release_tag)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('invalid repository')
    if environment.get('GITHUB_REPOSITORY', repo) != repo:
        raise ValueError('publication must target the workflow repository')
    if not re.fullmatch(r'[a-f0-9]{40}', source) or source != environment.get('GITHUB_SHA'):
        raise ValueError('publication must use this workflow source commit')
    if environment.get('GITHUB_REF') != 'refs/heads/main' or environment.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ValueError('publication requires an explicit main-branch dispatch')


def payload(directory, expected):
    directory = Path(directory)
    if directory.is_symlink() or not expected:
        raise ValueError('release payload must be a nonempty regular directory')
    actual = {path.name for path in directory.iterdir()}
    if actual != set(expected):
        raise ValueError('release directory differs from the accepted closed payload')
    for name, digest in expected.items():
        if (not re.fullmatch(r'[A-Za-z0-9@][A-Za-z0-9@._-]*', name)
                or not re.fullmatch(r'[a-f0-9]{64}', digest)):
            raise ValueError('invalid release asset identity')
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('release assets must be regular files')
        with path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                raise ValueError('release bytes changed after acceptance: ' + name)
    return directory.resolve()


def publish(directory, expected, release_tag, source, repo, notes=None):
    validate_request(repo, source, release_tag)
    directory = payload(directory, expected)
    existing = run('gh', 'api', '--paginate', f'repos/{repo}/releases?per_page=100', '--jq', '.[].tag_name')
    if release_tag in existing.splitlines():
        raise ValueError('release identities must be new, including drafts')
    endpoint = 'git/ref/tags/' + quote(release_tag, safe='')
    absent(repo, endpoint)
    absent(repo, 'releases/tags/' + quote(release_tag, safe=''))
    # Claim the exact source ref atomically. Never let release creation attach to
    # a tag that another publisher created between the absence check and upload.
    run('gh', 'api', '--method', 'POST', f'repos/{repo}/git/refs',
        '-f', 'ref=refs/tags/' + release_tag, '-f', 'sha=' + source)
    files = [str(directory / name) for name in sorted(expected)]
    subprocess.run(['gh', 'release', 'create', release_tag, *files, '--repo', repo,
                    '--target', source, '--verify-tag', '--draft', '--title', release_tag,
                    '--notes', notes or 'Prebuilt release from exact source ' + source + '.'], check=True)
    release_id = run('gh', 'release', 'view', release_tag, '--repo', repo,
                     '--json', 'databaseId', '--jq', '.databaseId')
    if not re.fullmatch(r'[1-9][0-9]*', release_id):
        raise ValueError('GitHub did not identify the uploaded draft')
    uploaded = json.loads(run('gh', 'api', f'repos/{repo}/releases/{release_id}'))
    ref = json.loads(run('gh', 'api', f'repos/{repo}/{endpoint}'))
    actual = {asset['name']: asset.get('digest') for asset in uploaded['assets']}
    if (not uploaded['draft'] or uploaded['tag_name'] != release_tag or uploaded['target_commitish'] != source
            or len(actual) != len(uploaded['assets'])
            or actual != {name: 'sha256:' + digest for name, digest in expected.items()}
            or ref.get('object', {}).get('type') != 'commit' or ref['object']['sha'] != source):
        raise ValueError('uploaded draft differs from the verified payload; it remains unpublished')
    subprocess.run(['gh', 'release', 'edit', release_tag, '--repo', repo, '--draft=false'], check=True)
