#!/usr/bin/env python3
"""Install a byte-pinned Windows Make bootstrap without executing package hooks."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile

URL = 'https://community.chocolatey.org/api/v2/package/make/4.4.1'
PACKAGE_SHA256 = '8b2dec73ed4c84163c4a4f9bfe658553b1e3d1fdfa8930d2aa438e030ff21f9d'
BINARY_SHA256 = 'cc6dc291113dcbcc7735835acbfc23c52ed037e4e124ff2d7a0aeae6df563a9f'
MEMBER = 'tools/install/bin/make.exe'
MAX_PACKAGE = 16 * 1024 * 1024


def binary(data):
    if len(data) > MAX_PACKAGE or hashlib.sha256(data).hexdigest() != PACKAGE_SHA256:
        raise ValueError('Windows Make bootstrap package checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename == MEMBER]
        if len(entries) != 1 or entries[0].is_dir() or entries[0].file_size != 331425:
            raise ValueError('Windows Make package requires its exact executable member')
        with archive.open(entries[0]) as stream:
            result = stream.read(331426)
    if hashlib.sha256(result).hexdigest() != BINARY_SHA256:
        raise ValueError('Windows Make bootstrap executable checksum mismatch')
    return result


def setup():
    if os.name != 'nt':
        raise ValueError('this bootstrap is only for the Windows task runner')
    spec = importlib.util.spec_from_file_location('make_output_protection', Path(__file__).with_name('verification.py'))
    verification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verification)
    directory = verification.protected_directory(Path('.artifacts/bootstrap/make-4.4.1'))
    target = directory/'make.exe'
    if target.exists() or target.is_symlink():
        info = target.lstat()
        verification.owned(target, info)
        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != BINARY_SHA256:
            raise ValueError('existing Windows Make bootstrap differs from the reviewed executable')
    else:
        with urllib.request.urlopen(URL, timeout=30) as response:
            data = response.read(MAX_PACKAGE+1)
        payload = binary(data)
        descriptor, temporary = tempfile.mkstemp(prefix='.make-', dir=directory)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(payload)
            Path(temporary).replace(target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    result = subprocess.check_output([str(target.resolve()), '--version'], text=True, timeout=15)
    if result.splitlines()[0] != 'GNU Make 4.4.1':
        raise ValueError('Windows Make bootstrap version mismatch')
    with open(os.environ['GITHUB_PATH'], 'a') as output:
        output.write(str(directory.resolve())+'\n')
    # A Bash shell is required by the shared Make recipes; the bootstrap Make
    # executable is only a host task runner, independent of Cargo target arch.
    with open(os.environ['GITHUB_ENV'], 'a') as output:
        output.write('SHELL=bash\n')


if __name__ == '__main__':
    try:
        setup()
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
