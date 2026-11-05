#!/usr/bin/env python3
"""Native Godot addon operations behind the shared Make command contract."""
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import sdk_environment

from configuration import godot_pins, load, relative
from native_tests import run_entries
import node_component

TOOLS = Path(__file__).resolve().parents[1]


def helper(name):
    spec = importlib.util.spec_from_file_location('cookbook_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(root):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('env', {}) if (root / 'mise.toml').is_file() else {}
    return {**sdk_environment.preserve_mise(), **({'GODOT_VERSION': pins['GODOT_VERSION']} if 'GODOT_VERSION' in pins else {}),
            'TOOLS_ROOT': str(TOOLS),
            'GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
            'XDG_DATA_HOME': str(root / '.artifacts/godot-data'),
            'XDG_CONFIG_HOME': str(root / '.artifacts/godot-config'),
            'TMPDIR': str(root / '.artifacts/tmp'),
            'GODOT_SILENCE_ROOT_WARNING': '1',
            'PLAYWRIGHT_CLI_BIN': str(root / '.artifacts/playwright-cli'),
            'GD_WEB_OUTPUT_DIR': str(root / '.artifacts/e2e/web-export-evidence'),
            'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright'),
            'RUNNER_TEMP': os.environ.get('RUNNER_TEMP', str(root / '.artifacts/tmp')),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(root / '.artifacts/godot/bin') + os.pathsep + os.environ.get('PATH', '')}


def run(argv, root, env, timeout=None):
    subprocess.run(argv, cwd=root, env=env, check=True, timeout=timeout)


def package_inputs(root, config):
    values = config['package']
    if set(values) - {'source', 'manifest', 'archive', 'script_uids', 'format'} or not {'source', 'manifest', 'archive'} <= set(values):
        raise ValueError('addon packaging requires source, closed manifest and archive paths')
    if type(values.get('script_uids', False)) is not bool:
        raise ValueError('script UID policy must be explicit boolean data')
    return (relative(root, values['source'], exists=True),
            relative(root, values['manifest'], exists=True),
            relative(root, values['archive']))


def package_module(config):
    if config.get('package', {}).get('format', 'closed') == 'manifest':
        import manifest_addon
        return manifest_addon
    if config.get('package', {}).get('format', 'closed') != 'closed':
        raise ValueError('unsupported addon package format')
    return None


def verification_scope(root, config):
    module = package_module(config)
    if module:
        paths = module.artifact_paths(root, config)
    else:
        _source, _manifest, archive = package_inputs(root, config)
        paths = [archive.relative_to(root).as_posix(), archive.relative_to(root).as_posix()+'.sha256']
    import addon_fixtures
    paths += addon_fixtures.artifact_paths(root, config)
    return helper('verification').scope(artifacts=paths)


def artifact_paths(root, config):
    return [*verification_scope(root, config)['artifacts'], *evidence_paths(root, config)]


def evidence_paths(root, config):
    paths = list(config.get('artifacts', {}).get('evidence_paths', []))
    for name in config.get('publication', {}).get('evidence_assets', []):
        if name not in paths:
            paths.append(name)
    if config.get('publication', {}).get('provenance'):
        paths.append('dist/CANDIDATE.json')
    if len(paths) != len(set(paths)):
        raise ValueError('artifact evidence paths must be distinct')
    for path in paths:
        relative(root, path)
    return paths


def post_test_evidence(root, config, env, commit):
    provenance = config.get('publication', {}).get('provenance')
    if provenance:
        if provenance != 'package-provenance.json':
            raise ValueError('candidate provenance requires the native package provenance policy')
        run(['node', str(TOOLS / 'helpers/addon-provenance.mjs'), 'candidate', '--commit', commit,
             '--zip', str(relative(root, config['package']['archive'], exists=True)),
             '--out', str(relative(root, 'dist/CANDIDATE.json'))], root, env)


def packages(config):
    return [({'directory': item, 'manager': 'npm', 'audit': True} if isinstance(item, str) else item)
            for item in config.get('node', {}).get('packages', [])]


def required_tools(root, config, stage='full'):
    pins = tomllib.loads((root/'mise.toml').read_text()).get('tools', {})
    if stage == 'full':
        return list(pins)
    names = ['python', *(['actionlint'] if stage == 'fast' else [])]
    if packages(config):
        names.append('node')
    if not set(names) <= set(pins):
        raise ValueError('required source-check SDKs must be pinned in mise.toml')
    return names


def test_suites(root, config):
    return ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if name in config.get('tests', {})]]


def configure_environment(root, config):
    return environment(root)


