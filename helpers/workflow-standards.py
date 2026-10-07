#!/usr/bin/env python3
"""Check generic workflow controls and caller-owned developer entry points."""
import argparse
import json
from pathlib import Path
import re

COMMANDS = {'install', 'lint', 'test', 'build', 'check', 'dev', 'stop', 'clean'}
PROFILES = {'static-website', 'go-cli', 'expo-application', 'godot-game',
            'godot-addon', 'python-art', 'configuration', 'documentation', 'retained'}


def load_workflow(text):
    import yaml
    class Loader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f'duplicate YAML key: {key}')
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(text, Loader=Loader)


def validate_profile(data):
    if data.get('version') != 1 or data.get('profile') not in PROFILES:
        raise ValueError('caller must declare a supported engineering profile v1')
    commands = data.get('commands', {})
    if set(commands) != COMMANDS:
        raise ValueError('every canonical command needs an argv or unsupported reason')
    for name, value in commands.items():
        if isinstance(value, dict) and set(value) == {'unsupported'} and isinstance(value['unsupported'], str) and value['unsupported'].strip():
            continue
        if not isinstance(value, list) or not value or not all(isinstance(argument, str) and argument for argument in value):
            raise ValueError(f'{name}: supply one argv or an explicit unsupported reason')
    if not isinstance(commands['check'], list):
        raise ValueError('check must execute the profile verification gate')


def findings(path, data):
    errors = []
    concurrency = data.get('concurrency', {})
    group = concurrency.get('group', '') if isinstance(concurrency, dict) else concurrency
    if re.search(r'\$\{(?!\{)\s*github\.', str(group)):
        errors.append('literal GitHub expression in concurrency group')
    jobs = dict(data.get('jobs', {}))
    if data.get('runs', {}).get('using') == 'composite':
        jobs['composite'] = data['runs']
    for key, job in jobs.items():
        if 'runs-on' in job and (type(job.get('timeout-minutes')) is not int or not 1 <= job['timeout-minutes'] <= 120):
            errors.append(f'{key}: runner job needs a timeout between 1 and 120 minutes')
        uses = [job.get('uses')] + [step.get('uses') for step in job.get('steps', [])]
        for reference in filter(None, uses):
            if reference.startswith('./'):
                continue
            if reference.startswith('docker://'):
                if not re.fullmatch(r'docker://[^\s@]+@sha256:[a-f0-9]{64}', reference):
                    errors.append(f'{key}: container action must name an immutable digest')
                continue
            if not re.fullmatch(r'[\w.-]+/[\w./-]+@[a-f0-9]{40}', reference):
                errors.append(f'{key}: external uses must name an immutable commit')
    return [f'{path}: {error}' for error in errors]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--profile', type=Path, default=Path('engineering-profile.json'))
    parser.add_argument('--command', choices=sorted(COMMANDS), help='execute the caller-owned command locally')
    args = parser.parse_args()
    profile = json.loads((args.root/args.profile).read_text())
    validate_profile(profile)
    if args.command:
        command = profile['commands'][args.command]
        if isinstance(command, dict):
            print(f'{args.command}: unsupported: {command["unsupported"]}')
            return
        import subprocess
        process = subprocess.run(command, cwd=args.root)
        raise SystemExit(process.returncode if process.returncode >= 0 else 128-process.returncode)
    errors = []
    files = list((args.root/'.github/workflows').glob('*.y*ml'))
    files.extend((args.root/'.github/actions').rglob('action.y*ml'))
    for file in sorted(files):
        errors.extend(findings(file.relative_to(args.root), load_workflow(file.read_text())))
    if errors:
        raise SystemExit('\n'.join(errors))
    print('Developer profile and workflow controls verified')


if __name__ == '__main__':
    main()
