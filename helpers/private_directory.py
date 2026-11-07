"""Prepare only a missing artifact parent, then enforce the native output policy."""
import argparse
import contextlib
import importlib.util
from pathlib import Path
import subprocess
import sys

SCRIPT = '''import os
from pathlib import Path
import stat
import sys
if os.name == "nt" and sys.version_info < (3, 13):
    raise SystemExit("Windows private SDK directories require the selected Python 3.13 or newer")
parent = Path.cwd() / ".artifacts"
for candidate in [parent, *parent.parents]:
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SystemExit("SDK artifact parent must not traverse links or reparse points")
if parent.exists():
    if not parent.is_dir():
        raise SystemExit("SDK artifact parent must be a directory")
else:
    parent.mkdir(mode=0o700)
'''


def protect(root):
    """Validate an existing parent without creating an output during execution."""
    root = Path(root).resolve(strict=True)
    if not ((root / '.artifacts').exists() or (root / '.artifacts').is_symlink()):
        return
    spec = importlib.util.spec_from_file_location('_private_directory_verification',
                                                Path(__file__).with_name('verification.py'))
    verification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verification)
    # The existing policy deliberately accepts checkout-relative paths only.
    # No native process runs while this temporary policy scope is active.
    with contextlib.chdir(root):
        verification.protected_directory(Path('.artifacts'))


def prepare(root):
    root = Path(root).resolve(strict=True)
    subprocess.run([sys.executable, '-I', '-B', '-c', 'exec(' + repr(SCRIPT) + ')'],
                   cwd=root, check=True, timeout=30)
    protect(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'protect'])
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        (prepare if args.operation == 'prepare' else protect)(args.directory)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
