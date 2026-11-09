"""Bounded native Podman execution with explicit local registry authority."""
import atexit
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
REMOTE_ENVIRONMENT = frozenset({
    'CONTAINER_HOST', 'CONTAINER_CONNECTION', 'CONTAINER_SSHKEY',
    'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH',
    'PODMAN_VARLINK_ADDRESS', 'BUILDKIT_HOST', 'BUILDX_BUILDER',
})
AUTH_COMMANDS = frozenset({'build', 'pull', 'push', 'login', 'logout', 'run', 'create'})
MAX_OUTPUT = 64 << 20
_anonymous = None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('container metadata contains repeated fields')
        result[key] = value
    return result


def config_digest(value):
    if not isinstance(value, str):
        raise ValueError('image configuration identity is missing')
    value = value if value.startswith('sha256:') else 'sha256:' + value
    if not DIGEST.fullmatch(value):
        raise ValueError('image configuration identity is invalid')
    return value


def anonymous_authfile():
    global _anonymous
    if _anonymous is None:
        directory = Path(tempfile.mkdtemp(prefix='tools-registry-auth-'))
        path = directory / 'auth.json'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write('{"auths":{}}\n')
        _anonymous = path
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return _anonymous


def checked_authfile(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('registry authority requires an absolute private authfile')
    for current in [path, *path.parents]:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('registry authority must not traverse links')
    info = path.stat()
    parent = path.parent.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1 << 20
            or parent.st_uid != os.geteuid() or parent.st_mode & 0o077):
        raise ValueError('registry authority requires owned private files and directory')
    data = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {'auths'} or not isinstance(data['auths'], dict):
        raise ValueError('registry authority requires explicit auths without credential helpers')
    return path


def _environment(values=None):
    result = dict(os.environ if values is None else values)
    for name in REMOTE_ENVIRONMENT:
        result.pop(name, None)
    return result


def environment(environment=None):
    """Sanitize local-engine selectors without invoking an engine or reading auth."""
    return _environment(environment)


def bind_mount_user_namespace(uid=None, gid=None):
    """Map the caller's owned bind files to an explicit image account if needed."""
    if uid is None and gid is None:
        return ['--userns=keep-id'] if os.geteuid() != 0 else []
    if any(type(value) is not int or not 0 < value <= 2**31-1 for value in (uid, gid)):
        raise ValueError('bind ownership requires an explicit nonroot UID and GID')
    return [f'--userns=keep-id:uid={uid},gid={gid}'] if os.geteuid() != 0 else []


def command(arguments, environment=None, *, authfile=None):
    """Return native local argv and a sanitized environment; no shell is used."""
    if (not isinstance(arguments, (list, tuple)) or not arguments
            or any(not isinstance(value, str) or '\0' in value for value in arguments)
            or arguments[0].startswith('-')):
        raise ValueError('Podman requires a declared native command and literal arguments')
    values = _environment(environment)
    selected = authfile if authfile is not None else values.get('REGISTRY_AUTH_FILE')
    selected = checked_authfile(selected if selected else anonymous_authfile())
    values['REGISTRY_AUTH_FILE'] = str(selected)
    # An explicit authfile suppresses the containers/image default auth search.
    # Do not permit a caller to add another auth source or a credential in argv.
    args = list(arguments)
    forbidden = {'--authfile', '--creds', '--username', '--password', '--identity-token'}
    if args[0] in AUTH_COMMANDS and any(value.split('=', 1)[0] in forbidden for value in args):
        raise ValueError('registry authority is selected separately from command arguments')
    if args[0] in AUTH_COMMANDS:
        args[1:1] = ['--authfile', str(selected)]
    return ['podman', '--remote=false', *args], values


def run(arguments, *, environment=None, authfile=None, timeout=60, **kwargs):
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ValueError('container execution requires a finite deadline')
    if 'env' in kwargs or 'shell' in kwargs:
        raise ValueError('container environment and native execution are explicit')
    argv, values = command(arguments, environment, authfile=authfile)
    return subprocess.run(argv, env=values, timeout=timeout, **kwargs)


def output(arguments, *, environment=None, authfile=None, timeout=60, **kwargs):
    """Capture finite metadata without admitting unbounded pipe output."""
    if any(name in kwargs for name in ['stdout', 'capture_output', 'text', 'encoding']):
        raise ValueError('container metadata owns its bounded output capture')
    with tempfile.TemporaryFile() as stream:
        run(arguments, environment=environment, authfile=authfile, timeout=timeout,
            stdout=stream, check=True, **kwargs)
        if stream.tell() > MAX_OUTPUT:
            raise ValueError('container metadata exceeds its byte limit')
        stream.seek(0)
        return stream.read(MAX_OUTPUT + 1).decode()


def inspect_images(references, *, environment=None, authfile=None, timeout=60):
    if (not isinstance(references, (list, tuple)) or not 1 <= len(references) <= 64
            or any(not isinstance(ref, str) or not ref or ref.startswith('-')
                   or any(char.isspace() or ord(char) < 32 for char in ref) for ref in references)):
        raise ValueError('image inspection requires explicit bounded references')
    values = json.loads(output(['image', 'inspect', *references], environment=environment,
                               authfile=authfile, timeout=timeout), object_pairs_hook=unique_object)
    if not isinstance(values, list) or len(values) != len(references):
        raise ValueError('Podman did not return every selected image')
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('image metadata must be an object')
        value['Id'] = config_digest(value.get('Id'))
        rootfs = value.get('RootFS')
        if (not isinstance(rootfs, dict) or rootfs.get('Type') != 'layers'
                or not isinstance(rootfs.get('Layers'), list) or len(rootfs['Layers']) > 128
                or any(not isinstance(layer, str) or not DIGEST.fullmatch(layer) for layer in rootfs['Layers'])
                or value.get('Os') != 'linux' or not isinstance(value.get('Architecture'), str)
                or not value['Architecture'] or not isinstance(value.get('Config'), dict)):
            raise ValueError('image metadata lacks its immutable rootfs, configuration or platform')
        for field in ['RepoTags', 'RepoDigests']:
            if value.get(field) is None:
                value[field] = []
            if not isinstance(value[field], list) or any(not isinstance(ref, str) for ref in value[field]):
                raise ValueError('image reference metadata is malformed')
        labels = value['Config'].get('Labels')
        if labels is None:
            value['Config']['Labels'] = {}
        elif not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
            raise ValueError('image labels are malformed')
    return values
