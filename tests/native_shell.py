"""Select the native Bash used by fixture subprocesses, without WSL discovery."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def bash(environment=None, *, system=None, lookup=shutil.which):
    environment = os.environ if environment is None else environment
    windows = os.name == 'nt' if system is None else system == 'Windows'
    if not windows:
        value = lookup('bash', path=environment.get('PATH'))
        if value is None:
            raise ValueError('native Bash is required for shell controls')
        return str(Path(value).resolve())
    value = lookup('git', path=environment.get('PATH'))
    if value is None:
        raise ValueError('native Git for Windows is required for shell controls')
    git = Path(value).resolve()
    # Git for Windows exposes git.exe from cmd/, bin/, or mingw*/bin/.
    # An explicit executable avoids CreateProcess's earlier System32 bash.exe.
    for parent in list(git.parents)[:3]:
        candidate = parent/'bin/bash.exe'
        if (parent/'cmd/git.exe').is_file() and candidate.is_file():
            return str(candidate.resolve())
    raise ValueError('the selected native Git installation has no Git Bash executable')


def process_image(process):
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    query = ctypes.WinDLL('kernel32', use_last_error=True).QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                      ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not query(wintypes.HANDLE(int(process._handle)), 0, buffer, ctypes.byref(size)):
        return {'query_error': ctypes.get_last_error()}
    return buffer.value


def probe(executable, environment=None):
    """Bound one finite Bash builtin probe; preserve native stdout/stderr bytes."""
    command = [executable, '-euc', 'printf "BASH_PROBE:%s|%s\\n" "$BASH_VERSION" "$OSTYPE"']
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr) as child:
            image = process_image(child)
            try:
                code = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                raise ValueError('native shell diagnostic exceeded its five-second bound')
        result = {'executable_argument': executable, 'process_image': image, 'exit': code}
        for name, stream in [('stdout', stdout), ('stderr', stderr)]:
            stream.seek(0)
            raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError('native shell diagnostic exceeded its output bound')
            result[name+'_base64'] = base64.b64encode(raw).decode()
            result[name] = raw.decode('utf-8', errors='replace')
        return result
