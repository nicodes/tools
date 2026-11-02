#!/usr/bin/env python3
"""Remove explicitly declared generated paths inside the current Git checkout."""
import argparse
from pathlib import Path
import shutil
import subprocess


def planned(root, names):
    root = Path(root).resolve()
    top = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], text=True).strip()
    if Path(top).resolve() != root:
        raise ValueError('clean must run at the checkout root')
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    result = []
    for name in names:
        path = Path(name)
        if (not name or path.is_absolute() or '..' in path.parts or not path.parts
                or any(part == '.git' for part in path.parts) or path == Path('.artifacts')):
            raise ValueError('clean paths must name particular generated outputs')
        relative = path.as_posix()
        if any(p == relative or p.startswith(relative + '/') for p in tracked if p):
            raise ValueError('clean refuses tracked source: ' + relative)
        target = root / path
        if any(p.is_symlink() for p in [target, *target.parents] if p != root):
            raise ValueError('clean refuses linked output: ' + relative)
        if target.exists() and not (target.is_file() or target.is_dir()):
            raise ValueError('clean refuses non-regular output: ' + relative)
        result.append(target)
    return result


def clean(root, names):
    targets = planned(root, names)
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', action='append', required=True, help='checkout-relative generated output; repeat as needed')
    args = parser.parse_args()
    try:
        clean(Path.cwd(), args.path)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
