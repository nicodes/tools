"""Image build and verification primitives shared by application recipes."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

from configuration import relative

TOOLS = Path(__file__).resolve().parents[1]
NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')


def helper(name):
    spec = importlib.util.spec_from_file_location('image_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def definition(root, config):
    path = relative(root, config.get('artifacts', {}).get('definition', 'deploy/release.json'), exists=True)
    return helper('verification').release_definition(path)


def components(value):
    base = value['image_base']
    result = []
    for image in value['inputs']['images']:
        prefix, suffix = base + '-', ':{revision}'
        if not image.startswith(prefix) or not image.endswith(suffix):
            raise ValueError('application images must use the exact release image base and source revision')
        name = image[len(prefix):-len(suffix)]
        if not NAME.fullmatch(name) or name in result:
            raise ValueError('release image component names must be distinct native identifiers')
        result.append(name)
    if not result:
        raise ValueError('image application requires declared release images')
    return result


def references(value, revision):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('image builds require an exact source revision')
    return [value['image_base'] + '-' + name + ':' + revision for name in components(value)]


def validate_compose(root, config, environment, run):
    settings = config.get('components', {}).get('compose', {})
    source = relative(root, settings.get('path', 'deploy/compose.yml'), exists=True)
    placeholders = settings.get('environment_files', [])
    if not isinstance(placeholders, list) or any(not isinstance(name, str) or Path(name).name != name for name in placeholders):
        raise ValueError('Compose validation placeholders must be flat native environment filenames')
    if placeholders:
        with tempfile.TemporaryDirectory(prefix='compose-validation-') as directory:
            target = Path(directory)
            shutil.copy2(source, target / 'compose.yml')
            for name in placeholders:
                if not re.fullmatch('[a-zA-Z0-9_.-]+', name) or name in {'.', '..', 'compose.yml'}:
                    raise ValueError('invalid Compose placeholder filename')
                (target / name).touch(mode=0o600)
            (target / 'interpolation.env').touch(mode=0o600)
            run(['docker', 'compose', '--env-file', str(target / 'interpolation.env'),
                 '--project-directory', str(target), '-p', 'engineering-validation',
                 '-f', str(target / 'compose.yml'), 'config', '--quiet'], root, environment)
    else:
        run(['docker', 'compose', '-f', str(source), 'config', '--no-env-resolution', '--quiet'], root, environment)


def stage_caddy(root, *, host=False):
    directory = relative(root, '.artifacts/caddy-build')
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    names = {'caddy.go.mod': 'go.mod', 'caddy.go.sum': 'go.sum', 'caddy-build.sh': 'build.sh'} if host else {
        name: name for name in ['caddy.go.mod', 'caddy.go.sum', 'caddy-build.sh']}
    for name, destination in names.items():
        shutil.copy2(TOOLS / 'helpers' / name, directory / destination)
    return directory


def host_caddy(root, environment, run):
    expected = tomllib.loads((root / 'mise.toml').read_text())['tools']['go']
    actual = subprocess.check_output(['go', 'env', 'GOVERSION'], cwd=root, env=environment, text=True).strip()
    if actual != 'go' + expected:
        raise ValueError('Caddy requires the exact caller-pinned Go SDK')
    platform = environment.get('DOCKER_DEFAULT_PLATFORM') or subprocess.check_output(
        ['docker', 'version', '--format', '{{.Server.Os}}/{{.Server.Arch}}'],
        cwd=root, env=environment, text=True, timeout=30).strip()
    if platform not in {'linux/amd64', 'linux/arm64'}:
        raise ValueError('unsupported Caddy runtime platform')
    directory = stage_caddy(root, host=True)
    values = environment | {'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': platform.split('/')[1],
                            'GOTOOLCHAIN': 'local', 'GOFLAGS': '-buildvcs=false'}
    run(['sh', 'build.sh'], directory, values)
    # The previous compiler-image cache is obsolete for host-compiled Caddy.
    # Use the same component lock as docker-build.py and retain every other cache.
    if environment.get('FLEET_BUILDKIT_CACHE'):
        import fcntl
        cache_root = Path(environment['FLEET_BUILDKIT_CACHE'])
        cache, lock = cache_root / 'gate-caddy', cache_root / 'gate-caddy.lock'
        if any(path.is_symlink() for path in [cache_root, cache, lock]):
            raise ValueError('compiler layer cache must not be a symlink')
        if cache.exists():
            with lock.open('a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                if cache.exists():
                    shutil.rmtree(cache)


def build(root, config, environment, run, *, native=None):
    value = definition(root, config)
    revision = environment['APP_VERSION']
    references(value, revision)
    records = config.get('components', {}).get('images', [])
    if not isinstance(records, list) or not records:
        raise ValueError('image builds require typed Dockerfile component records')
    built, plans = [], []
    for record in records:
        if set(record) - {'name', 'dockerfile', 'context', 'target', 'cache_key', 'pull', 'build_arguments', 'native'}:
            raise ValueError('unknown image component setting')
        name = record.get('name')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('invalid image component name')
        if name in built:
            raise ValueError('duplicate image build component')
        if record.get('native') and native is None:
            raise ValueError('native image preparation has no owning recipe runner')
        if 'pull' in record and type(record['pull']) is not bool:
            raise ValueError('image pull policy must be boolean')
        dockerfile = relative(root, record.get('dockerfile', 'deploy/images/' + name + '.Dockerfile'), exists=True)
        context = relative(root, record.get('context', '.'), exists=True)
        command = ['docker', 'buildx', 'build', '--load']
        if record.get('pull', True):
            command.append('--pull')
        target = record.get('target')
        if target:
            if not isinstance(target, str) or not NAME.fullmatch(target):
                raise ValueError('invalid native Dockerfile target')
            command += ['--target', target]
        arguments = record.get('build_arguments', {})
        if not isinstance(arguments, dict):
            raise ValueError('image build arguments must be a literal table')
        for key, argument in arguments.items():
            if not re.fullmatch('[A-Z][A-Z0-9_]*', key) or not isinstance(argument, str):
                raise ValueError('image build arguments must be literal named values')
            command += ['--build-arg', key + '=' + argument.replace('{revision}', revision)]
        command += ['-f', str(dockerfile), '-t', value['image_base'] + '-' + name + ':' + revision, str(context)]
        cache = record.get('cache_key')
        if cache:
            if not isinstance(cache, str) or not NAME.fullmatch(cache):
                raise ValueError('image cache key must be a native component identifier')
            command = [sys.executable, str(TOOLS / 'helpers/docker-build.py'), '--cache-key', cache, '--', *command[4:]]
        plans.append((record, command))
        built.append(name)
    if not set(components(value)) <= set(built):
        raise ValueError('not every declared shipping image has a build component')
    for record, command in plans:
        if record.get('native'):
            native(root, record['native'], environment)
        run(command, root, environment)


def package(root, config, environment, run):
    value = definition(root, config)
    refs = references(value, environment['APP_VERSION'])
    directory = relative(root, '.artifacts/release')
    directory.mkdir(parents=True, exist_ok=True)
    inspected = subprocess.check_output(['docker', 'image', 'inspect', *refs], cwd=root, env=environment, timeout=60)
    (directory / 'images.json').write_bytes(inspected)
    archive = directory / 'images.tar.gz'
    with tempfile.TemporaryFile() as errors, archive.open('wb') as output:
        process = subprocess.Popen(['docker', 'save', *refs], cwd=root, env=environment,
                                   stdout=subprocess.PIPE, stderr=errors)
        try:
            with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=1, mtime=0) as compressor:
                shutil.copyfileobj(process.stdout, compressor)
        except BaseException:
            process.kill()
            process.wait()
            archive.unlink(missing_ok=True)
            raise
        finally:
            process.stdout.close()
        if process.wait() != 0:
            archive.unlink(missing_ok=True)
            raise ValueError('Docker could not save the complete image set')
    with archive.open('rb') as incoming:
        digest = hashlib.file_digest(incoming, 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  images.tar.gz\n')
    command = [sys.executable, str(TOOLS / 'helpers/release.py'), 'record',
               '--project', value['project'], '--image-base', value['image_base'],
               '--revision', environment['APP_VERSION'], '--components', *components(value)]
    if value.get('stateful_contract'):
        command += ['--stateful-contract', value['stateful_contract']]
    run(command, root, environment)


def validate_release(root, config, environment):
    value = definition(root, config)
    release = helper('release')
    manifest = json.loads((root / '.artifacts/release/release.json').read_text())
    refs = release.validate(manifest, value['project'], environment['APP_VERSION'], components(value),
                            root / '.artifacts/release/images.tar.gz', value['image_base'])
    if release.inspect(refs) != manifest['images']:
        raise ValueError('local image identities changed after release packaging')


def static_image(image, source, installed='/srv/public'):
    def manifest(directory):
        values = {}
        for path in Path(directory).rglob('*'):
            if path.is_symlink():
                raise ValueError('static image inputs and installed bytes must not contain links')
            if path.is_file():
                with path.open('rb') as incoming:
                    values[path.relative_to(directory).as_posix()] = hashlib.file_digest(incoming, 'sha256').hexdigest()
        return values
    expected = manifest(source)
    if not expected:
        raise ValueError('static image source must contain actual output files')
    container = subprocess.check_output(['docker', 'create', image], text=True, timeout=30).strip()
    if not re.fullmatch('[a-f0-9]{64}', container):
        raise ValueError('an immutable temporary container identity is required')
    try:
        with tempfile.TemporaryDirectory(prefix='static-image-') as directory:
            subprocess.run(['docker', 'cp', container + ':' + installed + '/.', directory], check=True, timeout=60)
            actual = manifest(Path(directory))
            if expected != actual:
                raise ValueError('static image does not preserve its complete source bytes')
    finally:
        subprocess.run(['docker', 'rm', '-v', container], check=True, stdout=subprocess.DEVNULL, timeout=30)
    return expected


def rust_inventory(root, manifests, environment, run):
    if not manifests or len(set(manifests)) != len(manifests):
        raise ValueError('Rust inventory scanning requires declared Cargo manifests')
    directory = relative(root, '.artifacts/security/rust-inputs')
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for index, manifest in enumerate(manifests):
        source = relative(root, str(Path(manifest).with_name('Cargo.lock')), exists=True)
        target = directory / str(index)
        target.mkdir()
        shutil.copy2(source, target / 'Cargo.lock')
    report = root / '.artifacts/security/rust-source.json'
    run(['trivy', 'fs', '--quiet', '--scanners', 'vuln', '--pkg-types', 'library',
         '--include-dev-deps', '--list-all-pkgs', '--format', 'json', '--output', str(report), str(directory)],
        root, environment)
    value = json.loads(report.read_text())
    if value.get('SchemaVersion') != 2 or not isinstance(value.get('Results'), list):
        raise ValueError('unsupported Rust dependency report')
    inventories = [item for item in value['Results'] if item.get('Type') == 'cargo' and item.get('Packages')]
    targets = {Path(item.get('Target', '')).parts[-2:] for item in inventories}
    if len(inventories) != len(manifests) or targets != {(str(i), 'Cargo.lock') for i in range(len(manifests))}:
        raise ValueError('not every declared Rust dependency inventory was scanned')
    blocked = [v for item in inventories for v in item.get('Vulnerabilities', [])
               if v.get('Severity') in {'HIGH', 'CRITICAL'}]
    if blocked:
        raise ValueError('Rust dependency inventory has High/Critical findings; full report retained')


def rust_image(root, image, contract):
    if set(contract) - {'user', 'lock_source', 'lock_installed', 'binaries', 'forbidden_markers'}:
        raise ValueError('unknown native Rust image contract setting')
    inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True, timeout=30))
    if len(inspected) != 1 or not re.fullmatch('sha256:[a-f0-9]{64}', inspected[0].get('Id', '')):
        raise ValueError('Rust runtime requires an immutable image identity')
    identity = inspected[0]['Id']
    if inspected[0]['Config']['User'] != contract.get('user', '10001:10001'):
        raise ValueError('Rust runtime user differs from the declared contract')
    container = subprocess.check_output(['docker', 'create', identity], text=True, timeout=30).strip()
    if not re.fullmatch('[a-f0-9]{64}', container):
        raise ValueError('Rust inspection container must have an immutable identity')
    try:
        with tempfile.TemporaryDirectory(prefix='rust-image-') as directory:
            directory = Path(directory)
            source = relative(root, contract.get('lock_source', 'api/Cargo.lock'), exists=True)
            installed = contract.get('lock_installed')
            if not isinstance(installed, str) or not installed.startswith('/') or '..' in Path(installed).parts:
                raise ValueError('Rust runtime inventory needs an absolute installed lock path')
            lock = directory / 'Cargo.lock'
            subprocess.run(['docker', 'cp', container + ':' + installed, str(lock)], check=True, timeout=30)
            if source.read_bytes() != lock.read_bytes():
                raise ValueError('runtime dependency inventory differs from the source lockfile')
            entries = contract.get('binaries', [])
            if not entries:
                raise ValueError('Rust image contracts require actual shipped binaries')
            for index, entry in enumerate(entries):
                if set(entry) != {'source', 'installed'}:
                    raise ValueError('Rust image binaries require source and installed paths')
                original = relative(root, entry['source'], exists=True)
                installed = entry['installed']
                if not isinstance(installed, str) or not installed.startswith('/') or '..' in Path(installed).parts:
                    raise ValueError('Rust runtime binaries need absolute installed paths')
                destination = directory / ('binary-' + str(index))
                subprocess.run(['docker', 'cp', container + ':' + installed, str(destination)], check=True, timeout=30)
                data = destination.read_bytes()
                if data != original.read_bytes() or not data.startswith(b'\x7fELF'):
                    raise ValueError('Rust runtime differs from the accepted native executable')
                if any(marker.encode() in data for marker in contract.get('forbidden_markers', [])):
                    raise ValueError('development authority entered the shipping Rust image')
    finally:
        subprocess.run(['docker', 'rm', '-v', container], check=True, stdout=subprocess.DEVNULL, timeout=30)
