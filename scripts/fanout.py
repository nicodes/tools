#!/usr/bin/env python3
"""Review and adopt one verified tools release through isolated worktrees.

All supported orchestration references move with the package and independent
source manifest pin. Dry runs print complete diffs without editing files;
--push commits ordinary branches and opens or resumes adoption PRs. The caller
supplies repository checkouts, a worktree root, and an outcome manifest.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import hashlib
import tarfile
import difflib
import yaml
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
    r'(nicodes/(?:cicd|tools)/(?:make|workflow-standards|godot-setup|\.github/workflows/[a-z-]+\.ya?ml))@[a-f0-9]{40}([ \t]*#[ \t]*v?[0-9.]+)?')
TOOLS_REFERENCE = re.compile(r'uses:\s*[\"\']?(nicodes/(?:cicd|tools)/[^\s\"\']+)')
DOWNLOAD_ARTIFACT = 'actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c'


def wire_test_evidence(text):
    """Bind each shared publisher to its actual prerequisite job ID."""
    if not re.search(r'helpers/release\.py[\"\']?\s+publish\b', text):
        return text
    document = yaml.safe_load(text)
    if document.get('runs', {}).get('using') == 'composite':
        if 'engineering-test-evidence' in text:
            downloads = [step for step in document['runs']['steps'] if step.get('id') == 'engineering-test-evidence']
            if len(downloads) != 1 or downloads[0].get('uses') != DOWNLOAD_ARTIFACT or downloads[0].get('with') != {'pattern': 'engineering-test-*', 'path': '.artifacts/test-evidence'}:
                raise ValueError('composite publication evidence differs from the reviewed contract')
            for step in document['runs']['steps']:
                if re.search(r'helpers/release\.py[\"\']?\s+publish\b', step.get('run', '')) and not re.search(r'--test-job test(?:\s|$)', step['run']):
                    raise ValueError('composite publisher declares a different Test job')
            return text
        addition = ('  steps:\n'
                    '    - name: Download same-run engineering Test evidence\n'
                    '      id: engineering-test-evidence\n'
                    f'      uses: {DOWNLOAD_ARTIFACT} # v8.0.1\n'
                    '      with:\n'
                    '        pattern: engineering-test-*\n'
                    '        path: .artifacts/test-evidence\n')
        if text.count('  steps:\n') != 1:
            raise ValueError('unsupported composite publication layout')
        text = text.replace('  steps:\n', addition, 1)
        return re.sub(r'(^[^\n]*helpers/release\.py[\"\']?\s+publish[^\n]*)(\n|$)',
                      r'\1 --test-job test\2', text, flags=re.M)
    jobs = document.get('jobs', {})
    blocks = list(re.finditer(r'^  ([A-Za-z0-9_-]+):\s*$', text, re.M))
    updates = []
    for index, match in enumerate(blocks):
        name = match[1]
        if name not in jobs:
            continue
        job = jobs[name]
        steps = job.get('steps', [])
        publishers = [step for step in steps if re.search(r'helpers/release\.py[\"\']?\s+publish\b', step.get('run', ''))]
        if not publishers:
            continue
        needs = job.get('needs', [])
        needs = [needs] if isinstance(needs, str) else needs
        candidates = [dependency for dependency in needs if any(
            re.fullmatch(r'nicodes/(?:tools|cicd)/make@[a-f0-9]{40}', step.get('uses', ''))
            and {'test', 'check'} & set(step.get('with', {}).get('target', '').split())
            for step in jobs.get(dependency, {}).get('steps', []))]
        own = [step for step in steps if re.fullmatch(r'nicodes/(?:tools|cicd)/make@[a-f0-9]{40}', step.get('uses', ''))
               and 'build' in step.get('with', {}).get('target', '').split()]
        same_job = not candidates and len(own) == 1
        if len(candidates) != 1 and not same_job:
            raise ValueError(f'{name}: publication requires exactly one prerequisite engineering Test job')
        test_job = name if same_job else candidates[0]
        end = blocks[index+1].start() if index+1 < len(blocks) else len(text)
        block = text[match.start():end]
        existing = [step for step in steps if step.get('id') == 'engineering-test-evidence']
        expected = {'pattern': f'engineering-{test_job}-*', 'path': '.artifacts/test-evidence'}
        if same_job:
            target = own[0]['with']['target']
            if 'test' not in target.split() and 'check' not in target.split():
                new_target = target.replace('build', 'test build', 1)
                pattern = re.compile(r'^(\s*target:\s*)'+re.escape(target)+r'\s*$', re.M)
                block, count = pattern.subn(lambda match: match[1]+new_target, block)
                if count != 1:
                    raise ValueError(f'{name}: unsupported build target layout')
            if existing:
                raise ValueError(f'{name}: same-job publication must use its own test receipt')
        elif existing:
            download_index = next(i for i, step in enumerate(steps) if step.get('id') == 'engineering-test-evidence')
            if any(step.get('uses', '').startswith('actions/checkout@') for step in steps[download_index+1:]):
                raise ValueError(f'{name}: checkout would delete downloaded Test evidence')
            if len(existing) != 1 or existing[0].get('uses') != DOWNLOAD_ARTIFACT or existing[0].get('with') != expected:
                raise ValueError(f'{name}: existing Test evidence download differs from the reviewed contract')
        else:
            addition = ('      - name: Download same-run engineering Test evidence\n'
                        '        id: engineering-test-evidence\n'
                        f'        uses: {DOWNLOAD_ARTIFACT} # v8.0.1\n'
                        '        with:\n'
                        f'          pattern: engineering-{test_job}-*\n'
                        '          path: .artifacts/test-evidence\n')
            # Download after checkout: checkout's default git clean removes
            # untracked evidence downloaded into the workspace beforehand.
            step_blocks = list(re.finditer(r'^      - ', block, re.M))
            publication = None
            for step_index, step_start in enumerate(step_blocks):
                step_end = step_blocks[step_index+1].start() if step_index+1 < len(step_blocks) else len(block)
                if re.search(r'helpers/release\.py[\"\']?\s+publish\b', block[step_start.start():step_end]):
                    publication = step_start.start()
                    break
            if publication is None or re.search(r'uses:\s*actions/checkout@', block[publication:]):
                raise ValueError(f'{name}: unsupported checkout/publication step order')
            block = block[:publication]+addition+block[publication:]
        lines = block.splitlines(keepends=True)
        for line_index, line in enumerate(lines):
            if not re.search(r'helpers/release\.py[\"\']?\s+publish\b', line):
                continue
            if line.rstrip().endswith('\\'):
                raise ValueError(f'{name}: unsupported multiline publisher command')
            option = re.search(r'--test-job\s+([^\s]+)', line)
            if option and option[1] != test_job:
                raise ValueError(f'{name}: publisher declares a different Test job')
            if not option:
                lines[line_index] = line.rstrip('\n')+f' --test-job {test_job}\n'
            if same_job and '--test-evidence-directory' not in lines[line_index]:
                lines[line_index] = lines[line_index].rstrip('\n')+' --test-evidence-directory .artifacts/contract\n'
        updates.append((match.start(), end, ''.join(lines)))
    for start, end, block in reversed(updates):
        text = text[:start]+block+text[end:]
    return text


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
    """Repin supported immutable tools consumers, rejecting unknown formats."""
    for reference in TOOLS_REFERENCE.findall(text):
        if not WORKFLOW_PIN.fullmatch(reference):
            raise ValueError(f'unsupported or non-immutable tools reference: {reference}')
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
    checksum = found.group(1)
    with tempfile.TemporaryDirectory(prefix='engineering-release-') as directory:
        archive = Path(directory)/f'cicd-engineering-{tag}.tar.gz'
        subprocess.run(['gh', 'release', 'download', tag, '-R', 'nicodes/tools',
                        '-p', archive.name, '-D', directory], check=True, timeout=300)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
            raise ValueError('release archive differs from its published checksum')
        with tarfile.open(archive) as stream:
            manifests = [member for member in stream.getmembers()
                         if member.name.rstrip('/').split('/')[-1] == 'SOURCE.json']
            if len(manifests) != 1 or not manifests[0].isfile():
                raise ValueError('release must contain one regular SOURCE.json')
            source_bytes = stream.extractfile(manifests[0]).read()
        source = json.loads(source_bytes)
        if source.get('revision') != commit.stdout.strip():
            raise ValueError('release source manifest revision differs from tag')
        source_digest = hashlib.sha256(source_bytes).hexdigest()
    return commit.stdout.strip(), checksum, source_digest


def apply(root, version, commit, checksum, source_digest=None, dry_run=False, reviewed_policy=None, change_manifest=None):
    """Every edit this bump makes in one product. Returns the files touched."""
    updates = {}
    mise = root / '.mise.toml'
    text, changed = bump_mise(mise.read_text(), version, checksum)
    if changed:
        updates[mise] = text
    paths = sorted(set((root/'.github/workflows').glob('*.y*ml')) |
                   set((root/'.github/actions').rglob('action.y*ml')))
    for path in paths:
        text, changed = bump_workflow(path.read_text(), version, commit)
        if tuple(map(int, version.split('.'))) >= (0, 14, 0):
            text = wire_test_evidence(text)
            changed = text != path.read_text()
        if changed:
            updates[path] = text
    pin_path = root/'engineering-pin.json'
    if pin_path.exists():
        if not source_digest or not re.fullmatch(r'[a-f0-9]{64}', source_digest):
            raise ValueError('engineering pin requires the verified release manifest digest')
        pin = json.loads(pin_path.read_text())
        if pin.get('repository') not in ('https://github.com/nicodes/tools', 'https://github.com/nicodes/cicd'):
            raise ValueError('unknown engineering pin repository')
        pin.update(repository='https://github.com/nicodes/tools', revision=commit, source_sha256=source_digest)
        text = json.dumps(pin, indent=2)+'\n'
        if text != pin_path.read_text():
            updates[pin_path] = text
    policy_path = root/'fleet-policy.json'
    if policy_path.exists():
        old_bytes = policy_path.read_bytes()
        old_policy = json.loads(old_bytes)
        if reviewed_policy is None:
            if old_policy['baseline']['snapshot_revision'] != commit:
                raise ValueError('release adoption requires a reviewed fleet policy snapshot')
        else:
            policy = json.loads(reviewed_policy)
            expected = json.loads(old_bytes)
            expected['baseline']['snapshot_revision'] = commit
            if policy != expected:
                raise ValueError('reviewed release policy must change only the snapshot revision')
            env_path = root/'scripts/engineering-env.sh'
            env = env_path.read_text()
            pattern = re.compile(r'(FLEET_BASELINE_SHA256[^\n]*:-)([a-f0-9]{64})([}])')
            matches = pattern.findall(env)
            if len(matches) != 1 or matches[0][1] != hashlib.sha256(old_bytes).hexdigest():
                raise ValueError('existing fleet policy digest is missing or incoherent')
            digest = hashlib.sha256(reviewed_policy).hexdigest()
            new_env = pattern.sub(lambda match: match[1]+digest+match[3], env)
            if reviewed_policy != old_bytes:
                updates[policy_path] = reviewed_policy.decode()
            if new_env != env:
                updates[env_path] = new_env
    # Validate the entire change before writing any file. Other action pin
    # records (for example komizo-actions) belong to their own releases.
    for path, text in updates.items():
        before = path.read_text()
        if change_manifest is not None:
            change_manifest.append({'path': str(path.relative_to(root)),
                'before_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'after_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'before_tools_refs': [match.group(0) for match in WORKFLOW_PIN.finditer(before)],
                'after_tools_refs': [match.group(0) for match in WORKFLOW_PIN.finditer(text)]})
        if dry_run:
            print(''.join(difflib.unified_diff(path.read_text().splitlines(True),
                  text.splitlines(True), fromfile=str(path.relative_to(root)),
                  tofile=str(path.relative_to(root)))), end='')
        else:
            path.write_text(text)
    return [str(path.relative_to(root)) for path in updates]


def git(root, *arguments, check=True):
    return subprocess.run(['git', '-C', str(root), *arguments], check=check,
                          capture_output=True, text=True, timeout=120)


def product(repo, version, commit, checksum, source_digest, push, checkout_root, worktree_root, reviewed_policy=None, change_manifest=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('invalid repository name')
    checkout = checkout_root/repo
    root = worktree_root/f'engineering-{version}-{repo.replace("/", "-")}'
    branch = BRANCH.format(version=version)
    git(checkout, 'fetch', 'origin', 'main')
    if not root.exists():
        # Resume an existing branch without resetting it or force pushing.
        exists = git(checkout, 'show-ref', '--verify', f'refs/heads/{branch}', check=False).returncode == 0
        arguments = ['worktree', 'add', str(root)]
        git(checkout, *(arguments+[branch] if exists else arguments+['-b', branch, 'FETCH_HEAD']))
    if git(root, 'branch', '--show-current').stdout.strip() != branch:
        raise ValueError('adoption worktree is on another branch')
    if git(root, 'status', '--porcelain').stdout.strip():
        raise ValueError('adoption worktree has uncommitted changes; preserve and resolve them first')
    try:
        touched = apply(root, version, commit, checksum, source_digest, dry_run=not push, reviewed_policy=reviewed_policy, change_manifest=change_manifest)
    except ValueError as error:
        raise ValueError(f'{repo}: {error}') from error
    if not push:
        return f'{repo}: would bump {len(touched)} file(s) (no --push)' if touched else f'{repo}: already on v{version}'
    if not touched and git(root, 'rev-parse', 'HEAD').stdout == git(checkout, 'rev-parse', 'FETCH_HEAD').stdout:
        return f'{repo}: already on v{version}'
    if touched:
        git(root, 'add', '--', *touched)
        git(root, 'commit', '-q', '-m', MESSAGE.format(version=version, commit=commit))
    git(root, 'push', '-q', 'origin', branch)
    existing = subprocess.run(['gh', 'pr', 'list', '-R', repo, '--head', branch,
                              '--json', 'url', '--jq', '.[0].url'], capture_output=True, text=True, check=True)
    if existing.stdout.strip():
        return f'{repo}: {existing.stdout.strip()}'
    body = root.parent/f'{root.name}-pr.txt'
    body.write_text(MESSAGE.format(version=version, commit=commit))
    made = subprocess.run(
        ['gh', 'pr', 'create', '-R', repo, '--head', branch, '--base', 'main',
         '--title', f'build(deps): engineering snapshot v{version}',
         '--body-file', str(body)],
        capture_output=True, text=True, timeout=120)
    return f'{repo}: {(made.stdout or made.stderr).strip().splitlines()[-1]}'


MESSAGE = """build(deps): engineering snapshot v{version}

