#!/usr/bin/env python3
"""Check tracked text source for high-confidence credentials without printing values."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

_spec = importlib.util.spec_from_file_location('_secrets_source_config', Path(__file__).with_name('source_config.py'))
source_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source_config)

PATTERNS = {
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{48,}-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    'live-secret-key': re.compile(rb'\bsk_live_[A-Za-z0-9]{24,}\b'),
}


def scan(root, fixture_policy=None):
    root = Path(root).resolve()
    approved = {}
    if fixture_policy is not None:
        policy = source_config.read(fixture_policy)
        if policy.get('version') != 1 or not isinstance(policy.get('fixtures'), list):
            raise ValueError('invalid credential-fixture policy')
        for entry in policy['fixtures']:
            if set(entry) != {'path', 'sha256', 'reason'} or len(entry['reason'].strip()) < 12:
                raise ValueError('credential fixtures require an exact file hash and explanation')
            if not re.fullmatch('[a-f0-9]{64}', entry['sha256']) or entry['path'] in approved:
                raise ValueError('credential-fixture identities must be exact and unique')
            approved[entry['path']] = entry['sha256']
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).split(b'\0')
    findings, observed = [], set()
    for raw in names:
        if not raw:
            continue
        name = raw.decode('utf-8')
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as source:
            prefix = source.read(8192)
            if b'\0' in prefix:
                continue
            content = prefix + source.read(32 * 1024 * 1024)
            if source.read(1):
                raise ValueError('tracked text exceeds the source scan bound: ' + name)
        if b'\0' in content:
            continue
        if name in approved:
            if hashlib.sha256(content).hexdigest() != approved[name]:
                raise ValueError('credential fixture changed: ' + name)
            observed.add(name)
            continue
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                findings.append({'path': name, 'line': content.count(b'\n', 0, match.start()) + 1,
                                 'category': category,
                                 'fingerprint': hashlib.sha256(match.group()).hexdigest()})
    if set(approved) - observed:
        raise ValueError('credential policy names missing or non-text fixture files')
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path)
    args = parser.parse_args()
    try:
        findings = scan(Path.cwd(), args.fixtures)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps({'version': 1, 'status': 'failed' if findings else 'passed', 'findings': findings}))
    raise SystemExit(bool(findings))


if __name__ == '__main__':
    main()
