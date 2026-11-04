#!/usr/bin/env python3
"""Run the shared Make gate and bind its result to this exact Actions execution."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

from configuration import load, relative
from generate import makefile

TOOLS = Path(__file__).resolve().parents[1]
SHA = re.compile(r'[a-f0-9]{40}\Z')


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments], text=True, timeout=30).strip()


def identity(root, source, tools):
    if not SHA.fullmatch(source) or not SHA.fullmatch(tools):
        raise ValueError('CI requires exact source and cookbook commits')
    if git(root, 'rev-parse', 'HEAD') != source or git(TOOLS, 'rev-parse', 'HEAD') != tools:
        raise ValueError('checked-out source or cookbook differs from the requested revision')
    for checkout in [root, TOOLS]:
        if subprocess.run(['git', '-C', str(checkout), 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode:
            raise ValueError('CI requires clean tracked source and cookbook code')
    if (root / 'Makefile').read_text() != makefile(root, tools):
        raise ValueError('Makefile differs from the pinned recipe generator')
    return git(root, 'rev-parse', 'HEAD^{tree}')


def verification():
    spec = importlib.util.spec_from_file_location('cookbook_ci_verification', TOOLS / 'helpers/verification.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def proof(root, source, tools, config):
    tree = identity(root, source, tools)
    directory = root / '.artifacts/verification'
    build = json.loads((directory / 'build.json').read_text())
    test = json.loads((directory / 'test.json').read_text())
    verify = verification()
    context = verify.execution_context()
    for record in [build['provenance'], test]:
        if (record.get('source_commit') != source or record.get('source_tree') != tree
                or record.get('tracked_source_clean') is not True
                or record.get('execution_context') != context):
            raise ValueError('build/test receipt belongs to another source or execution')
    suites = {'lint', 'unit', 'security', *[s for s in ['integration', 'e2e'] if s in config.get('tests', {})]}
    if set(test.get('test_receipts', {})) != suites or test.get('tested_commit') != source:
        raise ValueError('not every implemented group passed in this execution')
    if build['identity'] != test.get('input_identity') or build['provenance']['input_identity'] != build['identity']:
        raise ValueError('build and tests did not accept the same source inputs')
    if config['recipe'] != 'godot-addon':
        raise ValueError('CI artifact scope for this recipe is not implemented yet')
    declaration = verify.scope(artifacts=[str(Path(config['package']['archive']).parent)])
    if verify.identity(declaration) != build['identity'] or verify.artifacts(declaration) != build['artifacts']:
        raise ValueError('source or artifact bytes changed after acceptance')
    return {'version': 1, 'repository': os.environ.get('GITHUB_REPOSITORY'),
            'source_commit': source, 'source_tree': tree, 'cookbook_commit': tools,
            'status': 'passed', 'tracked_source_clean': True,
            'build_verified': True, 'tests_verified': True, 'test_groups': sorted(suites),
            'execution_context': context, 'artifacts': build['artifacts']}


def run(root, mode, source, tools, release_bump=''):
    config = load(root)
    identity(root, source, tools)
    if config['recipe'] != 'godot-addon':
        raise ValueError('this shared CI adapter currently supports the addon recipe')
    if release_bump:
        if mode != 'full':
            raise ValueError('release acceptance requires full validation')
        from publication import identity as release_identity
        release_identity(root, config, release_bump)
    environment = {**os.environ, 'COOKBOOK_ROOT': str(TOOLS), 'COOKBOOK_STAGE': mode}
    commands = ['setup', 'test:lint'] if mode == 'fast' else ['setup', 'build', 'test']
    receipt = root / '.artifacts/cookbook-validation.json'
    receipt.unlink(missing_ok=True)
    for command in commands:
        subprocess.run(['make', '--no-print-directory', command], cwd=root, env=environment, check=True)
    identity(root, source, tools)
    if mode == 'fast':
        command = ['python3', str(TOOLS / 'helpers/source-secrets.py')]
        if config.get('security', {}).get('fixtures'):
            command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
        subprocess.run(command, cwd=root, env=environment, check=True)
        return
    accepted = proof(root, source, tools, config)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(accepted, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['fast', 'full'], required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--tools', required=True)
    parser.add_argument('--release-bump', default='')
    args = parser.parse_args()
    try:
        run(Path.cwd().resolve(), args.mode, args.source, args.tools, args.release_bump)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
