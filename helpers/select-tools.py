#!/usr/bin/env python3
"""Validate a subset of tool identifiers without overriding caller-owned pins."""
import argparse
from pathlib import Path
import re
import tomllib


def select(directory, value):
    root = Path(directory)
    path = next((root / name for name in ['mise.toml', '.mise.toml'] if (root / name).is_file()), None)
    if path is None:
        raise ValueError('tool selection requires caller-owned Mise pins')
    tools = tomllib.loads(path.read_text()).get('tools', {})
    names = value.split()
    if not names:
        raise ValueError('select at least one declared tool')
    for name in names:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_:./-]*', name) or name not in tools:
            raise ValueError('tool selection must name declared identifiers without version overrides')
    if len(set(names)) != len(names):
        raise ValueError('tool selection contains duplicate identifiers')
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='.')
    parser.add_argument('--tools', required=True)
    args = parser.parse_args()
    try:
        print(' '.join(select(args.directory, args.tools)))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
