#!/usr/bin/env python3
"""Addon release identity and publication behind the shared manual workflow."""
import argparse
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

from configuration import load, relative

TOOLS = Path(__file__).resolve().parents[1]


def helper(name):
    spec = importlib.util.spec_from_file_location('publication_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def identity(root, config, bump, environment=None):
    environment = environment or os.environ
    if environment.get('GITHUB_REF') != 'refs/heads/main' or environment.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ValueError('releases require an explicit dispatch from main')
    values = config.get('publication', {})
    if set(values) - {'tag_prefix', 'initial', 'plugin'}:
        raise ValueError('unknown addon publication setting')
    tags = subprocess.check_output(['git', 'tag', '--list'], cwd=root, text=True).splitlines()
    version, tag = helper('github_release').version(bump, values.get('tag_prefix', 'v'), tags,
                                                   values.get('initial', 'first'))
    existing = subprocess.run(['git', 'rev-parse', '-q', '--verify', 'refs/tags/' + tag],
                              cwd=root, capture_output=True, text=True)
    if existing.returncode != 1:
        raise ValueError('release identity already exists or could not be checked')
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(relative(root, values.get('plugin', 'addon/plugin.cfg'), exists=True))
    plugin_version = parser.get('plugin', 'version', fallback='')
    if plugin_version != '"' + version + '"':
        raise ValueError('plugin version must match the next immutable release: ' + version)
    return {'version': version, 'tag': tag, 'asset': Path(config['package']['archive']).name}


def assets(root, config, destination):
    source = relative(root, config['package']['archive'], exists=True)
    package = helper('addon-package')
    package.verify(source, relative(root, config['package']['manifest'], exists=True))
    package.verify_checksum(source)
    # This is a fresh staging directory, never a release-history store.
    destination.mkdir(parents=True, exist_ok=False)
    expected = {}
    for path in [source, source.with_suffix(source.suffix + '.sha256')]:
        shutil.copy2(path, destination / path.name)
        expected[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return expected


def validate_proof(root, config, accepted, source, environment=None):
    environment = environment or os.environ
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    tools = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=TOOLS, text=True).strip()
    if (revision != source or accepted.get('source_commit') != source or accepted.get('source_tree') != tree
            or accepted.get('repository') != environment.get('GITHUB_REPOSITORY')
            or accepted.get('cookbook_commit') != tools or accepted.get('status') != 'passed'
            or accepted.get('tracked_source_clean') is not True
            or accepted.get('build_verified') is not True or accepted.get('tests_verified') is not True):
        raise ValueError('publication requires this exact source and cookbook full validation proof')
    context = accepted.get('execution_context', {})
    for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_WORKFLOW_REF']:
        if not environment.get(key) or context.get(key) != environment[key]:
            raise ValueError('publication proof belongs to another workflow execution')
    source_archive = relative(root, config['package']['archive'], exists=True)
    expected = {}
    for path in [source_archive, source_archive.with_suffix(source_archive.suffix + '.sha256')]:
        if path.is_symlink() or not path.is_file():
            raise ValueError('accepted archive and checksum must be regular files')
        expected[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if accepted.get('artifacts') != expected:
        raise ValueError('downloaded archive/checksum differ from the accepted build bytes')
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=root).returncode:
        raise ValueError('publication source changed after checkout')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['identity', 'publish'])
    parser.add_argument('--bump', choices=['patch', 'minor', 'major'], required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'godot-addon':
            raise ValueError('this publisher requires the addon recipe')
        release = identity(root, config, args.bump)
        if args.operation == 'publish':
            accepted = json.loads((root / '.artifacts/cookbook-validation.json').read_text())
            source = os.environ['GITHUB_SHA']
            validate_proof(root, config, accepted, source)
            destination = root / '.artifacts/release-assets'
            expected = assets(root, config, destination)
            helper('github_release').publish(destination, expected, release['tag'], source,
                                              os.environ['GITHUB_REPOSITORY'])
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                for name, value in release.items():
                    output.write(name + '=' + value + '\n')
        print(json.dumps(release))
    except (OSError, ValueError, KeyError, configparser.Error, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
