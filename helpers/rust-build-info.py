#!/usr/bin/env python3
"""Verify a native runtime's embedded source, lockfile and routing contract."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib


def verify(binary, source, lock, contract, *, root=None, runtime='rust-axum', require_static=True):
    root = Path(root or Path.cwd()).resolve()
    if not re.fullmatch(r'[a-f0-9]{40}', source):
        raise ValueError('full source revision required')
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    rust = pins.get('rust')
    rust = rust.get('version') if isinstance(rust, dict) else rust
    if not isinstance(rust, str) or not re.fullmatch(r'\d+\.\d+\.\d+', rust):
        raise ValueError('native runtime verification requires exact caller Rust SDK pins')
    paths = []
    for value in [lock, contract]:
        path = Path(value)
        target = root / path
        if (path.is_absolute() or '..' in path.parts or not target.is_file()
                or not target.resolve().is_relative_to(root) or any(p.is_symlink() for p in [target, *target.parents] if p != root)):
            raise ValueError('native runtime contracts must be regular caller-owned source')
        paths.append(target)
    binary = Path(binary).resolve(strict=True)
    receipt = json.loads(subprocess.check_output([str(binary), '--build-info'], text=True, timeout=30))
    expected = {'runtime': runtime, 'source_commit': source,
                'lock_sha256': hashlib.sha256(paths[0].read_bytes()).hexdigest(),
                'routes_sha256': hashlib.sha256(paths[1].read_bytes()).hexdigest()}
    if not isinstance(receipt, dict) or set(receipt) != set(expected) | {'rustc'} or any(receipt[key] != value for key, value in expected.items()):
        raise ValueError('shipped runtime differs from its source, lock or routing contract')
    if not isinstance(receipt['rustc'], str) or not receipt['rustc'].startswith('rustc ' + rust + ' '):
        raise ValueError('shipped runtime differs from the caller Rust compiler pin')
    if require_static:
        program = subprocess.check_output(['readelf', '-lW', str(binary)], text=True, timeout=30)
        dynamic = subprocess.check_output(['readelf', '-dW', str(binary)], text=True, timeout=30)
        if re.search(r'\bINTERP\b', program) or '(NEEDED)' in dynamic:
            raise ValueError('scratch runtime must be statically linked')
    return {**receipt, 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--source', required=True)
    parser.add_argument('--lock', required=True)
    parser.add_argument('--contract', required=True)
    parser.add_argument('--runtime', default='rust-axum')
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.binary, args.source, args.lock, args.contract, runtime=args.runtime)))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
