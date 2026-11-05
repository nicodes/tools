#!/usr/bin/env python3
"""Release a closed native CLI payload after complete same-run source validation."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

import ci
from configuration import load, ci_settings, relative
import native_ci
import orchestration
import rust_cli

TOOLS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cookbook_cli_github_release', TOOLS / 'helpers/github_release.py')
github_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(github_release)


def policy(config):
    values = config.get('publication', {})
    allowed = {'tag_prefix', 'initial', 'initial_version', 'version_identity', 'cargo_manifest', 'acceptance_timeout'}
    if config.get('recipe') != 'rust-cli' or not isinstance(values, dict) or set(values)-allowed:
        raise ValueError('CLI publication requires native semantic release policy')
    result = {'tag_prefix': 'v', 'initial': 'first', 'version_identity': 'number', 'acceptance_timeout': 10, **values}
    if (not re.fullmatch(r'(?:[a-z][a-z0-9-]*-)?v', result['tag_prefix'])
            or result['initial'] not in {'first', 'bump'} or result['version_identity'] not in {'number', 'tag'}
            or type(result['acceptance_timeout']) is not int or not 1 <= result['acceptance_timeout'] <= 45):
        raise ValueError('invalid CLI release identity or existing acceptance budget')
    if 'initial_version' in result and (not isinstance(result['initial_version'], str)
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', result['initial_version'])):
        raise ValueError('CLI initial version must be a stable semantic version')
    if 'cargo_manifest' in result:
        value = result['cargo_manifest']
        if not isinstance(value, str) or not value.endswith('Cargo.toml') or Path(value).is_absolute() or '..' in Path(value).parts:
            raise ValueError('CLI package version must select its native Cargo manifest')
    orchestration.contract(config)
    return result


def context(source, environment=None):
    values = environment if environment is not None else os.environ
    repository = values.get('GITHUB_REPOSITORY', '')
    github_release.validate_request(repository, source, 'v0.0.1', values)
    if values.get('GITHUB_WORKFLOW_REF') != repository + '/.github/workflows/release.yml@refs/heads/main':
        raise ValueError('CLI publication must retain its source release workflow identity')
    for name in ['GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT']:
        if not re.fullmatch(r'[1-9][0-9]*', values.get(name, '')):
            raise ValueError('CLI publication requires an identified Actions execution')
    return values


def select_version(root, config, bump, tags):
    values = policy(config)
    tags = list(tags)
    if values.get('initial_version') and not any(re.fullmatch(re.escape(values['tag_prefix']) + r'[0-9]+\.[0-9]+\.[0-9]+', value) for value in tags):
        tags.append(values['tag_prefix'] + values['initial_version'])
    number, tag = github_release.version(bump, values['tag_prefix'], tags, values['initial'])
    if tag in tags:
        raise ValueError('release tag already exists; refusing to replace it')
    if 'cargo_manifest' in values:
        manifest = tomllib.loads(relative(root, values['cargo_manifest'], exists=True).read_text())
        if manifest.get('package', {}).get('version') != number:
            raise ValueError('commit the native Cargo package version before releasing')
    return {'tag': tag, 'version': tag if values['version_identity'] == 'tag' else number,
            'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}


def identity(root, config, source, tools, bump):
    context(source)
    ci.identity(root, source, tools)
    subprocess.run(['git', 'fetch', '--tags', '--force'], cwd=root, check=True, timeout=60)
    tags = subprocess.check_output(['git', 'tag', '--list'], cwd=root, text=True, timeout=30).splitlines()
    result = select_version(root, config, bump, tags)
    # Never mutate a tag or release while planning. The publisher claims it atomically.
    github_release.absent(os.environ['GITHUB_REPOSITORY'], 'git/ref/tags/' + result['tag'])
    github_release.absent(os.environ['GITHUB_REPOSITORY'], 'releases/tags/' + result['tag'])
    return result


def read_proof(root):
    path = relative(root, '.artifacts/cookbook-validation.json', exists=True)
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('CLI release proof must be a bounded regular file')
    return json.loads(path.read_text())


def accepted_payload(root, config, proof, source, tools, tag, environment=None, jobs=None):
    environment = context(source, environment)
    ci.identity(root, source, tools)
    values = policy(config)
    github_release.tag(tag)
    if not re.fullmatch(re.escape(values['tag_prefix']) + r'[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('CLI release tag differs from its native prefix')
    configuration_sha256 = hashlib.sha256(subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:cookbook.toml'], timeout=30)).hexdigest()
    if (proof.get('source_commit') != source or proof.get('source_tree') != ci.git(root, 'rev-parse', 'HEAD^{tree}')
            or proof.get('cookbook_commit') != tools or proof.get('recipe') != 'rust-cli'
            or proof.get('configuration_sha256') != configuration_sha256
            or proof.get('status') != 'passed' or proof.get('tracked_source_clean') is not True
            or proof.get('build_verified') is not True or proof.get('tests_verified') is not True
            or proof.get('test_groups') != orchestration.contract(config)['test_groups']
            or proof.get('coverage') != 'complete-host-and-native-matrix'):
        raise ValueError('CLI release requires complete exact-source host and native proof')
    native_ci.context_matches(proof.get('execution_context', {}), environment, job='validate')
    repository, run, attempt = native_ci.run_identity(environment)
    jobs = jobs if jobs is not None else native_ci.github(repository, f'/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100')
    native_ci.validate_acceptance(proof.get('native_matrix'), config, jobs, source, tools, environment, configuration_sha256)
    host = [value for value in jobs.get('jobs', []) if value.get('name', '').endswith('Make full')]
    if (len(host) != 1 or host[0].get('status') != 'completed' or host[0].get('conclusion') != 'success'
            or ci_settings(config)['runner'] not in host[0].get('labels', [])):
        raise ValueError('CLI release requires the actual successful complete host job')
    expected_version = tag if values['version_identity'] == 'tag' else tag[len(values['tag_prefix']):]
    native_proofs = proof['native_matrix']['proofs']
    if not native_proofs or any(value.get('version_identity') != expected_version for value in native_proofs):
        raise ValueError('native CLI archives were built with another release version')
    package, native = rust_cli.package_data(root, config), rust_cli.helper('native_cli')
    names = [native.archive_name(package, value) for value in package['native_targets']]
    names += [*package.get('extra_files', []), 'checksums.txt']
    if package.get('release_metadata'):
        names.append('release.json')
    if len(names) != len(set(names)):
        raise ValueError('CLI release assets must have unique native names')
    expected = {}
    for name in names:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
            raise ValueError('CLI release assets require plain native names')
        path = relative(root, 'dist/' + name, exists=True)
        if not path.is_file():
            raise ValueError('CLI release assets must be regular files')
        digest = native.digest(path)
        if proof.get('artifacts', {}).get('dist/' + name) != digest:
            raise ValueError('CLI release bytes differ from the accepted full gate')
        expected[name] = digest
    github_release.payload(root / 'dist', expected)
    for target in package['native_targets']:
        native.inspect_archive(root / 'dist' / native.archive_name(package, target), package, target)
    if (root / 'dist/checksums.txt').read_text() != native.checksums(root / 'dist', [name for name in names if name not in {'checksums.txt', 'release.json'}]):
        raise ValueError('CLI release checksums differ from the complete native archive set')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['identity', 'accept', 'publish'])
    parser.add_argument('--source', required=True)
    parser.add_argument('--tools', required=True)
    parser.add_argument('--bump', default='')
    parser.add_argument('--tag', default='')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if args.operation == 'identity':
            values = identity(root, config, args.source, args.tools, args.bump)
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    for key, value in values.items():
                        output.write(key + '=' + value + '\n')
            print(json.dumps(values))
        else:
            expected = accepted_payload(root, config, read_proof(root), args.source, args.tools, args.tag)
            if args.operation == 'publish':
                github_release.publish(root / 'dist', expected, args.tag, args.source, os.environ['GITHUB_REPOSITORY'])
            print(json.dumps(expected, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
