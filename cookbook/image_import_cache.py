"""Reload a closed, accepted image set without resetting unrelated Docker state."""
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import time

from configuration import relative

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
    """Read save metadata and authenticate selected layers without extracting paths."""
    result = {}
    with tarfile.open(path, 'r:*') as archive:
        members, total = {}, 0
        for member in archive:
            remaining(deadline)
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or str(name) in {'.', ''}
                    or str(name) != member.name.rstrip('/') or '\\' in member.name
                    or any(ord(char) < 32 for char in member.name)
                    or str(name) in members or not (member.isfile() or member.isdir())
                    or member.size < 0 or (member.isdir() and member.size)):
                raise ValueError('image archive contains unsafe, linked or repeated paths')
            members[str(name)] = member
            total += member.size
            if len(members) > MAX_MEMBERS or total > MAX_EXPANDED:
                raise ValueError('image archive exceeds its member or expanded size bound')

        def small(name):
            if not isinstance(name, str):
                raise ValueError('image metadata path must be a regular archive member')
            member = members.get(name)
            if member is None or not member.isfile() or not 0 < member.size <= MAX_JSON:
                raise ValueError('image archive metadata is missing or oversized')
            remaining(deadline)
            with archive.extractfile(member) as stream:
                return stream.read(MAX_JSON + 1)

        manifests = json.loads(small('manifest.json'), object_pairs_hook=unique_object)
        if not isinstance(manifests, list) or not 1 <= len(manifests) <= len(expected):
            raise ValueError('image archive has no bounded declared image set')
        checked_layers, image_rows, expanded = {}, {}, 0
        for manifest in manifests:
            if (not isinstance(manifest, dict) or set(manifest) - {'Config', 'RepoTags', 'Layers', 'LayerSources'}
                    or not {'Config', 'RepoTags', 'Layers'} <= set(manifest)
                    or not isinstance(manifest['RepoTags'], list) or not manifest['RepoTags']):
                raise ValueError('image save manifest has no closed tagged image contract')
            body = small(manifest['Config'])
            identity = 'sha256:' + hashlib.sha256(body).hexdigest()
            config = json.loads(body, object_pairs_hook=unique_object)
            rootfs = config.get('rootfs') if isinstance(config, dict) else None
            layers = manifest['Layers']
            if (not isinstance(rootfs, dict) or rootfs.get('type') != 'layers'
                    or not isinstance(rootfs.get('diff_ids'), list)
                    or not isinstance(layers, list) or len(layers) != len(rootfs['diff_ids'])
                    or len(layers) > 128
                    or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in rootfs['diff_ids'])):
                raise ValueError('image config has no bounded immutable RootFS layer identities')
            for reference in manifest['RepoTags']:
                if (not isinstance(reference, str) or reference not in expected or reference in result
                        or expected[reference] != identity):
                    raise ValueError('image archive tags or config IDs differ from the accepted image set')
                result[reference] = {'id': identity, 'rootfs': rootfs['diff_ids']}
                image_rows[reference] = manifest
            sources = manifest.get('LayerSources')
            if 'LayerSources' in manifest and (not isinstance(sources, dict) or set(sources) != set(rootfs['diff_ids'])):
                raise ValueError('image layer descriptors differ from the config RootFS')
            for name, wanted in zip(layers, rootfs['diff_ids']):
                member = members.get(name) if isinstance(name, str) else None
                if member is None or not member.isfile():
                    raise ValueError('image archive layer is missing')
                if name not in checked_layers:
                    remaining(deadline)
                    with archive.extractfile(member) as stream:
                        magic = stream.read(2)
                        stream.seek(0)
                        raw, size = digest(stream, deadline, maximum=MAX_EXPANDED - expanded)
                        observed = raw
                        if magic == b'\x1f\x8b':
                            stream.seek(0)
                            with gzip.GzipFile(fileobj=stream) as content:
                                observed, size = digest(content, deadline, maximum=MAX_EXPANDED - expanded)
                    expanded += size
                    checked_layers[name] = {'diff': 'sha256:' + observed, 'raw': 'sha256:' + raw,
                                            'gzip': magic == b'\x1f\x8b'}
                if checked_layers[name]['diff'] != wanted:
                    raise ValueError('image layer bytes differ from the accepted config RootFS')
                if sources is not None:
                    descriptor = sources[wanted]
                    media = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.docker.image.rootfs.diff.tar'}
                    if checked_layers[name]['gzip']:
                        media = {'application/vnd.oci.image.layer.v1.tar+gzip', 'application/vnd.docker.image.rootfs.diff.tar.gzip'}
                    if (not isinstance(descriptor, dict) or set(descriptor) != {'digest', 'mediaType', 'size'}
                            or descriptor['digest'] != checked_layers[name]['raw']
                            or type(descriptor['size']) is not int or descriptor['size'] != member.size
                            or descriptor['mediaType'] not in media):
                        raise ValueError('image layer descriptor differs from its admitted archive bytes')

        if 'index.json' in members or 'oci-layout' in members:
            layout = json.loads(small('oci-layout'), object_pairs_hook=unique_object)
            index = json.loads(small('index.json'), object_pairs_hook=unique_object)
            if (layout != {'imageLayoutVersion': '1.0.0'} or not isinstance(index, dict)
                    or set(index) != {'schemaVersion', 'mediaType', 'manifests'} or index['schemaVersion'] != 2
                    or index['mediaType'] != 'application/vnd.oci.image.index.v1+json'
                    or not isinstance(index['manifests'], list) or len(index['manifests']) != len(result)):
                raise ValueError('OCI image index differs from the closed saved-image contract')

            def descriptor_body(descriptor, media):
                if (not isinstance(descriptor, dict) or set(descriptor) != {'digest', 'size', 'mediaType'}
                        or not isinstance(descriptor['digest'], str) or not DIGEST.fullmatch(descriptor['digest'])
                        or type(descriptor['size']) is not int or descriptor['mediaType'] not in media):
                    raise ValueError('OCI descriptor is outside the admitted image contract')
                body = small('blobs/sha256/' + descriptor['digest'][7:])
                if len(body) != descriptor['size'] or 'sha256:' + hashlib.sha256(body).hexdigest() != descriptor['digest']:
                    raise ValueError('OCI descriptor differs from its immutable metadata bytes')
                return json.loads(body, object_pairs_hook=unique_object)

            seen = set()
            for descriptor in index['manifests']:
                if not isinstance(descriptor, dict) or set(descriptor) != {'digest', 'size', 'mediaType', 'annotations'}:
                    raise ValueError('OCI image index has an unbound descriptor')
                annotations = descriptor['annotations']
                if (not isinstance(annotations, dict)
                        or set(annotations) != {'io.containerd.image.name', 'org.opencontainers.image.ref.name'}):
                    raise ValueError('OCI image index has unbound tag annotations')
                reference = annotations['io.containerd.image.name']
                if (not isinstance(reference, str) or reference not in result or reference in seen
                        or annotations['org.opencontainers.image.ref.name'] not in {reference, reference.rsplit(':', 1)[1]}):
                    raise ValueError('OCI image index tag differs from the accepted image set')
                seen.add(reference)
                image = descriptor_body({key: value for key, value in descriptor.items() if key != 'annotations'},
                                        {'application/vnd.oci.image.manifest.v1+json'})
                if (not isinstance(image, dict) or set(image) != {'schemaVersion', 'mediaType', 'config', 'layers'}
                        or image['schemaVersion'] != 2 or image['mediaType'] != descriptor['mediaType']
                        or not isinstance(image['layers'], list)
                        or len(image['layers']) != len(image_rows[reference]['Layers'])):
                    raise ValueError('OCI manifest differs from the admitted saved image')
                config = image['config']
                descriptor_body(config, {'application/vnd.oci.image.config.v1+json'})
                if config['digest'] != result[reference]['id']:
                    raise ValueError('OCI manifest config differs from the accepted image ID')
                for layer, name in zip(image['layers'], image_rows[reference]['Layers']):
                    media = ('application/vnd.oci.image.layer.v1.tar+gzip' if checked_layers[name]['gzip']
                             else 'application/vnd.oci.image.layer.v1.tar')
                    if (not isinstance(layer, dict) or set(layer) != {'digest', 'size', 'mediaType'}
                            or layer['digest'] != checked_layers[name]['raw'] or layer['mediaType'] != media
                            or type(layer['size']) is not int or layer['size'] != members[name].size
                            or name != 'blobs/sha256/' + layer['digest'][7:]):
                        raise ValueError('OCI layer differs from the admitted archive layer bytes')
    return result


