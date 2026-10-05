#!/usr/bin/env python3
"""Open the engineering-snapshot bump in every product, for one cicd release.

WHY THIS IS NOT A WORKFLOW. The bump edits .github/workflows -- the
`uses: nicodes/cicd/.github/workflows/*.yml@<sha>` pins, which helpers/pins.mjs
requires to name the same commit as the installed snapshot. GITHUB_TOKEN is
refused when it pushes a workflow file:

    refusing to allow a GitHub App to create or update workflow
    `.github/workflows/backup.yml` without `workflows` permission

That permission needs a PAT or an App credential with write access to all
nine products across three owners -- a standing push-to-any-workflow secret,
which is precisely what docs/releases.md says this repository will not hold.
So this runs on the operator's machine with the operator's own rights, for
the same reason `release.sh prepare` does.

What it removes is the hand-work, not the human: nine clones, nine identical
edits, nine pull requests. It pushes nothing without --push.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRANCH = 'deps/engineering-{version}'
# Line-wise, not one regex over the whole entry: the URL carries a literal
# "{{ version }}", so anything written as [^}]* stops inside the template
# rather than at the end of the table.
MISE_LINE = re.compile(r'^\s*"http:cicd-engineering"\s*=', re.M)
MISE_VERSION = re.compile(r'(\bversion\s*=\s*")([^"]+)(")')
MISE_CHECKSUM = re.compile(r'(\bchecksum\s*=\s*"sha256:)([a-f0-9]{64})(")')
WORKFLOW_PIN = re.compile(
    r'(nicodes/(?:cicd|tools)/\.github/workflows/[a-z-]+\.yml)@[a-f0-9]{40}([ \t]*#[ \t]*v?[0-9.]+)?')


def bump_mise(text, version, checksum):
    """Point .mise.toml at a release. Returns (text, changed)."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if MISE_LINE.match(line)]
    if not hits:
        raise ValueError('no "http:cicd-engineering" entry to bump')
    if len(hits) > 1:
        raise ValueError('more than one "http:cicd-engineering" entry')
    line = lines[hits[0]]
    bumped, versions = MISE_VERSION.subn(rf'\g<1>{version}\g<3>', line, count=1)
    bumped, sums = MISE_CHECKSUM.subn(rf'\g<1>{checksum}\g<3>', bumped, count=1)
    if not versions or not sums:
        raise ValueError('the "http:cicd-engineering" entry has no version or no checksum')
    lines[hits[0]] = bumped
    new = ''.join(lines)
    return new, new != text


def bump_workflow(text, version, commit):
    """Repin the cicd reusable-workflow calls. Returns (text, changed)."""
    new = WORKFLOW_PIN.sub(rf'\1@{commit} # v{version}', text)
    return new, new != text


def release_facts(version):
    """The commit a release tag peels to, and its artifact's sha256."""
    tag = f'v{version}'
    commit = subprocess.run(['gh', 'api', f'repos/nicodes/tools/commits/{tag}', '-q', '.sha'],
                            capture_output=True, text=True, timeout=60)
    if commit.returncode != 0 or not re.fullmatch(r'[a-f0-9]{40}', commit.stdout.strip()):
        raise SystemExit(f'{tag}: no such release commit')
    notes = subprocess.run(['gh', 'release', 'view', tag, '-R', 'nicodes/tools', '--json', 'body',
                            '-q', '.body'], capture_output=True, text=True, timeout=60)
    found = re.search(r'sha256:([a-f0-9]{64})', notes.stdout or '')
    if not found:
        raise SystemExit(f'{tag}: the release notes carry no artifact sha256')
    return commit.stdout.strip(), found.group(1)


def apply(root, version, commit, checksum):
    """Every edit this bump makes in one product. Returns the files touched."""
    touched = []
    mise = root / '.mise.toml'
    text, changed = bump_mise(mise.read_text(), version, checksum)
    if changed:
        mise.write_text(text)
        touched.append('.mise.toml')
    for path in sorted((root / '.github/workflows').glob('*.yml')):
        text, changed = bump_workflow(path.read_text(), version, commit)
        if changed:
            path.write_text(text)
            touched.append(str(path.relative_to(root)))
    return touched


def git(root, *arguments, check=True):
    return subprocess.run(['git', '-C', str(root), *arguments], check=check,
                          capture_output=True, text=True, timeout=120)


def product(repo, version, commit, checksum, push):
    with tempfile.TemporaryDirectory(prefix='fanout-') as directory:
        root = Path(directory) / 'repo'
        clone = subprocess.run(['gh', 'repo', 'clone', repo, str(root), '--', '--depth', '1'],
                               capture_output=True, text=True, timeout=300)
        if clone.returncode != 0:
            return f'{repo}: clone failed: {clone.stderr.strip()}'
        branch = BRANCH.format(version=version)
        git(root, 'checkout', '-q', '-B', branch)
        try:
            touched = apply(root, version, commit, checksum)
        except ValueError as error:
            return f'{repo}: {error}'
        if not touched:
            return f'{repo}: already on v{version}'
        git(root, 'add', '-A')
        git(root, 'commit', '-q', '-m', MESSAGE.format(version=version, commit=commit))
        if not push:
            return f'{repo}: would bump {len(touched)} file(s) (no --push)'
        git(root, 'push', '-q', '-f', 'origin', branch)
        made = subprocess.run(
            ['gh', 'pr', 'create', '-R', repo, '--head', branch, '--base', 'main',
             '--title', f'build(deps): engineering snapshot v{version}',
             '--body', MESSAGE.format(version=version, commit=commit)],
            capture_output=True, text=True, timeout=120)
        return f'{repo}: {(made.stdout or made.stderr).strip().splitlines()[-1]}'


MESSAGE = """build(deps): engineering snapshot v{version}

Moves this product to cicd v{version} ({commit}).

Both pins move together because helpers/pins.mjs requires it: the installed
snapshot's revision and every nicodes/cicd reusable-workflow call must name
one commit, or the product is running helper code from one revision and
workflows from another.

Opened by scripts/fanout.py from nicodes/cicd.
"""


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('version', help='the release to move the fleet to, without the v')
    parser.add_argument('--fleet', type=Path, required=True)
    parser.add_argument('--push', action='store_true', help='push branches and open pull requests')
    parser.add_argument('--only', action='append', default=[], metavar='REPO',
                        help='limit to these products; repeat for several')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        raise SystemExit('version must be X.Y.Z, without the leading v')
    commit, checksum = release_facts(args.version)
    fleet = json.loads(args.fleet.read_text())
    repos = args.only or sorted(fleet['products'])
    print(f'v{args.version} = {commit}  sha256:{checksum[:12]}', file=sys.stderr)
    failures = 0
    for repo in repos:
        line = product(repo, args.version, commit, checksum, args.push)
        print(line)
        if 'failed' in line or ': no ' in line:
            failures += 1
    sys.exit(1 if failures else 0)
