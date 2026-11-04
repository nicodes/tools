#!/usr/bin/env python3
"""Native Godot addon operations behind the shared Make command contract."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from configuration import godot_pins, load, relative

TOOLS = Path(__file__).resolve().parents[1]


def helper(name):
    spec = importlib.util.spec_from_file_location('cookbook_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(root):
    return {**os.environ, 'TOOLS_ROOT': str(TOOLS),
            'GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
            'XDG_DATA_HOME': str(root / '.artifacts/godot-data'),
            'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright'),
            'RUNNER_TEMP': os.environ.get('RUNNER_TEMP', str(root / '.artifacts/tmp')),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(root / '.artifacts/godot/bin') + os.pathsep + os.environ.get('PATH', '')}


def run(argv, root, env, timeout=None):
    subprocess.run(argv, cwd=root, env=env, check=True, timeout=timeout)


def package_inputs(root, config):
    values = config['package']
    if set(values) != {'source', 'manifest', 'archive'}:
        raise ValueError('addon packaging requires source, closed manifest and archive paths')
    return (relative(root, values['source'], exists=True),
            relative(root, values['manifest'], exists=True),
            relative(root, values['archive']))


def setup(root, config, env):
    if os.environ.get('COOKBOOK_STAGE') == 'fast':
        # Source/configuration checks do not need an editor or export templates.
        run(['mise', 'install', 'python', 'actionlint'], root, env)
        return
    run(['mise', 'install'], root, env)
    pins = godot_pins(root)
    command = [sys.executable, str(TOOLS / 'helpers/godot-setup.py'),
               '--version', pins['GODOT_VERSION'], '--expected-version', pins['GODOT_EXPECTED_VERSION'],
               '--binary-checksum', pins['GODOT_BINARY_CHECKSUM'],
               '--templates-checksum', pins['GODOT_TEMPLATES_CHECKSUM'],
               '--origin', 'godot-builds', '--root', str(root / '.artifacts/godot')]
    command.append('--templates')
    (root / '.artifacts').mkdir(exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    run(command, root, env)
    for name in config.get('node', {}).get('packages', []):
        directory = relative(root, name, exists=True)
        run(['npm', 'ci', '--prefix', str(directory), '--no-audit', '--no-fund'], root, env)
    (root / '.artifacts/tmp').mkdir(exist_ok=True)


def lint(root, config, env):
    for name in config.get('lint', {}).get('required_paths', []):
        relative(root, name, exists=True)
    source, manifest, _ = package_inputs(root, config)
    expected = helper('addon-package').manifest_files(manifest)
    actual = sorted(p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file())
    if actual != expected:
        raise ValueError('addon source differs from the closed package manifest')
    if (root / '.github/workflows').is_dir():
        run(['actionlint'], root, env)
    run([sys.executable, str(TOOLS / 'helpers/documentation-policy.py')], root, env)
    if 'lint' in config.get('tests', {}):
        test_entries(root, config, env, 'lint')


def test_entries(root, config, env, suite):
    entries = config.get('tests', {}).get(suite, [])
    if not entries:
        raise ValueError('test group has no declared product checks: ' + suite)
    for entry in entries:
        program = relative(root, entry['path'], exists=True)
        if entry['runner'] in {'godot', 'godot-suite'}:
            native = helper('godot-test')
            if entry['runner'] == 'godot-suite':
                native.suite(env['GODOT_BIN'], root, program, entry['sentinel'], entry.get('timeout', 30), env)
            else:
                native.run(env['GODOT_BIN'], root, program, entry['sentinel'],
                           entry.get('timeout', 30), entry.get('args', []), env)
            continue
        executable = {'python': sys.executable, 'node': 'node', 'bun': 'bun',
                      'bash': 'bash'}[entry['runner']]
        argv = [executable, str(program), *entry.get('args', [])]
        run(argv, root, env, timeout=entry.get('timeout', 300))


def current_build(root, config, verification):
    declaration = verification.scope(artifacts=[str(Path(config['package']['archive']).parent)])
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    if receipt.get('identity') != verification.identity(declaration):
        raise ValueError('build is stale; run make build before packaged integration/E2E tests')
    if receipt.get('artifacts') != verification.artifacts(declaration):
        raise ValueError('packaged outputs changed after build verification')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build',
                                            'test:lint', 'test:unit', 'test:integration',
                                            'test:e2e', 'test:security', '_test', 'test', 'clean'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'godot-addon':
            raise ValueError('Godot addon operations require the addon recipe')
        env = environment(root)
        if args.operation == 'setup':
            setup(root, config, env)
        elif args.operation == 'fmt':
            paths = config.get('format', {}).get('paths', ['addon', 'tests'])
            for name in paths:
                relative(root, name, exists=True)
            run(['gdformat', *paths], root, env)
        elif args.operation in ['_build', 'validate']:
            source, manifest, archive = package_inputs(root, config)
            package = helper('addon-package')
            if args.operation == '_build':
                digest = package.build(source, manifest, archive)
                archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  ' + archive.name + '\n')
            else:
                digest = package.verify(archive, manifest)
                package.verify_checksum(archive)
            print(digest + '  ' + config['package']['archive'])
        elif args.operation == 'clean':
            paths = config.get('clean', {}).get('paths', [])
            if not paths:
                raise ValueError('cleanup requires explicit generated output paths')
            helper('clean').clean(root, paths)
        else:
            verification = helper('verification')
            directory = Path('.artifacts/verification')
            declaration = verification.scope(artifacts=[str(Path(config['package']['archive']).parent)])
            if args.operation == 'build':
                verification.run_command('build', 'build', declaration,
                    [sys.executable, str(Path(__file__).resolve()), '_build'], directory)
            elif args.operation == 'test':
                suites = ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if name in config.get('tests', {})]]
                verification.run_session(suites, ['make', '--no-print-directory', '_cookbook-tests'], directory)
            elif args.operation == '_test':
                if args.suite == 'lint':
                    lint(root, config, env)
                elif args.suite == 'security':
                    scan = [sys.executable, str(TOOLS / 'helpers/source-secrets.py')]
                    fixtures = config.get('security', {}).get('fixtures')
                    if fixtures:
                        scan += ['--fixtures', str(relative(root, fixtures, exists=True))]
                    run(scan, root, env)
                    for name in config.get('node', {}).get('packages', []):
                        run(['npm', 'audit', '--prefix', str(relative(root, name)), '--audit-level=high'], root, env)
                    if 'security' in config.get('tests', {}):
                        test_entries(root, config, env, 'security')
                else:
                    test_entries(root, config, env, args.suite)
            else:
                suite = args.operation.split(':', 1)[1]
                if suite in ['integration', 'e2e']:
                    current_build(root, config, verification)
                verification.run_command('test', suite, declaration,
                    [sys.executable, str(Path(__file__).resolve()), '_test', '--suite', suite], directory)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