def reload(root, source, archives, expected, environment, timeout=600):
    """Admit every archive and cache reference before nonforce scoped reset/load.

    The caller owns source/configuration/build-proof admission and supplies the
    complete declared image set. This helper independently closes the archive
    tag/config/layer set, protects aliases and containers, and retains one total
    deadline across validation, cache inspection, reset, load and verification.
    """
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError('accepted image reload requires its original bounded deadline')
    deadline = time.monotonic() + timeout
    root = Path(root).resolve(strict=True)
    if (not isinstance(source, str) or not SHA.fullmatch(source)
            or not isinstance(expected, dict) or not 1 <= len(expected) <= 64
            or any(not isinstance(ref, str) or not REFERENCE.fullmatch(ref) or not ref.endswith(':' + source)
                   or not isinstance(value, str) or not DIGEST.fullmatch(value) for ref, value in expected.items())
            or not isinstance(archives, list) or not 1 <= len(archives) <= 16):
        raise ValueError('accepted image reload requires exact source tags, IDs and archives')

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
        return subprocess.run(command, cwd=root, env=environment, check=True, capture_output=True,
                              text=True, timeout=min(cap, remaining(deadline))).stdout

    listing = execute(['docker', 'image', 'ls', '--all', '--digests', '--no-trunc', '--format', '{{json .}}'])
    rows, present = [], {}
    for line in listing.splitlines():
        row = json.loads(line, object_pairs_hook=unique_object)
        if not isinstance(row, dict) or not isinstance(row.get('ID'), str) or not DIGEST.fullmatch(row['ID']):
            raise ValueError('Docker image metadata has no complete immutable ID')
        repository, tag = row.get('Repository'), row.get('Tag')
        if not isinstance(repository, str) or not isinstance(tag, str) or not isinstance(row.get('Digest'), str):
            raise ValueError('Docker image metadata omits tag or digest scope')
        reference = repository + ':' + tag
        if reference in expected:
            if reference in present and present[reference] != row['ID']:
                raise ValueError('existing source tag has ambiguous cache image IDs')
            present[reference] = row['ID']
        rows.append((row, reference))
    # A source tag can name another build of the same commit. Its cached ID is
    # not accepted build evidence, but removing that tag still requires protecting
    # every alias/digest/container of BOTH the prior and incoming image IDs.
    targets = set(expected.values()) | set(present.values())
    for row, reference in rows:
        repository, tag = row['Repository'], row['Tag']
        if row['ID'] in targets and (row['Digest'] not in {'', '<none>'}
                or (reference not in expected and (repository, tag) != ('<none>', '<none>'))):
            raise ValueError('accepted cache image has an out-of-scope alias or digest')
    for identity in sorted(targets):
        if execute(['docker', 'container', 'ls', '--all', '--quiet', '--filter', 'ancestor=' + identity]).strip():
            raise ValueError('accepted cache image is used by an existing container')

    def unchanged(path):
        relative(root, path.relative_to(root).as_posix(), exists=True)
        info = path.stat()
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != fingerprints[path]
                or path.is_symlink() or info.st_nlink != 1):
            raise ValueError('accepted image archive changed during cache admission')

    for path in paths:
        unchanged(path)
    if present:
        ordered = sorted(present, key=lambda ref: (-len(admitted[ref]['rootfs']), ref))
        execute(['docker', 'image', 'rm', '--no-prune', '--', *ordered])
    for path in paths:
        unchanged(path)
        execute(['docker', 'image', 'load', '--input', str(path)], cap=600)
    for reference, wanted in admitted.items():
        rows = json.loads(execute(['docker', 'image', 'inspect', reference]), object_pairs_hook=unique_object)
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get('Id') != wanted['id'] or not isinstance(rows[0].get('RootFS'), dict)
                or rows[0]['RootFS'].get('Layers') != wanted['rootfs']):
            raise ValueError('reloaded image differs from its accepted config ID or RootFS')
    remaining(deadline)
    return admitted
