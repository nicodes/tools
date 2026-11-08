"""Reuse or repair a closed, accepted image set from authenticated store state."""
import hashlib
import json
import importlib.util
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import time

_spec = importlib.util.spec_from_file_location('_tools_cache_oci', Path(__file__).with_name('oci_archive.py'))
oci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oci)
runtime = oci.runtime

def relative(root, name, *, exists=False):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(character) < 32 for character in name)):
        raise ValueError('image archive paths must be nonempty relative paths')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
        raise ValueError('image archive paths must stay inside the checkout')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('image archive path escapes the checkout')
    if any(p.is_symlink() for p in [target, *target.parents] if p != root and p.is_relative_to(root)):
        raise ValueError('image archive input/output paths may not be symlinks')
    if exists and not target.exists():
        raise ValueError('image archive input is missing: ' + name)
    return target

MAX_ARCHIVE = 2 << 30
MAX_EXPANDED = 16 << 30
MAX_JSON = 1 << 20
MAX_MEMBERS = 20000
SHA = re.compile(r'[a-f0-9]{40}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
REFERENCE = re.compile(r'[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?'
                       r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+:[a-f0-9]{40}\Z')


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ValueError('accepted image reload deadline exceeded')
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('image metadata contains repeated fields')
        result[key] = value
    return result


def digest(stream, deadline, *, maximum):
    value, size = hashlib.sha256(), 0
    while True:
        remaining(deadline)
        chunk = stream.read(1 << 20)
        if not chunk:
            return value.hexdigest(), size
        size += len(chunk)
        if size > maximum:
            raise ValueError('accepted image archive exceeds its size bound')
        value.update(chunk)


def archive_images(path, expected, deadline):
    """Authenticate one exact OCI image and bind it to an accepted source tag."""
    metadata = oci.inspect_archive(path, timeout=remaining(deadline))
    candidates = [reference for reference, identity in expected.items()
                  if identity == metadata['config_digest'] and metadata['reference'] in
                  {reference, reference.rsplit(':', 1)[-1]}]
    if len(candidates) != 1:
        raise ValueError('OCI archive reference/config differs from the accepted image set')
    return {candidates[0]: {'id': metadata['config_digest'],
                            'rootfs': [row['diff_id'] for row in metadata['layers']]}}


def reload(root, source, archives, expected, environment, timeout=600):
    """Admit every archive and cache reference before deriving reuse/load/repair.

    The caller owns source/configuration/build-proof admission and supplies the
    complete declared image set. This helper independently closes the archive
    tag/config/layer set, protects aliases and containers, and retains one total
    deadline across validation, cache inspection, repair, load and verification.
    """
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError('accepted image reload requires its original bounded deadline')
    deadline = time.monotonic() + timeout
    root = Path(root).resolve(strict=True)
    if (not isinstance(source, str) or not SHA.fullmatch(source)
            or not isinstance(expected, dict) or len(expected) > 64
            or any(not isinstance(ref, str) or not REFERENCE.fullmatch(ref) or not ref.endswith(':' + source)
                   or not isinstance(value, str) or not DIGEST.fullmatch(value) for ref, value in expected.items())
            or not isinstance(archives, list) or len(archives) > 64):
        raise ValueError('accepted image reload requires exact source tags, IDs and archives')

    if not expected:
        if archives:
            raise ValueError('an empty image set must not contain image archives')
        return {}

    admitted, paths, fingerprints = {}, [], {}
    for record in archives:
        if (not isinstance(record, dict) or set(record) != {'path', 'sha256'}
                or not isinstance(record['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', record['sha256'])):
            raise ValueError('accepted image archive requires its exact path and SHA256')
        path = relative(root, record['path'], exists=True)
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ARCHIVE
                or path in paths):
            raise ValueError('accepted image archive must be a distinct bounded unlinked regular file')
        with path.open('rb') as stream:
            observed, _ = digest(stream, deadline, maximum=MAX_ARCHIVE)
        if observed != record['sha256']:
            raise ValueError('image archive differs from the accepted build bytes')
        values = archive_images(path, expected, deadline)
        if admitted.keys() & values.keys():
            raise ValueError('image references repeat across accepted archives')
        admitted.update(values)
        paths.append(path)
        fingerprints[path] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if set(admitted) != set(expected):
        raise ValueError('image archives do not cover the complete accepted image set')

    def execute(command, cap=60):
        argv, values = runtime.command(command, environment)
        return subprocess.run(argv, cwd=root, env=values, check=True, capture_output=True,
                              text=True, timeout=min(cap, remaining(deadline))).stdout

    listing = execute(['image', 'ls', '--all', '--digests', '--no-trunc', '--format', '{"ID":{{json .Id}},"Repository":{{json .Repository}},"Tag":{{json .Tag}},"RepoDigests":{{json .RepoDigests}}}'])
    rows, present = [], {}
    for line in listing.splitlines():
        row = json.loads(line, object_pairs_hook=unique_object)
        if not isinstance(row, dict):
            raise ValueError('Podman image metadata has no complete immutable ID')
        row['ID'] = runtime.config_digest(row.get('ID'))
        repository, tag = row.get('Repository'), row.get('Tag')
        if (not isinstance(repository, str) or not isinstance(tag, str)
                or not isinstance(row.get('RepoDigests'), list)
                or any(not isinstance(value, str) for value in row['RepoDigests'])):
            raise ValueError('Podman image metadata omits tag or digest scope')
        reference = repository + ':' + tag
        if reference in expected:
            if reference in present and present[reference] != row['ID']:
                raise ValueError('existing source tag has ambiguous cache image IDs')
            present[reference] = row['ID']
        rows.append((row, reference))
    def inspect(reference):
        values = json.loads(execute(['image', 'inspect', reference]), object_pairs_hook=unique_object)
        if isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict):
            values[0]['Id'] = runtime.config_digest(values[0].get('Id'))
        if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict)
                or not isinstance(values[0].get('Id'), str) or not DIGEST.fullmatch(values[0]['Id'])
                or not isinstance(values[0].get('RootFS'), dict)
                or not isinstance(values[0]['RootFS'].get('Layers'), list)
                or len(values[0]['RootFS']['Layers']) > 128
                or any(not isinstance(layer, str) or not DIGEST.fullmatch(layer)
                       for layer in values[0]['RootFS']['Layers'])):
            raise ValueError('Podman image inspection has no immutable config ID and RootFS')
        return {'id': values[0]['Id'], 'rootfs': values[0]['RootFS']['Layers']}

    repair = any(identity != expected[reference] for reference, identity in present.items())
    for reference, identity in present.items():
        if identity != expected[reference]:
            continue
        # Unknown storage/inspection failures do not authorize deletion. Only
        # independently observed config/rootfs differences derive scoped repair.
        observed = inspect(reference)
        if observed['id'] != identity:
            raise ValueError('source tag changed during image cache admission')
        if observed != admitted[reference]:
            repair = True

    mutating = repair or set(present) != set(expected)
    if mutating:
        # A verified no-op leaves borrowed aliases and containers unchanged.
        # Any load/repair still protects BOTH prior and incoming image IDs.
        targets = set(expected.values()) | set(present.values())
        for row, reference in rows:
            repository, tag = row['Repository'], row['Tag']
            if row['ID'] in targets and (row['RepoDigests']
                    or (reference not in expected and (repository, tag) != ('<none>', '<none>'))):
                raise ValueError('accepted cache image has an out-of-scope alias or digest')
        for identity in sorted(targets):
            if execute(['container', 'ls', '--all', '--quiet', '--filter', 'ancestor=' + identity]).strip():
                raise ValueError('accepted cache image is used by an existing container')

    def unchanged(path):
        relative(root, path.relative_to(root).as_posix(), exists=True)
        info = path.stat()
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != fingerprints[path]
                or path.is_symlink() or info.st_nlink != 1):
            raise ValueError('accepted image archive changed during cache admission')

    for path in paths:
        unchanged(path)
    if repair and present:
        ordered = sorted(present, key=lambda ref: (-len(admitted[ref]['rootfs']), ref))
        execute(['image', 'rm', '--no-prune', '--', *ordered])
    if mutating:
        for path in paths:
            unchanged(path)
            execute(['image', 'load', '--input', str(path)], cap=600)
            values = archive_images(path, expected, deadline)
            for reference, value in values.items():
                execute(['image', 'tag', value['id'], reference])
    for reference, wanted in admitted.items():
        if inspect(reference) != wanted:
            raise ValueError('imported image differs from its accepted config ID or RootFS')
    remaining(deadline)
    return admitted
