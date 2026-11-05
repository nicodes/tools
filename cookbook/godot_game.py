#!/usr/bin/env python3
"""Native Godot application recipe; domain source and assertions remain caller-owned."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor

import sdk_environment

from configuration import godot_pins, load, relative
from native_tests import run_entries
import rust_component
import godot_package
import godot_sdk
import node_component

TOOLS = Path(__file__).resolve().parents[1]


def helper(name):
    spec = importlib.util.spec_from_file_location('game_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verification_scope(root, config):
    values = config.get('artifacts', {})
    if set(values) - {'paths', 'images', 'environment', 'revision_environment', 'definition'}:
        raise ValueError('unknown Godot build artifact selector')
    if values.get('definition'):
        return helper('verification').scope(**helper('verification').release_definition(relative(root, values['definition'], exists=True))['inputs'])
    return helper('verification').scope(artifacts=values.get('paths', []), images=values.get('images', []),
        environment=values.get('environment', []), revision_environment=values.get('revision_environment', []))


def artifact_paths(root, config):
    return list(verification_scope(root, config)['artifacts'])


def project(root, config):
    values = config.get('components', {}).get('godot', {})
    directory = relative(root, values.get('project', 'app'), exists=True)
    if not (directory / 'project.godot').is_file():
        raise ValueError('Godot application component must own project.godot')
    return directory, values


def validate_config(root, config):
    directory, values = project(root, config)
    allowed = {'project', 'origin', 'exports', 'test_jobs', 'import_timeout', 'import_arguments',
               'import_ignored_patterns', 'lint_arguments', 'forbidden_diagnostics', 'art_sources',
               'prepare', 'postprocess', 'packs', 'web_releases', 'import_stage', 'rust_web', 'extensions'}
    if set(values) - allowed:
        raise ValueError('unsupported Godot component setting')
    if values.get('origin', 'godot') not in {'godot', 'godot-builds'}:
        raise ValueError('unsupported Godot release archive origin')
    if values.get('lint_arguments', ['-warn']) not in [[], ['-warn']]:
        raise ValueError('Godot source lint must not mutate product code')
    if type(values.get('test_jobs', 4)) is not int or not 1 <= values.get('test_jobs', 4) <= 16:
        raise ValueError('Godot test concurrency must be bounded')
    if type(values.get('import_timeout', 180)) is not int or not 1 <= values.get('import_timeout', 180) <= 600:
        raise ValueError('Godot imports require a bounded reviewed deadline')
    if values.get('import_stage', 'setup') not in {'setup', 'build'}:
        raise ValueError('native imports belong to explicit setup or build stages')
    seen = set()
    for export in values.get('exports', []):
        if not isinstance(export, dict) or set(export) - {'preset', 'output', 'kind', 'platform', 'runtime_compatibility', 'project', 'timeout', 'import_timeout', 'import_arguments'}:
            raise ValueError('unsupported native Godot export setting')
        if export.get('kind', 'release') not in {'release', 'debug'} or export.get('platform', 'web') not in {'web', 'linux', 'windows', 'macos'}:
            raise ValueError('unsupported Godot export kind or platform')
        native_project = relative(root, export.get('project', values.get('project', 'app')), exists=True)
        if not (native_project / 'project.godot').is_file():
            raise ValueError('native exports require an owned Godot project')
        if native_project != directory and not native_project.is_relative_to(root / 'tests'):
            raise ValueError('additional native Godot export projects belong to owned test fixtures')
        helper('godot-export').native_options(export.get('import_arguments', values.get('import_arguments', [])),
            export.get('timeout', 600), export.get('import_timeout', values.get('import_timeout', 180)))
        output = relative(root, export['output'])
        if output in seen:
            raise ValueError('Godot export destinations must be distinct')
        seen.add(output)
    components = config.get('components', {})
    for component in components.get('rust', []):
        rust_component.validate(component, root)
    for package in packages(config):
        node_component.validate(package, root)
    godot_sdk.inputs(root, config)
    godot_package.validate(root, config, verification_scope(root, config)['artifacts'])
    if config.get('package'):
        raise ValueError('Godot delivery packaging integration is not implemented yet')


def packages(config):
    return [({'directory': value, 'manager': 'npm'} if isinstance(value, str) else value)
            for value in config.get('node', {}).get('packages', [])]


def package_linters(config):
    return [value for value in packages(config)
            if any(value.get(key) for key in ['typecheck', 'lint', 'format_check', 'expo'])]


def required_tools(root, config, stage='full'):
    pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
    components = config.get('components', {})
    if stage == 'full':
        mandatory = {'python', 'actionlint', 'github:aviorstudio/gdlint', 'pipx:gdtoolkit'}
        if components.get('rust'):
            mandatory.update({'rust', 'cargo:cargo-audit'})
        if components.get('godot', {}).get('art_sources'):
            mandatory.add('bun')
        for value in packages(config):
            mandatory.add('bun' if value.get('manager', 'npm') == 'bun' else 'node')
        for entries in config.get('tests', {}).values():
            for entry in entries:
                if entry['runner'] in {'node', 'node-test'}:
                    mandatory.add('node')
                if entry['runner'] in {'bun', 'bun-test'}:
                    mandatory.add('bun')
        for phase in ['prepare', 'postprocess']:
            for record in components.get('godot', {}).get(phase, []):
                if record['runner'] in {'node', 'bun'}:
                    mandatory.add(record['runner'])
        if not mandatory <= pins.keys():
            raise ValueError('required native SDKs must be declared in caller Mise pins: ' + ', '.join(sorted(mandatory - pins.keys())))
        selected = list(pins)
    else:
        selected = ['python']
        if stage == 'fast':
            selected += ['actionlint', 'github:aviorstudio/gdlint']
        if components.get('rust'):
            selected.append('rust')
            if stage == 'security':
                selected.append('cargo:cargo-audit')
        active_packages = package_linters(config) if stage == 'fast' else [value for value in packages(config) if value.get('audit', True)]
        for value in active_packages:
            selected.append('bun' if value.get('manager', 'npm') == 'bun' else 'node')
            if (relative(root, value['directory']) / 'security-remediations.json').exists():
                selected.append('node')
    if any(name not in pins for name in selected):
        raise ValueError('required native SDK is not pinned in mise.toml')
    return sorted(set(selected))


def environment(root, config):
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    if os.environ.get('SOURCE_COMMIT', source) != source:
        raise ValueError('SOURCE_COMMIT must identify this exact checkout')
    values = {**sdk_environment.preserve_mise(), 'TOOLS_ROOT': str(TOOLS), 'SOURCE_COMMIT': source,
              'GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
              'GDRUST_GODOT_BIN': str(root / '.artifacts/godot/bin/godot'),
              'XDG_DATA_HOME': str(root / '.artifacts/godot-data'),
              'PLAYWRIGHT_BROWSERS_PATH': str(root / '.artifacts/playwright'),
              'RUNNER_TEMP': os.environ.get('RUNNER_TEMP', str(root / '.artifacts/tmp')),
              'PYTHONDONTWRITEBYTECODE': '1'}
    values['PATH'] = str(root / '.artifacts/godot/bin') + os.pathsep + os.environ.get('PATH', '')
    return values


def configure_environment(root, config):
    return environment(root, config)


def test_suites(root, config):
    if (not config.get('tests', {}).get('unit') and not config.get('components', {}).get('rust')
            and not any(value.get('test') or value.get('test_paths') for value in packages(config))):
        raise ValueError('Godot applications require real native unit checks')
    return ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if config.get('tests', {}).get(name)]]


def run(argv, root, env, timeout=None):
    subprocess.run(argv, cwd=root, env=env, check=True, timeout=timeout)


def setup(root, config, env):
    stage = os.environ.get('COOKBOOK_STAGE', 'full')
    selected = required_tools(root, config, stage)
    run(['mise', 'install', *selected], root, env)
    components = config.get('components', {})
    sdk = lambda argv, directory, environment: run(['mise', 'exec', '--', *argv], directory, environment)
    if components.get('python') and stage in {'fast', 'full', 'security'}:
        sdk(['python3', str(TOOLS / 'cookbook/python_component.py'), 'setup'], root, env)
    if stage == 'full' and godot_sdk.inputs(root, config) is not None:
        sdk(['python3', str(TOOLS / 'cookbook/godot_sdk.py'), 'setup'], root, env)
        env = godot_sdk.activate(root, config, env)
        os.environ.update(env)
    if components.get('rust'):
        if stage == 'full':
            rust_component.setup(root, components['rust'], env, sdk)
        elif stage == 'fast':
            pins = tomllib.loads((root / 'mise.toml').read_text())['tools']
            sdk(['rustup', 'component', 'add', '--toolchain', pins['rust'], 'rustfmt'], root, env)
    if stage in {'full', 'security', 'fast'}:
        node_component.install(root, package_linters(config) if stage == 'fast' else packages(config), env, sdk)
    if stage != 'full':
        return
    directory, settings = project(root, config)
    pins = godot_pins(root)
    (root / '.artifacts').mkdir(exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    (root / '.artifacts/tmp').mkdir(exist_ok=True)
    run([sys.executable, str(TOOLS / 'helpers/godot-setup.py'), '--version', pins['GODOT_VERSION'],
         '--expected-version', pins['GODOT_EXPECTED_VERSION'], '--binary-checksum', pins['GODOT_BINARY_CHECKSUM'],
         '--templates-checksum', pins['GODOT_TEMPLATES_CHECKSUM'], '--templates',
         '--root', str(root / '.artifacts/godot'), '--origin', settings.get('origin', 'godot')], root, env)
    if (directory / 'gdam.json').exists():
        helper('godot-addons').restore(directory)
    if settings.get('import_stage', 'setup') == 'setup':
        import_native(root, config, env)


def import_native(root, config, env):
    directory, settings = project(root, config)
    strict = helper('godot-game-runner')
    helper('godot-project').import_project(directory, env['GODOT_BIN'],
        lambda argv, timeout: strict.main(argv, timeout=timeout, environment=env),
        timeout=settings.get('import_timeout', 180), arguments=settings.get('import_arguments', []),
        ignored_patterns=settings.get('import_ignored_patterns', []))


def current_build(root, config, verify):
    declaration = verification_scope(root, config)
    receipt = json.loads((root / '.artifacts/verification/build.json').read_text())
    if receipt.get('identity') != verify.identity(declaration) or receipt.get('artifacts') != verify.artifacts(declaration):
        raise ValueError('build outputs are missing, changed or stale; run make build first')


def native_godot(root, config, entry, env):
    directory, settings = project(root, config)
    source = relative(root, entry['path'], exists=True)
    cwd = relative(root, entry.get('cwd', '.'), exists=True)
    argv = [env['GODOT_BIN'], '--headless', '--path', str(directory)]
    if source.suffix == '.gd':
        argv += ['--script', str(source)]
    else:
        argv.append(str(source))
    argv += entry.get('args', [])
    # The native helper receives explicit project paths; no user-owned task or
    # shell implementation is introduced by this metadata.
    result = helper('godot-game-runner').main(argv, timeout=entry.get('timeout', 180),
        sentinel=entry.get('sentinel'), forbidden=settings.get('forbidden_diagnostics', []), environment=env, cwd=cwd)
    if result:
        raise ValueError('native Godot assertion failed: ' + entry['path'])


def native_tests(root, config, env, suite):
    entries = config.get('tests', {}).get(suite, [])
    _, settings = project(root, config)
    godot_entries = [entry for entry in entries if entry['runner'] == 'godot-check']
    others = [entry for entry in entries if entry['runner'] != 'godot-check']
    if others:
        run_entries(root, {**config, 'tests': {suite: others}}, env, suite)
    if godot_entries:
        jobs = settings.get('test_jobs', 4)
        if type(jobs) is not int or not 1 <= jobs <= 16:
            raise ValueError('Godot test concurrency must be explicitly bounded')
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            list(executor.map(lambda entry: native_godot(root, config, entry, env), godot_entries))


def lint(root, config, env):
    directory, settings = project(root, config)
    if (root / '.github/workflows').is_dir():
        run(['actionlint'], root, env)
    run([sys.executable, str(TOOLS / 'helpers/documentation-policy.py')], root, env)
    run(['gdlint', *settings.get('lint_arguments', ['-warn'])], directory, env)
    node_component.lint(root, packages(config), env, run)
    components = config.get('components', {})
    if components.get('rust'):
        rust_component.lint(root, components['rust'], env, run)
    if config.get('tests', {}).get('lint'):
        native_tests(root, config, env, 'lint')


def security(root, config, env):
    command = [sys.executable, str(TOOLS / 'helpers/source-secrets.py')]
    if config.get('security', {}).get('fixtures'):
        command += ['--fixtures', str(relative(root, config['security']['fixtures'], exists=True))]
    run(command, root, env)
    for component in config.get('components', {}).get('rust', []):
        lock = str(Path(component['manifest']).with_name('Cargo.lock'))
        run(['cargo', 'audit', '--file', lock, '--deny', 'warnings'], root, env)
    node_component.audit(root, packages(config), env, run)
    if config.get('tests', {}).get('security'):
        native_tests(root, config, env, 'security')


def validate_outputs(root, config, env, compiler=False):
    _, settings = project(root, config)
    for export in settings.get('exports', []):
        output = relative(root, export['output'], exists=True)
        required = [output]
        if export.get('platform', 'web') == 'web':
            required += [output.with_suffix(suffix) for suffix in ['.wasm', '.pck', '.js']]
        for path in required:
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise ValueError('Godot Web export is missing a required payload: ' + str(path))
    components = config.get('components', {})
    if compiler and components.get('rust'):
        validate = getattr(rust_component, 'validate_build', None)
        if validate is None:
            raise ValueError('shared Cargo build validation is not integrated')
        validate(root, components['rust'], env, run)
    if compiler:
        for source in settings.get('art_sources', []):
            run(['bun', '--no-macros', str(TOOLS / 'helpers/godot-art-sources.mjs'), str(relative(root, source, exists=True))], root, env)
    helper('verification').artifacts(verification_scope(root, config))


def compile_outputs(root, config, env):
    compiler_environment = godot_sdk.activate(root, config, env)
    node_component.build(root, packages(config), env, run)
    directory, settings = project(root, config)
    if (directory / 'gdam.json').exists():
        helper('godot-addons').verify(directory)
    if config.get('components', {}).get('rust'):
        rust_component.build(root, config['components']['rust'], compiler_environment, run)
    # Emscripten owns a bundled Node for its compiler. Native app packaging must
    # keep the caller's Mise Node/Bun selection and its reviewed compression bytes.
    if compiler_environment.get('LIBCLANG_PATH'):
        env = {**env, 'LIBCLANG_PATH': compiler_environment['LIBCLANG_PATH']}
    os.environ.update(env)
    godot_package.extensions(root, config, env)
    godot_package.author(root, config, env, run, 'prepare')
    if settings.get('import_stage', 'setup') == 'build':
        import_native(root, config, env)
    exports = settings.get('exports', [])
    if not exports:
        raise ValueError('build requires at least one real native export')
    # The shared exporter performs strict isolated imports and preserves authored
    # input files. No product tests or dependency installation occur here.
    groups = {}
    for export in exports:
        native = relative(root, export.get('project', settings.get('project', 'app')), exists=True)
        arguments = tuple(export.get('import_arguments', settings.get('import_arguments', [])))
        key = (native, arguments, export.get('timeout', 600), export.get('import_timeout', settings.get('import_timeout', 180)))
        groups.setdefault(key, []).append((export['preset'], relative(root, export['output']), export.get('kind', 'release')))
    for (native, arguments, timeout, import_timeout), presets in groups.items():
        helper('godot-export').export(native, presets, binary=env['GODOT_BIN'],
            import_arguments=arguments, timeout=timeout, import_timeout=import_timeout)
    godot_package.runtime(root, config)
    godot_package.packs(root, config, env)
    godot_package.author(root, config, env, run, 'postprocess')
    godot_package.package(root, config, env)
    validate_outputs(root, config, env, compiler=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build', 'test',
        'test:lint', 'test:unit', 'test:integration', 'test:e2e', 'test:security', '_test', 'clean', 'dev', 'dev:stop', 'dev:logs'])
    parser.add_argument('--suite')
    args = parser.parse_args()
    root = Path.cwd().resolve()
    try:
        config = load(root)
        if config['recipe'] != 'godot-game':
            raise ValueError('this adapter requires the Godot application recipe')
        validate_config(root, config)
        env = environment(root, config)
        os.environ.update(env)
        if args.operation not in {'setup', 'clean', 'dev:stop', 'dev:logs'} and (root / 'mise.toml').is_file():
            pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
            if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
                raise ValueError('use the exact caller-pinned Python SDK; run make setup first')
            stage = 'fast' if args.operation == 'test:lint' or (args.operation == '_test' and args.suite == 'lint') else 'full'
            if args.operation == 'test:security' or (args.operation == '_test' and args.suite == 'security'):
                stage = 'security'
            selected = required_tools(root, config, stage)
            for name in selected:
                subprocess.run(['mise', 'where', name], cwd=root, env=env, check=True, stdout=subprocess.DEVNULL)
        verify = helper('verification')
        declaration = verification_scope(root, config)
        if args.operation == 'setup':
            setup(root, config, env)
        elif args.operation == 'fmt':
            tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
            sources = [str(root / name) for name in tracked if name.endswith('.gd') and '/addons/' not in name]
            if sources:
                run(['gdformat', *sources], root, env)
            node_component.format(root, packages(config), env, run)
            if config.get('components', {}).get('rust'):
                rust_component.format(root, config['components']['rust'], env, run)
        elif args.operation == 'clean':
            if config.get('dev'):
                with helper('dev').checkout_lease(root):
                    helper('clean').clean(root, config.get('clean', {}).get('paths', []))
            else:
                helper('clean').clean(root, config.get('clean', {}).get('paths', []))
        elif args.operation == '_build':
            compile_outputs(root, config, env)
        elif args.operation == 'build':
            verify.run_command('build', 'build', declaration, [sys.executable, str(Path(__file__).resolve()), '_build'], Path('.artifacts/verification'))
        elif args.operation == 'validate':
            current_build(root, config, verify)
            validate_outputs(root, config, env)
        elif args.operation == 'test':
            suites = test_suites(root, config)
            verify.run_session(suites, ['make', '--no-print-directory', '_cookbook-tests'], Path('.artifacts/verification'))
        elif args.operation == '_test':
            if args.suite == 'lint':
                lint(root, config, env)
            elif args.suite == 'security':
                security(root, config, env)
            else:
                if args.suite == 'unit':
                    node_component.test(root, packages(config), env, run)
                if args.suite == 'unit' and config.get('components', {}).get('rust'):
                    rust_component.test(root, config['components']['rust'], env, run)
                directory, _ = project(root, config)
                if (directory / 'gdam.json').exists():
                    helper('godot-addons').verify(directory)
                native_tests(root, config, env, args.suite)
        elif args.operation.startswith('test:'):
            suite = args.operation.split(':', 1)[1]
            if suite in {'integration', 'e2e'}:
                current_build(root, config, verify)
            verify.run_command('test', suite, declaration, [sys.executable, str(Path(__file__).resolve()), '_test', '--suite', suite], Path('.artifacts/verification'))
        else:
            directory, _ = project(root, config)
            state = config.get('dev', {}).get('state', '.local/dev')
            if not config.get('dev'):
                raise ValueError('development capability is not implemented')
            operation = {'dev': 'run', 'dev:stop': 'stop', 'dev:logs': 'logs'}[args.operation]
            command = [sys.executable, str(TOOLS / 'helpers/dev.py'), operation, '--state', state]
            if args.operation == 'dev':
                command += ['--log', config['dev'].get('log', '.artifacts/dev/game.log'), '--', env['GODOT_BIN']]
                if config['dev'].get('mode', 'editor') == 'editor':
                    command.append('--editor')
                command += ['--path', str(directory)]
            run(command, root, env)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
