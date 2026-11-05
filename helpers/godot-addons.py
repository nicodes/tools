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


def tree_hash(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('released addon must be a regular local directory')
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('released addon may not contain symbolic links')
        if path.is_file():
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


def verify(project):
    project = Path(project)
    lock = declaration(project)
    addons = project / 'addons'
    if addons.is_symlink() or not addons.is_dir():
        raise ValueError('expected a local released addon root')
    if {p.name for p in addons.iterdir()} != {name.replace('/', '_') for name in lock}:
        raise ValueError('addon root contains missing or undeclared releases')
    for name, entry in lock.items():
        path = addons / name.replace('/', '_')
        if not (path / 'plugin.cfg').is_file() or tree_hash(path) != entry['tree_sha256']:
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


def restore(project, fetch=download, *, taint_markers=()):
    project = Path(project).resolve()
    lock = declaration(project)
    if (not isinstance(taint_markers, (list, tuple)) or len(taint_markers) > 8
            or any(not isinstance(name, str) or not re.fullmatch(r'\.[a-z][a-z0-9-]{0,79}', name) for name in taint_markers)):
        raise ValueError('addon recovery markers must be bounded literal hidden filenames')
    try:
        verify(project)
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
        for index, (name, entry) in enumerate(lock.items()):
            archive = temporary / (str(index) + '.zip')
            fetch(name[1:], entry['asset_id'], archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['asset_sha256']:
                raise ValueError('downloaded addon differs from its reviewed asset digest')
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
    verify(project)


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
