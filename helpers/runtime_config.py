"""Authenticate ignored native JSON projections from tracked TOML source."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


_spec = importlib.util.spec_from_file_location('_runtime_source_config', Path(__file__).with_name('source_config.py'))
source_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source_config)
FAMILIES = {'gdam': ('gdam.toml', True), 'gdam-link': ('gdam.link.toml', True),
            'ghenv': ('ghenv.toml', False), 'gdlint': ('gdlint.toml', False),
            'komizo': ('komizo.toml', True), 'readme-policy-v1': ('readme-source.toml', True)}
MAXIMUM = 8 << 20


def sha(body):
    return hashlib.sha256(body).hexdigest()


def git(root, *arguments):
    return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *arguments],
                          check=False, capture_output=True, timeout=15)


def owned(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or '..' in path.parts or '.git' in path.parts:
        raise ValueError('runtime source paths must remain in the owning checkout')
    result = root / path
    if not result.resolve().is_relative_to(root) or any(
            part.is_symlink() for part in [result, *result.parents] if part.is_relative_to(root)):
        raise ValueError('runtime source paths may not traverse links')
    return result


def regular(path, maximum=MAXIMUM):
    if path.is_symlink() or not path.is_file():
        raise ValueError('native projection requires regular unlinked files')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum
            or hasattr(os, 'geteuid') and info.st_uid != os.geteuid()):
        raise ValueError('native projection file identity or ownership differs')
    body = path.read_bytes()
    after = path.stat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('native projection changed during admission')
    return body


def encoded(data):
    raw = (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
    if not source_config.equal(json.loads(raw), data):
        raise ValueError('native JSON projection changed typed source data')
    return raw


def declaration(root, source, family):
    root = Path(root).resolve(strict=True)
    if family not in FAMILIES:
        raise ValueError('unsupported native projection family')
    expected_name, adjacent = FAMILIES[family]
    source = owned(root, source)
    name = source.relative_to(root).as_posix()
    if source.name != expected_name or git(root, 'ls-files', '--error-unmatch', '--', name).returncode:
        raise ValueError('native projection requires its tracked canonical TOML source')
    raw = source_config.read_bytes(source, maximum=MAXIMUM)
    data = source_config.loads(raw, maximum=MAXIMUM)
    key = sha(name.encode())[:24]
    directory = Path('.artifacts/runtime-config') / key
    target = source.with_suffix('.json') if adjacent else owned(root, directory / (family + '.json'))
    receipt = owned(root, directory / (family + '.receipt.json'))
    target_name = target.relative_to(root).as_posix()
    for path in [target, receipt]:
        relative = path.relative_to(root).as_posix()
        if not git(root, 'ls-files', '--error-unmatch', '--', relative).returncode:
            raise ValueError('native projection must not replace tracked source')
        if git(root, 'check-ignore', '--no-index', '--quiet', '--', relative).returncode:
            raise ValueError('native projection and receipt must be explicitly ignored')
    native = encoded(data)
    identity = {'version': 1, 'family': family, 'source': name, 'target': target_name,
                'source_sha256': sha(raw), 'native_sha256': sha(native),
                'semantic_sha256': sha(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                                  allow_nan=False).encode()),
                'compiler_sha256': sha(Path(__file__).read_bytes() + Path(source_config.__file__).read_bytes())}
    return root, source, target, receipt, data, native, identity


def prior(target, receipt, identity):
    if not target.exists():
        if receipt.exists():
            raise ValueError('native projection is missing its recorded output')
        return None
    if not receipt.exists():
        raise ValueError('refusing to overwrite foreign native JSON')
    raw, record = regular(target), json.loads(regular(receipt, 65536))
    if (not isinstance(record, dict) or set(record) != set(identity)
            or any(record.get(key) != identity[key] for key in ['version', 'family', 'source', 'target', 'compiler_sha256'])
            or record.get('native_sha256') != sha(raw)
            or record.get('semantic_sha256') != sha(json.dumps(json.loads(raw), sort_keys=True,
                                                              ensure_ascii=False, allow_nan=False).encode())):
        raise ValueError('native JSON is foreign or differs from its authenticated receipt')
    return record


def atomic(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.native-config-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(root, source, family):
    root, source, target, receipt, data, native, identity = declaration(root, source, family)
    previous = prior(target, receipt, identity)
    if previous == identity:
        if not source_config.equal(json.loads(regular(target)), data):
            raise ValueError('native projection semantics differ from current TOML source')
        return target
    if sha(source_config.read_bytes(source, maximum=MAXIMUM)) != identity['source_sha256']:
        raise ValueError('TOML source changed before native preparation')
    # Recheck every lexical parent immediately before touching generated paths.
    owned(root, target.relative_to(root)); owned(root, receipt.relative_to(root))
    atomic(target, native)
    atomic(receipt, (json.dumps(identity, sort_keys=True, indent=2) + '\n').encode())
    return verify(root, source.relative_to(root), family)


def verify(root, source, family):
    _, _, target, receipt, data, native, identity = declaration(root, source, family)
    if prior(target, receipt, identity) != identity or regular(target) != native:
        raise ValueError('prepare current TOML native configuration during explicit setup first')
    if not source_config.equal(json.loads(regular(target)), data):
        raise ValueError('native projection semantics differ from current TOML source')
    return target


@contextmanager
def temporary(root, source):
    """Translate one explicitly selected assertion input; never persist source."""
    root = Path(root).resolve(strict=True)
    source = owned(root, source)
    name = source.relative_to(root).as_posix()
    if git(root, 'ls-files', '--error-unmatch', '--', name).returncode:
        raise ValueError('native JSON assertion requires tracked TOML source')
    raw = source_config.read_bytes(source, maximum=MAXIMUM)
    data = source_config.loads(raw, maximum=MAXIMUM)
    native = encoded(data)
    with tempfile.TemporaryDirectory(prefix='native-config-') as directory:
        target = Path(directory) / 'config.json'
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(native)
        try:
            yield target
        finally:
            if (regular(target) != native or not source_config.equal(json.loads(regular(target)), data)
                    or source_config.read_bytes(source, maximum=MAXIMUM) != raw):
                raise ValueError('native assertion projection or TOML source changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'verify', 'run'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source', required=True)
    parser.add_argument('--family', choices=sorted(FAMILIES))
    arguments = sys.argv[1:]
    separator = arguments.index('--') if '--' in arguments else len(arguments)
    args = parser.parse_args(arguments[:separator])
    native_arguments = arguments[separator + 1:]
    if args.command == 'run':
        if args.family is not None or native_arguments.count('@NATIVE_CONFIG@') != 1:
            parser.error('run requires one explicit @NATIVE_CONFIG@ native argv placeholder')
    elif args.family is None or native_arguments:
        parser.error('prepare/verify require their native family and no child command')
    try:
        if args.command == 'run':
            with temporary(args.root, args.source) as path:
                command = [str(path) if item == '@NATIVE_CONFIG@' else item for item in native_arguments]
                result = subprocess.run(command, cwd=args.root, check=False)
            if result.returncode < 0:
                os.kill(os.getpid(), -result.returncode)
            raise SystemExit(result.returncode)
        result = globals()[args.command](args.root, args.source, args.family)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')
    print(result)


if __name__ == '__main__':
    main()