def setup(root, config, env):
    (root / '.artifacts').mkdir(exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    (root / '.artifacts/tmp').mkdir(exist_ok=True)
    import python_component
    if os.environ.get('COOKBOOK_STAGE') == 'fast':
        # Source/configuration checks do not need an editor or export templates.
        run(['mise', 'install', *required_tools(root, config, 'fast')], root, env)
        python_component.setup(root, config, env)
        return
    run(['mise', 'install'], root, env)
    pins = godot_pins(root)
    command = [sys.executable, str(TOOLS / 'helpers/godot-setup.py'),
               '--version', pins['GODOT_VERSION'], '--expected-version', pins['GODOT_EXPECTED_VERSION'],
               '--binary-checksum', pins['GODOT_BINARY_CHECKSUM'],
               '--templates-checksum', pins['GODOT_TEMPLATES_CHECKSUM'],
               '--origin', 'godot-builds', '--root', str(root / '.artifacts/godot')]
    command.append('--templates')
    run(command, root, env)
    python_component.setup(root, config, env)
    sdk = lambda argv, directory, values: run(['mise', 'exec', '--', *argv], directory, values)
    node_component.install(root, packages(config), env, sdk)
    for value in packages(config):
        _data, directory = node_component.validate(value, root)
        if (directory/'node_modules').is_dir():
            (directory/'node_modules/.gdignore').touch()
    import addon_dependencies
    addon_dependencies.setup(root, config, env)
    browser = config.get('components', {}).get('browser')
    if browser:
        if set(browser)-{'package', 'cli_version'}:
            raise ValueError('unsupported native browser setup setting')
        helper('playwright-local').prepare(root, browser['package'], cli_version=browser.get('cli_version'),
            run=lambda argv, **kwargs: subprocess.run(['mise', 'exec', '--', *argv], **kwargs))


def lint(root, config, env):
    for name in config.get('lint', {}).get('required_paths', []):
        relative(root, name, exists=True)
    module = package_module(config)
    if module:
        module.inputs(root, config)
    else:
        source, manifest, _ = package_inputs(root, config)
        helper('addon-package').source_files(source, manifest, config['package'].get('script_uids', False))
    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--', '*.py', '*.sh'], cwd=root).split(b'\0')
    for encoded in tracked:
        if not encoded:
            continue
        path = relative(root, encoded.decode(), exists=True)
        if path.suffix == '.py':
            ast.parse(path.read_bytes(), filename=str(path))
        else:
            run(['bash', '-n', str(path)], root, env)
    if (root / '.github/workflows').is_dir():
        run(['actionlint'], root, env)
    run([sys.executable, str(TOOLS / 'helpers/documentation-policy.py')], root, env)
    if 'lint' in config.get('tests', {}):
        test_entries(root, config, env, 'lint')


def test_entries(root, config, env, suite):
    run_entries(root, config, env, suite)


def current_build(root, config, verification):
    declaration = verification_scope(root, config)
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
        if args.operation not in {'setup', 'clean'} and (root / 'mise.toml').is_file():
            tools = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
            if tools.get('python') != '.'.join(map(str, sys.version_info[:3])):
                raise ValueError('use the exact caller-pinned Python SDK; run make setup first')
            required = ['python']
            if args.operation == 'fmt':
                required.append('pipx:gdtoolkit')
            if args.operation in {'test', 'test:lint'} or (args.operation == '_test' and args.suite == 'lint'):
                required.append('actionlint')
            if config.get('node', {}).get('packages') and args.operation in {'test', 'test:security', '_test'}:
                required.append('node')
            for name in required:
                if name not in tools:
                    raise ValueError('declare the required SDK in mise.toml: ' + name)
                subprocess.run(['mise', 'where', name], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
        if args.operation == 'setup':
            setup(root, config, env)
        elif args.operation == 'fmt':
            paths = config.get('format', {}).get('paths', ['addon', 'tests'])
            for name in paths:
                relative(root, name, exists=True)
            run(['gdformat', *paths], root, env)
        elif args.operation in ['_build', 'validate']:
            if args.operation == '_build':
                node_component.build(root, packages(config), env, run)
            module = package_module(config)
            if module:
                (module.build if args.operation == '_build' else module.verify)(root, config)
            else:
                source, manifest, archive = package_inputs(root, config)
                package = helper('addon-package')
                if args.operation == '_build':
                    digest = package.build(source, manifest, archive, config['package'].get('script_uids', False))
                    archive.with_suffix(archive.suffix + '.sha256').write_text(digest + '  ' + archive.name + '\n')
                else:
                    digest = package.verify(archive, manifest, source=source, script_uids=config['package'].get('script_uids', False))
                    package.verify_checksum(archive)
                print(digest + '  ' + config['package']['archive'])
            if args.operation == '_build':
                import addon_fixtures
                addon_fixtures.build_exports(root, config, env)
        elif args.operation == 'clean':
            paths = config.get('clean', {}).get('paths', [])
            if not paths:
                raise ValueError('cleanup requires explicit generated output paths')
            helper('clean').clean(root, paths)
        else:
            verification = helper('verification')
            directory = Path('.artifacts/verification')
            declaration = verification_scope(root, config)
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
                    node_component.audit(root, packages(config), env, run)
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
