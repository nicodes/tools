#!/usr/bin/env python3
"""Restore and verify exact addon release bytes from caller-owned provenance."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile


def tree_hash(root, generated=()):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('released addon must be a regular local directory')
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('released addon may not contain symbolic links')
        if path.is_file():
            if path.relative_to(root).as_posix() in generated:
                continue
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        elif not path.is_dir():
            raise ValueError('released addon contains non-regular content')
    return digest.hexdigest()


def declaration(project):
    project = Path(project)
    for name in ['gdam.json', 'addon-provenance.json']:
        path = project / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1 << 20:
            raise ValueError('addon declarations must be bounded regular source files')
    manifest = json.loads((project / 'gdam.json').read_text())['addons']
    lock = json.loads((project / 'addon-provenance.json').read_text())
    if not isinstance(manifest, dict) or not manifest or set(manifest) != set(lock):
        raise ValueError('addon manifest and reviewed provenance must name the same releases')
    installed = set()
    for name, entry in lock.items():
        if not re.fullmatch(r'@[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', name):
            raise ValueError('addon names must identify a GitHub owner and repository')
        mangled = name.replace('/', '_')
        if mangled in installed:
            raise ValueError('addon names have conflicting installation paths')
        installed.add(mangled)
        if manifest[name].get('tag') != entry.get('tag'):
            raise ValueError('addon release differs from its reviewed provenance')
        if type(entry.get('asset_id')) is not int or entry['asset_id'] <= 0:
            raise ValueError('addon release requires an exact GitHub asset identity')
        for field in ['asset_sha256', 'tree_sha256']:
            if not isinstance(entry.get(field), str) or not re.fullmatch('[a-f0-9]{64}', entry[field]):
                raise ValueError('addon provenance requires exact SHA256 identities')
    frozen = project / 'gdam.lock'
    if frozen.exists() or frozen.is_symlink():
        if frozen.is_symlink() or not frozen.is_file() or frozen.stat().st_size > 1 << 20:
            raise ValueError('frozen addon lock must be a bounded regular source file')
        resolved = json.loads(frozen.read_text())
        requirements = {name: entry.get('tag') for name, entry in manifest.items()}
        if resolved.get('version') != 1 or resolved.get('requirements') != requirements:
            raise ValueError('Frozen lock requirements differ from the addon manifest')
        releases = resolved.get('addons', {})
        if set(releases) != {name + '@' + entry['tag'] for name, entry in lock.items()}:
            raise ValueError('Frozen lock and byte provenance name different releases')
        parents = {name: {'gdam.json'} for name in lock}
        for identity, release in releases.items():
            dependencies = release.get('dependencies', {})
            if not isinstance(dependencies, dict):
                raise ValueError('Frozen lock dependencies must describe the closed selected release graph')
            for name, tag in dependencies.items():
                if name not in lock or tag != lock[name]['tag']:
                    raise ValueError('Frozen lock dependency differs from its exact selected release')
                parents[name].add(identity)
        for name, entry in lock.items():
            release = releases[name + '@' + entry['tag']]
            installs = release.get('installs')
            installation = installs[0] if isinstance(installs, list) and len(installs) == 1 and isinstance(installs[0], dict) else {}
            required_by = installation.get('required_by')
            valid_install = (set(installation) == {'path', 'required_by'}
                and installation.get('path') == 'addons/' + name.replace('/', '_')
                and isinstance(required_by, list) and all(isinstance(parent, str) for parent in required_by)
                and len(required_by) == len(set(required_by)) and set(required_by) == parents[name])
            if (release.get('name') != name or release.get('tag') != entry['tag']
                    or release.get('commit_sha') != entry.get('commit')
                    or not isinstance(entry.get('commit'), str) or not re.fullmatch('[a-f0-9]{40}', entry['commit'])
                    or release.get('asset_id') != entry['asset_id']
                    or release.get('asset_digest') != 'sha256:' + entry['asset_sha256']
                    or not valid_install):
                raise ValueError('Frozen lock identity or install path differs from byte provenance')
    return lock


def binding_inputs(project, bindings):
    binary = Path(bindings['binary'])
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('addon bindings require the installed regular pinned CLI')
    return {'tool': bindings['tool'], 'pin': bindings['pin'],
            'cli_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'sources': {name: hashlib.sha256((project / name).read_bytes()).hexdigest()
                        for name in ['gdam.json', 'gdam.lock', 'addon-provenance.json']}}


def binding_paths(project):
    frozen = project / 'gdam.lock'
    if frozen.is_symlink() or not frozen.is_file() or frozen.stat().st_size > 1 << 20:
        raise ValueError('canonical addon bindings require the exact frozen lock')
    if json.loads((project / 'gdam.json').read_text()).get('package'):
        raise ValueError('release addon bindings require a dependency-only project manifest')
    return sorted(release['installs'][0]['path'] + '/.gdam/deps.gd'
                  for release in json.loads(frozen.read_text())['addons'].values() if release.get('dependencies'))


def regular(path, limit=1 << 20):
    if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
            or not path.is_file() or path.stat().st_size > limit):
        raise ValueError('addon binding evidence must be a bounded unlinked regular file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('addon binding evidence repeats a field')
        result[key] = value
    return result


def verify_bindings(project, bindings):
    expected = binding_paths(project)
    path = project / 'addons/.cookbook-bindings.json'
    regular(path)
    receipt = json.loads(path.read_text(), object_pairs_hook=unique_fields)
    if (not isinstance(receipt, dict) or set(receipt) != {'version', 'inputs', 'bridges'}
            or type(receipt['version']) is not int or receipt['version'] != 1 or receipt['inputs'] != binding_inputs(project, bindings)
            or not isinstance(receipt['bridges'], dict) or set(receipt['bridges']) != set(expected)):
        raise ValueError('addon binding receipt differs from frozen sources or exact pinned CLI')
    if (project / '.gdam').exists() or (project / '.gdam').is_symlink():
        raise ValueError('addon binding project contains undeclared generated content')
    actual = set()
    for directory in (project / 'addons').rglob('.gdam'):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('addon binding directory must be regular and unlinked')
        children = list(directory.iterdir())
        if len(children) != 1 or children[0].name != 'deps.gd':
            raise ValueError('addon bindings contain undeclared generated content')
        name = children[0].relative_to(project).as_posix()
        actual.add(name)
        if name not in receipt['bridges'] or regular(children[0]) != receipt['bridges'][name]:
            raise ValueError('canonical addon binding bytes changed after setup')
    if actual != set(expected):
        raise ValueError('canonical addon bindings are missing or undeclared')
    return set(expected)


def prepare_bindings(project, bindings, archives):
    """Run the actual caller-pinned generator offline, only in explicit setup."""
    expected = binding_paths(project)
    inputs = binding_inputs(project, bindings)
    version = subprocess.check_output([str(bindings['binary']), '--version'], text=True, timeout=10)
    if version.splitlines()[0] != 'gdam ' + bindings['pin']['version']:
        raise ValueError('installed addon CLI differs from its exact caller version')
    with tempfile.TemporaryDirectory(prefix='godot-addon-bindings-') as temporary:
        candidate = Path(temporary) / 'project'; candidate.mkdir()
        cache = Path(temporary) / 'cache'; cache.mkdir()
        for name in ['gdam.json', 'gdam.lock']:
            shutil.copyfile(project / name, candidate / name)
        lock = declaration(project)
        for name, entry in lock.items():
            archive = archives[name]
            if regular(archive, 512 << 20) != entry['asset_sha256']:
                raise ValueError('offline addon cache differs from its sealed release')
            shutil.copyfile(archive, cache / (entry['asset_sha256'] + '.zip'))
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'GH_TOKEN', 'GITHUB_TOKEN', 'GH_HOST'}}
        subprocess.run([str(bindings['binary']), 'install', '--frozen-lockfile', '--offline', '--cache-dir', str(cache)],
                       cwd=candidate, env=environment, check=True, timeout=180)
        bridges = {}
        for name, entry in lock.items():
            relative = 'addons/' + name.replace('/', '_')
            generated = ['.gdam/deps.gd'] if relative + '/.gdam/deps.gd' in expected else []
            if tree_hash(candidate / relative, generated) != entry['tree_sha256']:
                raise ValueError('native addon generator changed sealed release bytes')
        actual = {p.relative_to(candidate).as_posix() for p in (candidate / 'addons').rglob('*')
                  if '.gdam' in p.relative_to(candidate).parts and not p.is_dir()}
        if actual != set(expected):
            raise ValueError('native addon generator produced undeclared bridge files')
        for name in expected:
            bridges[name] = regular(candidate / name)
        # No source or ordinary release asset is copied back from the generator.
        for name in expected:
            target = project / name
            if target.parent.is_symlink() or target.is_symlink():
                raise ValueError('canonical addon binding destination may not be linked')
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(candidate / name, target)
        directory = project / 'addons'
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError('addon binding receipt requires an unlinked directory')
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / '.cookbook-bindings.json'
        if path.is_symlink():
            raise ValueError('addon binding receipt may not be linked')
        path.write_text(json.dumps({'version': 1, 'inputs': inputs, 'bridges': bridges}, sort_keys=True, indent=2) + '\n')
        path.chmod(0o600)


def verify(project, bindings=None):
    project = Path(project)
    lock = declaration(project)
    generated = verify_bindings(project, bindings) if bindings else set()
    addons = project / 'addons'
    if addons.is_symlink() or not addons.is_dir():
        raise ValueError('expected a local released addon root')
    expected = {name.replace('/', '_') for name in lock} | ({'.cookbook-bindings.json'} if bindings else set())
    if {p.name for p in addons.iterdir()} != expected:
        raise ValueError('addon root contains missing or undeclared releases')
    for name, entry in lock.items():
        path = addons / name.replace('/', '_')
        allowed = [value.removeprefix('addons/' + name.replace('/', '_') + '/') for value in generated
                   if value.startswith('addons/' + name.replace('/', '_') + '/')]
        if not (path / 'plugin.cfg').is_file() or tree_hash(path, allowed) != entry['tree_sha256']:
            raise ValueError('installed addon bytes differ from reviewed release: ' + name)
    return lock


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        if urllib.parse.urlsplit(target).scheme != 'https':
            raise ValueError('addon assets require HTTPS')
        redirected = super().redirect_request(request, response, code, message, headers, target)
        if urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(target).netloc:
            redirected.remove_header('Authorization')
        return redirected


def download(repository, asset, destination):
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    headers = {'Accept': 'application/octet-stream', 'User-Agent': 'tools-godot-addons',
               'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(f'https://api.github.com/repos/{repository}/releases/assets/{asset}', headers=headers)
    opener = urllib.request.build_opener(AssetRedirect())
    with opener.open(request, timeout=60) as incoming, destination.open('wb') as output:
        total = 0
        for block in iter(lambda: incoming.read(1024 * 1024), b''):
            total += len(block)
            if total > 512 * 1024 * 1024:
                raise ValueError('addon archive exceeds its download bound')
            output.write(block)


def extract(archive, destination):
    with zipfile.ZipFile(archive) as source:
        seen = set()
        for entry in source.infolist():
            name = entry.filename.rstrip('/')
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (not name or path.is_absolute() or '..' in path.parts or path.as_posix() != name
                    or '\\' in name or ':' in name or name in seen or stat.S_ISLNK(mode)):
                raise ValueError('unsafe or duplicate addon archive entry')
            seen.add(name)
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError('addon archive contains non-regular content')
        if source.testzip() is not None:
            raise ValueError('addon archive CRC verification failed')
        source.extractall(destination)


def restore(project, fetch=download, *, taint_markers=(), bindings=None):
    project = Path(project).resolve()
    lock = declaration(project)
    if (not isinstance(taint_markers, (list, tuple)) or len(taint_markers) > 8
            or any(not isinstance(name, str) or not re.fullmatch(r'\.[a-z][a-z0-9-]{0,79}', name) for name in taint_markers)):
        raise ValueError('addon recovery markers must be bounded literal hidden filenames')
    try:
        verify(project, bindings)
        print('Using byte-verified addon cache.')
        return
    except ValueError:
        pass
    # Registry installation stays optional: the closed asset lock is sufficient
    # to recover without registry availability or floating version resolution.
    addons = project / 'addons'
    if addons.is_symlink():
        raise ValueError('addon root may not be a symbolic link')
    addons.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='godot-addon-assets-') as directory:
        temporary = Path(directory)
        planned = []
        archives = {}
        for index, (name, entry) in enumerate(lock.items()):
            archive = temporary / (str(index) + '.zip')
            fetch(name[1:], entry['asset_id'], archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['asset_sha256']:
                raise ValueError('downloaded addon differs from its reviewed asset digest')
            archives[name] = archive
            staging = temporary / str(index)
            staging.mkdir()
            extract(archive, staging)
            if not (staging / 'plugin.cfg').is_file() or tree_hash(staging) != entry['tree_sha256']:
                raise ValueError('downloaded addon tree differs from its reviewed release')
            target = addons / name.replace('/', '_')
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError('addon cache target must be a regular local directory')
            planned.append((staging, target))
        # Validate every asset before replacing any existing release.
        for staging, target in planned:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(staging, target)
        for marker in taint_markers:
            path = addons / marker
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise ValueError('addon recovery marker must be an unlinked regular file')
                path.unlink()
        if bindings:
            prepare_bindings(project, bindings, archives)
    verify(project, bindings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['verify', 'restore'])
    parser.add_argument('project', type=Path)
    args = parser.parse_args()
    try:
        globals()[args.operation](args.project)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
