#!/usr/bin/env python3
"""Materialize exact native runtime filenames from caller-owned tracked inputs."""
import argparse
import json
import os
from pathlib import Path


def inside(root, name):
    if not isinstance(name, str) or not name or '\\' in name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('runtime input paths must stay inside the checkout')
    path = root / name
    if not path.parent.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
        raise ValueError('runtime input parents may not escape through links')
    return path


def prepare(root, manifest):
    root = Path(root).resolve()
    declaration = inside(root, manifest)
    if declaration.is_symlink() or declaration.stat().st_size > 262144:
        raise ValueError('runtime input manifest must be a bounded regular source file')
    rows = json.loads(declaration.read_text())
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('runtime input records must be bounded source data')
    plan = []
    targets = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'source', 'target', 'symlink'}:
            raise ValueError('runtime input record must name source and target')
        source = inside(root, row.get('source'))
        target = inside(root, row.get('target'))
        if target in targets or source.is_symlink() or not source.is_file():
            raise ValueError('runtime sources must be regular and targets unique')
        targets.add(target)
        raw = source.read_bytes()
        expected = row.get('symlink')
        if expected is not None and (not isinstance(expected, str) or not (target.parent / expected).resolve().is_relative_to(root)):
            raise ValueError('runtime symlink must remain inside the checkout')
        if target.is_symlink() or target.exists():
            if expected is not None and target.is_symlink() and os.readlink(target) == expected:
                continue
            if expected is None and not target.is_symlink() and target.is_file() and target.read_bytes() == raw:
                continue
            raise ValueError('refusing to replace modified runtime input: ' + row['target'])
        plan.append((target, raw, expected))
    for target, raw, expected in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        if expected is None:
            # Exclusive creation preserves edits made after the validation pass.
            with target.open('xb') as stream:
                stream.write(raw)
        else:
            target.symlink_to(expected)
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    try:
        print('Prepared', prepare(args.root, args.manifest), 'runtime inputs.')
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
