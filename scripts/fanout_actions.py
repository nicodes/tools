#!/usr/bin/env python3
"""Review coherent immutable deployment-action adoption; never merge a PR."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess

REFERENCE = re.compile(r'(nicodes/komizo-actions/[\w-]+)@([^\s\"\']+)')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def changes(root, version, revision):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('release requires X.Y.Z and an immutable commit')
    pins_path = root/'ACTION-PINS.json'
    pins = json.loads(pins_path.read_text())
    if pins['repository'] != 'https://github.com/nicodes/komizo-actions' or not pins['pins']:
        raise ValueError('unsupported action manifest')
    old = {(pin['tag'], pin['sha']) for pin in pins['pins'].values()}
    if len(old) != 1:
        raise ValueError('mixed action releases require explicit review')
    old_tag, old_revision = old.pop()
    if not re.fullmatch(r'v\d+\.\d+\.\d+', old_tag) or not re.fullmatch(r'[a-f0-9]{40}', old_revision):
        raise ValueError('moving or malformed recorded action pin')
    policy_path = root/'fleet-policy.json'
    policy = json.loads(policy_path.read_text())
    for key in ('action_tag', 'komizo_actions_tag'):
        if policy['baseline'][key] != old_tag:
            raise ValueError('action policy and manifest disagree')
    result = {}
    def record(path, value):
        before = path.read_bytes()
        after = value.encode()
        if before != after:
            result[str(path.relative_to(root))] = (before, after)
    for path in sorted((root/'.github').rglob('*.y*ml')):
        text = path.read_text()
        for action, ref in REFERENCE.findall(text):
            name = action.rsplit('/', 1)[1]
            if name not in pins['pins'] or ref != old_revision:
                raise ValueError(f'{path.relative_to(root)}: unrecorded or incoherent action reference')
        text = REFERENCE.sub(lambda match: match[1]+'@'+revision, text)
        record(path, text.replace('# '+old_tag, '# v'+version))
    for pin in pins['pins'].values():
        pin.update(tag='v'+version, sha=revision)
    record(pins_path, json.dumps(pins, indent=2)+'\n')
    # Preserve unrelated policy bytes and its independently checked digest.
    policy_bytes = policy_path.read_bytes()
    policy_text = policy_bytes.decode()
    for key in ('action_tag', 'komizo_actions_tag'):
        pattern = r'("'+key+r'"\s*:\s*")'+re.escape(old_tag)+r'(")'
        policy_text, count = re.subn(pattern, lambda m: m[1]+'v'+version+m[2], policy_text)
        if count != 1:
            raise ValueError('unsupported policy layout')
    record(policy_path, policy_text)
    environment = root/'scripts/engineering-env.sh'
    text = environment.read_text()
    before_digest = digest(policy_bytes)
    if text.count(before_digest) != 1:
        raise ValueError('caller policy checksum differs from actual bytes')
    record(environment, text.replace(before_digest, digest(policy_text.encode())))
    # These game fixtures deliberately assert a reviewed release, rather than
    # deriving expected values from the manifest they are supposed to verify.
    fixture = root/'tools/test_supply_chain.py'
    if fixture.exists():
        record(fixture, fixture.read_text().replace(old_revision, revision).replace(old_tag, 'v'+version))
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    for relative in filter(None, tracked):
        path = root/relative
        if not path.is_file() or path.is_symlink():
            continue
        try:
            after = result.get(relative, (None, path.read_bytes()))[1].decode()
        except UnicodeDecodeError:
            continue
        if old_revision != revision and old_revision in after:
            raise ValueError(f'{relative}: unhandled old release reference; review before adoption')
    return result


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def adopt(repo, args, revision):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('invalid repository')
    original = args.checkout_root/repo
    root = args.worktree_root/('actions-'+args.version+'-'+repo.replace('/', '-'))
    branch = 'deps/komizo-actions-'+args.version
    git(original, 'fetch', 'origin', 'main')
    if not root.exists():
        exists = subprocess.run(['git', '-C', str(original), 'show-ref', '--verify', 'refs/heads/'+branch], capture_output=True).returncode == 0
        git(original, 'worktree', 'add', str(root), *([branch] if exists else ['-b', branch, 'FETCH_HEAD']))
    if git(root, 'branch', '--show-current') != branch or git(root, 'status', '--porcelain'):
        raise ValueError('preserve conflicting branch or dirty worktree; resolve before resuming')
    edits = changes(root, args.version, revision)
    manifest = []
    for relative, (before, after) in edits.items():
        print(''.join(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True), fromfile=relative, tofile=relative)), end='')
        manifest.append({'path': relative, 'before_sha256': digest(before), 'after_sha256': digest(after)})
    if args.push:
        for relative, (_, after) in edits.items():
            (root/relative).write_bytes(after)
        if edits:
            git(root, 'add', '--', *edits)
            git(root, 'commit', '-m', 'Adopt verified komizo actions v'+args.version)
        if edits or git(root, 'rev-parse', 'HEAD') != git(original, 'rev-parse', 'FETCH_HEAD'):
            git(root, 'push', '-u', 'origin', branch)
            existing = subprocess.check_output(['gh', 'pr', 'list', '--repo', repo, '--head', branch, '--json', 'url'], text=True)
            if not json.loads(existing):
                subprocess.run(['gh', 'pr', 'create', '--repo', repo, '--head', branch, '--title', 'Adopt verified deployment actions v'+args.version,
                                '--body', 'Adopt immutable action references, reviewed manifest, policy digest and explicit supply-chain fixtures coherently. Full exact-head CI and applicable preview gates must pass before merge; verify merged CD and live artifacts afterward.'], check=True)
    return {'repository': repo, 'base_commit': git(original, 'rev-parse', 'FETCH_HEAD'), 'head_commit': git(root, 'rev-parse', 'HEAD'),
            'changes': manifest, 'validation': 'coherent transform verified; hosted product gates required', 'pushed': args.push}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--repository', action='append', required=True)
    parser.add_argument('--checkout-root', type=Path, required=True)
    parser.add_argument('--worktree-root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()
    if '.worktrees' not in args.worktree_root.resolve().parts:
        parser.error('adoption worktrees must be under .worktrees')
    remote = subprocess.check_output(['git', 'ls-remote', 'https://github.com/nicodes/komizo-actions.git',
                                      'refs/tags/v'+args.version, 'refs/tags/v'+args.version+'^{}'], text=True)
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    resolved = refs.get('refs/tags/v'+args.version+'^{}', refs.get('refs/tags/v'+args.version))
    if resolved != args.revision:
        parser.error('published tag does not resolve to the reviewed immutable revision')
    outcomes = []
    for repo in args.repository:
        try:
            outcomes.append(adopt(repo, args, resolved))
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            outcomes.append({'repository': repo, 'error': str(error)})
    args.manifest.write_text(json.dumps({'version': args.version, 'revision': resolved, 'outcomes': outcomes}, indent=2)+'\n')
    raise SystemExit(int(any('error' in outcome for outcome in outcomes)))


if __name__ == '__main__':
    main()