Moves this product to tools v{version} ({commit}).

Updates the installed archive checksum, independent source manifest pin,
Make action, and supported reusable workflow references together. Product
policy is updated only when explicitly supplied and validated; unrelated action releases are preserved.

Opened by scripts/fanout.py from nicodes/tools.
"""


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('version', help='the release to move the fleet to, without the v')
    parser.add_argument('--fleet', type=Path, required=True)
    parser.add_argument('--policy', type=Path, help='reviewed caller-owned fleet policy; only the release revision may change')
    parser.add_argument('--push', action='store_true', help='push branches and open pull requests')
    parser.add_argument('--checkout-root', type=Path, required=True, help='original repository checkouts, used only to fetch/manage worktrees')
    parser.add_argument('--worktree-root', type=Path, required=True, help='directory for isolated adoption worktrees')
    parser.add_argument('--manifest', type=Path, required=True, help='write machine-readable adoption outcomes')
    parser.add_argument('--only', action='append', default=[], metavar='REPO',
                        help='limit to these products; repeat for several')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        raise SystemExit('version must be X.Y.Z, without the leading v')
    commit, checksum, source_digest = release_facts(args.version)
    fleet = json.loads(args.fleet.read_text())
    repos = args.only or sorted(fleet['products'])
    print(f'v{args.version} = {commit}  sha256:{checksum[:12]}', file=sys.stderr)
    failures = 0
    outcomes = []
    for repo in repos:
        changes = []
        try:
            line = product(repo, args.version, commit, checksum, source_digest, args.push,
                           args.checkout_root, args.worktree_root, args.policy.read_bytes() if args.policy else None, changes)
        except (ValueError, subprocess.SubprocessError) as error:
            line = f'{repo}: failed: {error}'
        print(line)
        outcomes.append({'repository': repo, 'result': line, 'changes': changes,
                         'validation': 'rejected' if 'failed' in line else 'transforms verified; hosted checks pending'})
        if 'failed' in line or ': no ' in line:
            failures += 1
    args.manifest.write_text(json.dumps({'version': args.version, 'revision': commit,
        'archive_sha256': checksum, 'source_sha256': source_digest, 'push': args.push,
        'policy_sha256': hashlib.sha256(args.policy.read_bytes()).hexdigest() if args.policy else None,
        'outcomes': outcomes}, indent=2)+'\n')
    sys.exit(1 if failures else 0)
