#!/usr/bin/env python3
"""React Native application operations behind the shared Make contract."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

from configuration import load, relative
import images
import node_component
from native_tests import run_entries

TOOLS = Path(__file__).resolve().parents[1]
ENVIRONMENT = re.compile('[A-Z][A-Z0-9_]*\\Z')
PHASES = {'setup', 'pre_packages', 'pre_export', 'post_export', 'post_public', 'pre_images', 'post_images', 'fixtures', 'validate'}


def helper(name):
    spec = importlib.util.spec_from_file_location('react_native_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cargo():
    spec = importlib.util.spec_from_file_location('react_native_cargo', TOOLS / 'cookbook/rust_component.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def packages(config):
    return config.get('node', {}).get('packages', [])


def rust_components(config):
    return config.get('components', {}).get('rust', [])


def configure_environment(root, config):
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    values = {'APP_VERSION': revision, 'CI': '1', 'GOTOOLCHAIN': 'local',
              'TOOLS_ROOT': str(TOOLS), 'PYTHONDONTWRITEBYTECODE': '1',
              'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright')}
    if os.environ.get('APP_VERSION') not in {None, '', revision}:
        raise ValueError('APP_VERSION must match this exact source checkout')
    app = config.get('components', {}).get('app', {})
    defaults = app.get('environment', {})
    if not isinstance(defaults, dict):
        raise ValueError('application build environment must be a literal named table')
    for name, value in defaults.items():
        if not ENVIRONMENT.fullmatch(name) or not isinstance(value, str) or '\0' in value:
            raise ValueError('application build environment contains an invalid literal')
        values[name] = os.environ.get(name, value)
    if os.environ.get('GITHUB_EVENT_NAME') == 'pull_request':
        for name, value in app.get('preview_environment', {}).items():
            if not ENVIRONMENT.fullmatch(name) or not isinstance(value, str):
                raise ValueError('preview public environment must use literal named values')
            values[name] = os.environ.get(name, value)
        for name, source in app.get('preview_variables', {}).items():
            if not ENVIRONMENT.fullmatch(name) or source != 'COOKBOOK_PREVIEW_CLERK_KEY':
                raise ValueError('preview variables must select the bounded public Clerk key input')
            value = os.environ.get(source, '')
            if not value:
                raise ValueError('the declared Preview public Clerk variable is unavailable')
            values[name] = value
    os.environ.update(values)
    return dict(os.environ)


def required_tools(root, config, stage):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if stage in {'local', 'dev'}:
        return list(pins)
    result = {'python', 'node'}
    for value in packages(config):
        package, _ = node_component.validate(value, root)
        if package.get('manager', 'npm') == 'bun':
            result.add('bun')
    if stage in {'fast', 'full'}:
        result.add('actionlint')
        if config.get('lint', {}).get('shell_paths'):
            result.add('shellcheck')
        if rust_components(config):
            result.add('rust')
    if stage in {'unit', 'integration', 'e2e'} and rust_components(config):
        result.add('rust')
    if stage in {'full', 'security'}:
        result.add('trivy')
        if config.get('security', {}).get('go_images'):
            result |= {'go', 'go:golang.org/x/vuln/cmd/govulncheck'}
        if config.get('security', {}).get('spdx_images'):
            result.add('syft')
    if stage == 'format':
        if rust_components(config):
            result.add('rust')
        if any(node_component.validate(value, root)[0].get('format_paths') for value in packages(config)):
            result.add('npm:prettier')
    absent = result - pins.keys()
    if absent:
        raise ValueError('required SDKs must be declared in mise.toml: ' + ', '.join(sorted(absent)))
    return sorted(result)


def verification_scope(root, config):
    definition = images.definition(root, config)
    inputs = {key: list(value) for key, value in definition['inputs'].items()}
    inputs['artifacts'] += config.get('artifacts', {}).get('fixture_paths', [])
    return helper('verification').scope(**inputs)


def artifact_paths(root, config):
    return [*verification_scope(root, config)['artifacts'], *config.get('artifacts', {}).get('evidence_paths', [])]


def test_suites(root, config):
    names = {'lint', 'unit', 'security', *config.get('tests', {})}
    names.update(entry.get('suite') for entry in config.get('components', {}).get('rust_tests', []))
    if not names <= {'lint', 'unit', 'integration', 'e2e', 'security'}:
        raise ValueError('unknown React Native native test suite')
    return [name for name in ['lint', 'unit', 'security', 'integration', 'e2e'] if name in names]


def run(arguments, root, environment, timeout=None):
    subprocess.run(arguments, cwd=root, env=environment, check=True, timeout=timeout)


def native(root, entries, environment):
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'runner', 'path', 'cwd', 'args', 'timeout', 'environment', 'phase', 'stages'}:
            raise ValueError('invalid native product component entry')
        path = relative(root, entry.get('path'), exists=True)
        if not path.is_file() or Path(entry['path']).parts[0] not in {'app', 'api', 'internal', 'tests'}:
            raise ValueError('native product generators and assertions must remain in their owning component')
        runner = {'python': sys.executable, 'node': 'node', 'bun': 'bun', 'bash': 'bash'}.get(entry.get('runner'))
        if runner is None:
            raise ValueError('unknown native product component runner')
        arguments = entry.get('args', [])
        if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
            raise ValueError('native product arguments must be literal strings')
        timeout = entry.get('timeout', 300)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError('native product operations require bounded deadlines')
        values = entry.get('environment', {})
        if not isinstance(values, dict) or any(not ENVIRONMENT.fullmatch(key) or not isinstance(value, str) for key, value in values.items()):
            raise ValueError('native product environment must use literal named values')
        values = {key: value.replace('{revision}', environment['APP_VERSION']) for key, value in values.items()}
        run([runner, str(path), *arguments], relative(root, entry.get('cwd', '.'), exists=True), environment | values, timeout)


def phase(root, config, environment, name, stage='full'):
    if name not in PHASES:
        raise ValueError('unknown native build component phase')
    entries = []
    for entry in config.get('components', {}).get('native', []):
        if entry.get('phase') not in PHASES:
            raise ValueError('unknown native product build component phase')
        if entry.get('phase') != 'setup' and 'stages' in entry:
            raise ValueError('native build and validation components cannot silently vary by CI stage')
        stages = entry.get('stages', ['full', 'local'])
        if not isinstance(stages, list) or not stages or not set(stages) <= {'fast', 'full', 'local', 'security'}:
            raise ValueError('native SDK setup requires explicit supported stages')
        if entry['phase'] == name and stage in stages:
            entries.append(entry)
    native(root, entries, environment)


def cargo_records(config, environment):
    records = []
    for value in rust_components(config):
        record = {key: item for key, item in value.items() if key != 'output_directory'}
        record['environment'] = {key: item.replace('{revision}', environment['APP_VERSION'])
                                 for key, item in record.get('environment', {}).items()}
        records.append(record)
    return records


def setup(root, config, environment):
    stage = os.environ.get('COOKBOOK_STAGE', 'local')
    run(['mise', 'install', *required_tools(root, config, stage)], root, environment)
    selected = []
    for value in packages(config):
        package, _ = node_component.validate(value, root)
        if stage != 'fast' or any(package.get(key) for key in ['expo', 'typecheck', 'lint', 'format_check']):
            selected.append(package)
    node_component.install(root, selected, environment, run)
    records = cargo_records(config, environment)
    if records:
        if stage == 'fast':
            version = tomllib.loads((root / 'mise.toml').read_text())['tools']['rust']
            run(['rustup', 'component', 'add', '--toolchain', version, 'rustfmt', 'clippy'], root, environment)
        elif stage != 'security':
            cargo().setup(root, records, environment, run)
    phase(root, config, environment, 'setup', stage)
    if stage in {'local', 'full'}:
        for name in config.get('components', {}).get('playwright', {}).get('packages', []):
            directory = relative(root, name, exists=True)
            command = ['bun', 'x', '--no-install', 'playwright', 'install']
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                command.append('--with-deps')
            run([*command, '--only-shell', 'chromium'], directory, environment)


def lint(root, config, environment):
    run(['actionlint'], root, environment)
    run([sys.executable, str(TOOLS / 'helpers/documentation-policy.py')], root, environment)
    node_component.lint(root, packages(config), environment, run)
    for value in packages(config):
        _, directory = node_component.validate(value, root)
        run(['node', str(TOOLS / 'helpers/dependency-compatibility.cjs'), str(directory)], root, environment)
    if rust_components(config):
        cargo().lint(root, cargo_records(config, environment), environment, run)
    paths = []
    for name in config.get('lint', {}).get('shell_paths', []):
        paths.append(str(relative(root, name, exists=True)))
    if paths:
        run(['shellcheck', *paths], root, environment)
    images.validate_compose(root, config, environment, run)
    if config.get('tests', {}).get('lint'):
        run_entries(root, config, environment, 'lint')


def build(root, config, environment):
    app = config.get('components', {}).get('app', {})
    for name in app.get('forbidden_environment', []):
        if not ENVIRONMENT.fullmatch(name) or environment.get(name):
            raise ValueError('development-only environment cannot enter a shipping build: ' + name)
    phase(root, config, environment, 'pre_packages')
    node_component.build(root, packages(config), environment, run)
    phase(root, config, environment, 'pre_export')
    directory = relative(root, app.get('directory', 'app'), exists=True)
    run([sys.executable, str(TOOLS / 'helpers/export-web.py'), '--app', str(directory)], root, environment)
    exported = relative(root, str(directory.relative_to(root) / 'dist'), exists=True)
    html_count = len(list(exported.rglob('*.html')))
    if not app.get('minimum_html', 1) <= html_count <= app.get('maximum_html', 100000):
        raise ValueError('Expo export does not satisfy the declared HTML output count')
    for path in exported.rglob('*'):
        if path.is_symlink():
            raise ValueError('shipping app output cannot contain symlinks')
        if path.is_file():
            content = path.read_bytes()
            if any(value.encode() in content for value in app.get('forbidden_markers', [])):
                raise ValueError('development fixture marker entered the shipping app')
    phase(root, config, environment, 'post_export')
    if app.get('public_target'):
        target = relative(root, app['public_target'])
        if app.get('replace_public'):
            public = relative(root, 'public')
            if public.exists():
                shutil.rmtree(public)
        elif target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(exported, target)
        if app.get('release_file'):
            (target / 'release.json').write_text(json.dumps({'revision': environment['APP_VERSION']}) + '\n')
    phase(root, config, environment, 'post_public')
    images.validate_compose(root, config, environment, run)
    records = cargo_records(config, environment)
    if records:
        cargo().build(root, records, environment, run)
        for original, record in zip(rust_components(config), records, strict=True):
            if record.get('build', {}).get('enabled', True) is False:
                continue
            options = record | record.get('build', {})
            profile = options.get('profile', 'release')
            target = Path(environment.get('CARGO_TARGET_DIR', str(root / Path(record['manifest']).parent / 'target')))
            if record.get('target_directory'):
                target = relative(root, record['target_directory'])
            if record.get('target'):
                target /= record['target']
            target /= 'release' if profile == 'release' else 'debug'
            destination = relative(root, original.get('output_directory', '.artifacts'))
            destination.mkdir(parents=True, exist_ok=True)
            for name in options.get('binaries', []):
                shutil.copy2(target / name, destination / name)
        cargo().validate_build(root, records, environment, run)
    caddy = config.get('components', {}).get('caddy', {})
    if caddy:
        if caddy.get('host'):
            images.host_caddy(root, environment, run)
        else:
            images.stage_caddy(root)
    phase(root, config, environment, 'pre_images')
    if 'config' in images.components(images.definition(root, config)):
        run(['bun', str(TOOLS / 'helpers/hostnames.mjs')], root, environment)
    images.build(root, config, environment, run, native=native)
    phase(root, config, environment, 'post_images')
    phase(root, config, environment, 'fixtures')
    images.package(root, config, environment, run)
    validate_outputs(root, config, environment)


def validate_outputs(root, config, environment):
    images.validate_release(root, config, environment)
    value = images.definition(root, config)
    app = config.get('components', {}).get('app', {})
    if app.get('verify_image', True):
        images.static_image(value['image_base'] + '-gate:' + environment['APP_VERSION'],
                            relative(root, app.get('image_source', 'public'), exists=True),
                            app.get('installed_root', '/srv/public'))
    for name, contract in config.get('components', {}).get('rust_images', {}).items():
        if name not in images.components(value):
            raise ValueError('Rust runtime contract names an undeclared image')
        images.rust_image(root, value['image_base'] + '-' + name + ':' + environment['APP_VERSION'], contract)
    phase(root, config, environment, 'validate')


def current_build(root, config):
    verification = helper('verification')
    declaration = verification_scope(root, config)
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    if receipt.get('identity') != verification.identity(declaration) or receipt.get('artifacts') != verification.artifacts(declaration):
        raise ValueError('missing, changed or stale build; run make build before this check')


def security(root, config, environment):
    command = [sys.executable, str(TOOLS / 'helpers/source-secrets.py')]
    if config.get('security', {}).get('fixtures'):
        command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
    run(command, root, environment)
    node_component.audit(root, packages(config), environment, run)
    records = cargo_records(config, environment)
    if records:
        images.rust_inventory(root, sorted({record['manifest'] for record in records}), environment, run)
    definition = images.definition(root, config)
    declared = set(images.components(definition))
    for name in config.get('security', {}).get('images', []):
        if name not in declared:
            raise ValueError('security scan names an undeclared release image')
        image = definition['image_base'] + '-' + name + ':' + environment['APP_VERSION']
        output = relative(root, '.artifacts/security')
        output.mkdir(parents=True, exist_ok=True)
        if name in config.get('security', {}).get('spdx_images', []):
            run(['syft', 'docker:' + image, '-o', 'spdx-json=' + str(output / (name + '.spdx.json'))], root, environment)
        run(['trivy', 'image', '--image-src', 'docker', '--scanners', 'vuln', '--pkg-types', 'os,library',
             '--severity', 'HIGH,CRITICAL', '--format', 'json', '--output', str(output / (name + '.json')), image], root, environment)
        run(['trivy', 'image', '--image-src', 'docker', '--scanners', 'vuln', '--pkg-types', 'os,library',
             '--severity', 'HIGH,CRITICAL', '--ignore-unfixed', '--exit-code', '1', image], root, environment)
    pins = tomllib.loads((root / 'mise.toml').read_text())['tools']
    for name in config.get('security', {}).get('go_images', []):
        if name not in declared:
            raise ValueError('Go runtime scan names an undeclared release image')
        run([sys.executable, str(TOOLS / 'helpers/scan-image.py'), definition['image_base'] + '-' + name + ':' + environment['APP_VERSION'],
             '--expected-go', pins['go']], root, environment)
    if config.get('tests', {}).get('security'):
        run_entries(root, config, environment, 'security')


def behavioral_tests(root, config, environment, suite):
    count = 0
    if suite == 'unit':
        count += node_component.test(root, packages(config), environment, run)
    records = {record['manifest']: record for record in cargo_records(config, environment)}
    for entry in config.get('components', {}).get('rust_tests', []):
        if entry.get('suite') == suite:
            if entry.get('manifest') not in records:
                raise ValueError('native Rust test selects an undeclared component')
            record = records[entry['manifest']] | {'test': entry['selectors']}
            cargo().test(root, [record], environment, run)
            count += 1
    if config.get('tests', {}).get(suite):
        run_entries(root, config, environment, suite)
        count += 1
    if not count:
        raise ValueError('native behavioral test group has no real implemented checks: ' + suite)


def check_sdks(root, config, operation, environment, suite=None):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
        raise ValueError('use the exact caller-pinned Python SDK; run make setup first')
    stage = {'fmt': 'format', 'test:lint': 'fast', 'test:unit': 'unit',
             'test:integration': 'integration', 'test:e2e': 'e2e', 'test:security': 'security'}.get(operation, 'full')
    if operation == '_test':
        stage = 'fast' if suite == 'lint' else suite
    for name in required_tools(root, config, stage):
        subprocess.run(['mise', 'where', name], cwd=root, env=environment, check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build', 'test', '_test',
        'test:lint', 'test:unit', 'test:integration', 'test:e2e', 'test:security', 'clean'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'react-native':
            raise ValueError('React Native operations require the application recipe')
        environment = configure_environment(root, config)
        if args.operation not in {'setup', 'clean'}:
            check_sdks(root, config, args.operation, environment, args.suite)
        if args.operation == 'setup':
            setup(root, config, environment)
        elif args.operation == 'fmt':
            count = node_component.format(root, packages(config), environment, run)
            if rust_components(config):
                cargo().format(root, cargo_records(config, environment), environment, run)
                count += 1
            if not count:
                raise ValueError('recipe has no implemented native formatter')
        elif args.operation == '_build':
            build(root, config, environment)
        elif args.operation == 'validate':
            current_build(root, config)
        elif args.operation == 'clean':
            paths = config.get('clean', {}).get('paths', [])
            if not paths:
                raise ValueError('clean requires explicit generated output paths')
            helper('clean').clean(root, paths)
        else:
            verification = helper('verification')
            directory = Path('.artifacts/verification')
            declaration = verification_scope(root, config)
            if args.operation == 'build':
                verification.run_command('build', 'build', declaration,
                    [sys.executable, str(Path(__file__).resolve()), '_build'], directory)
            elif args.operation == 'test':
                verification.run_session(test_suites(root, config), ['make', '--no-print-directory', '_cookbook-tests'], directory)
            elif args.operation == '_test':
                if args.suite == 'lint':
                    lint(root, config, environment)
                elif args.suite == 'security':
                    security(root, config, environment)
                else:
                    behavioral_tests(root, config, environment, args.suite)
            else:
                suite = args.operation.split(':', 1)[1]
                if suite in {'integration', 'e2e', 'security'}:
                    current_build(root, config)
                verification.run_command('test', suite, declaration,
                    [sys.executable, str(Path(__file__).resolve()), '_test', '--suite', suite], directory)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
