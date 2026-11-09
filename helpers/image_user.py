"""Read an immutable image's account table without executing its contents."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time


def runtime():
    spec = importlib.util.spec_from_file_location('image_user_runtime', Path(__file__).with_name('container_runtime.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def passwd_identity(raw, username='postgres'):
    if (not isinstance(username, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username)
            or not isinstance(raw, bytes) or not 1 <= len(raw) <= 65536 or b'\0' in raw):
        raise ValueError('image account table is invalid')
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeError:
        raise ValueError('image account table is invalid') from None
    matches = []
    if len(lines) > 1024:
        raise ValueError('image account table exceeds its row limit')
    for line in lines:
        if not line or line.startswith('#'):
            continue
        fields = line.split(':')
        if len(fields) != 7 or any(ord(char) < 32 or ord(char) == 127 for char in line):
            raise ValueError('image account table contains malformed rows')
        if fields[0] == username:
            if any(not re.fullmatch(r'[1-9][0-9]{0,9}', value) or int(value) > 2**31-1 for value in fields[2:4]):
                raise ValueError('image account requires a nonroot numeric UID and GID')
            matches.append(tuple(int(value) for value in fields[2:4]))
    if len(matches) != 1:
        raise ValueError('image account is missing or repeated')
    return matches[0]


def archive_identity(raw, username):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 131072:
        raise ValueError('image account archive exceeds its bound')
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            member = archive.next()
            if (member is None or member.name not in {'passwd', 'etc/passwd'} or not member.isfile()
                    or member.issparse() or member.pax_headers or not 1 <= member.size <= 65536):
                raise ValueError('image account archive is not one ordinary account file')
            stream = archive.extractfile(member)
            content = stream.read(65537)
            if len(content) != member.size or archive.next() is not None:
                raise ValueError('image account archive has unexpected members')
            return passwd_identity(content, username)
    except (tarfile.TarError, OSError, EOFError):
        raise ValueError('image account archive is invalid') from None


def capture(native, args, environment, deadline, limit=1 << 20):
    argv, values = native.command(args, environment, authfile=native.anonymous_authfile())
    process = subprocess.Popen(argv, env=values, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    chunks = []
    size = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('image account inspection exceeded its deadline')
                for key, _ in selector.select(min(remaining, .25)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit-size+1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        size += len(chunk)
                        if size > limit:
                            raise ValueError('image account inspection exceeded its output bound')
                        chunks.append(chunk)
            code = process.wait(timeout=max(.001, deadline-time.monotonic()))
        return code, b''.join(chunks)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        process.stdout.close()


def intent(directory, record):
    descriptor, temporary = tempfile.mkstemp(prefix='.pending-', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(json.dumps(record, sort_keys=True, separators=(',', ':')).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory/'intent.json')
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def owned(native, name, token, config, environment, deadline):
    code, _ = capture(native, ['container', 'exists', name], environment, deadline)
    if code == 1:
        return None
    if code != 0:
        raise RuntimeError('cannot establish image account inspection ownership')
    code, raw = capture(native, ['container', 'inspect', name], environment, deadline)
    values = json.loads(raw, object_pairs_hook=native.unique_object) if code == 0 else None
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise RuntimeError('cannot establish image account inspection ownership')
    value = values[0]
    state = value.get('State', {})
    settings = value.get('Config', {})
    if (not re.fullmatch(r'[a-f0-9]{64}', value.get('Id', '')) or value.get('Name', '').lstrip('/') != name
            or native.config_digest(value.get('Image')) != config
            or settings.get('Labels', {}).get('tools.image-user') != token
            or settings.get('User') != '65534:65534' or settings.get('Entrypoint') not in ['/bin/false', ['/bin/false']]
            or state.get('Running') is not False or state.get('Pid') != 0
            or state.get('Status') not in {'configured', 'created'} or value.get('Mounts') != []):
        raise RuntimeError('image account inspection container identity changed')
    return value['Id']


def identity(config_id, username='postgres', *, environment=None, timeout=60):
    """Return the exact nonroot account UID/GID from a local config digest.

    The private intent survives uncertain cleanup. Only this inspection's fresh
    token-owned, never-started container can be removed; images remain untouched.
    """
    native = runtime()
    if (not isinstance(config_id, str) or not native.DIGEST.fullmatch(config_id)
            or type(timeout) not in {int, float} or not 0 < timeout <= 300
            or not isinstance(username, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username)):
        raise ValueError('image account inspection requires an immutable config and finite deadline')
    deadline = time.monotonic()+timeout
    token = secrets.token_hex(16)
    name = 'tools-image-user-'+token
    directory = Path(tempfile.mkdtemp(prefix='tools-image-user-'))
    record = {'version': 1, 'name': name, 'token': token, 'config_digest': config_id, 'container_id': ''}
    intent(directory, record)
    try:
        code, raw = capture(native, ['create', '--pull=never', '--name', name, '--label', 'tools.image-user='+token,
            '--network=none', '--read-only', '--read-only-tmpfs=false', '--image-volume=ignore', '--http-proxy=false',
            '--user', '65534:65534', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
            '--memory=32m', '--memory-swap=32m', '--cpus=.25', '--pids-limit=16', '--entrypoint=/bin/false', config_id],
            environment, deadline)
        if code != 0 or not re.fullmatch(rb'[a-f0-9]{64}\s*', raw):
            raise RuntimeError('image account inspection container creation failed')
        container_id = owned(native, name, token, config_id, environment, deadline)
        if container_id != raw.decode().strip():
            raise RuntimeError('image account inspection acknowledgment changed')
        record['container_id'] = container_id
        intent(directory, record)
        code, raw = capture(native, ['cp', container_id+':/etc/passwd', '-'], environment, deadline, 131072)
        if code != 0:
            raise RuntimeError('image account table could not be read')
        result = archive_identity(raw, username)
        if owned(native, name, token, config_id, environment, deadline) != container_id:
            raise RuntimeError('image account inspection container disappeared')
        return result
    finally:
        cleanup_deadline = time.monotonic()+15
        container_id = owned(native, name, token, config_id, environment, cleanup_deadline)
        if container_id is not None:
            if record['container_id'] and container_id != record['container_id']:
                raise RuntimeError('image account cleanup acknowledgment changed')
            code, _ = capture(native, ['rm', container_id], environment, cleanup_deadline)
            if code != 0 or owned(native, name, token, config_id, environment, cleanup_deadline) is not None:
                raise RuntimeError('image account inspection cleanup remains uncertain')
        shutil.rmtree(directory)
