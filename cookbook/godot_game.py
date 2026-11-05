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
import rust_staging
import godot_static
import godot_package
import godot_sdk
import node_component
import images
import application_development

TOOLS = Path(__file__).resolve().parents[1]


def helper(name):
    directory = str(TOOLS / 'helpers')
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location('game_' + name.replace('-', '_'), TOOLS / 'helpers' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verification_scope(root, config):
    values = config.get('artifacts', {})
    if set(values) - {'paths', 'images', 'environment', 'revision_environment', 'definition', 'release_metadata'}:
        raise ValueError('unknown Godot build artifact selector')
    verification = helper('verification')
    if values.get('definition'):
        inputs = verification.release_definition(relative(root, values['definition'], exists=True))['inputs']
    else:
        inputs = {'artifacts': values.get('paths', []), 'images': values.get('images', []),
            'environment': values.get('environment', []), 'revision_environment': values.get('revision_environment', [])}
    inputs = {key: list(value) for key, value in inputs.items()}
    inputs['artifacts'] += values.get('release_metadata', []) + rust_staging.artifact_paths(rust_components(config))
    return verification.scope(**inputs)


def rust_components(config):
    return config.get('components', {}).get('rust', [])


def cargo_records(config, environment):
    records = []
    for component in rust_components(config):
        record = {key: value for key, value in component.items() if key not in {'output_directory', 'output_names'}}
        if any(key in {'APP_VERSION', 'SOURCE_COMMIT', 'CI', 'TOOLS_ROOT', 'GH_TOKEN', 'GITHUB_TOKEN',
                'TOOLS_VERIFICATION_SESSION', 'GOTOOLCHAIN', 'RUSTUP_AUTO_INSTALL'}
                or key.startswith(('GITHUB_', 'MISE_', 'COOKBOOK_', 'REQUEST_')) for key in record.get('environment', {})):
            raise ValueError('Rust component environment may not replace execution authority')
        record['environment'] = {key: value.replace('{revision}', environment.get('APP_VERSION', ''))
            for key, value in record.get('environment', {}).items()}
        records.append(record)
    return records


def build_rust(root, config, environment):
    records = cargo_records(config, environment)
    if records:
        rust_staging.compile_and_stage(root, rust_components(config), records, environment, run)


def artifact_paths(root, config):
    paths=list(verification_scope(root, config)['artifacts'])
    if config.get('components',{}).get('image_security'):
        paths.append(config['components']['image_security']['reports'])
    if config.get('components',{}).get('browser'):
        paths.append(config['components']['browser']['reports'])
    return paths


def evidence_paths(root, config):
    paths=[]
    if config.get('components',{}).get('image_security'):
        import godot_security
        paths.extend(godot_security.evidence_paths(root,config))
    if config.get('components',{}).get('browser'):
        reports=relative(root,config['components']['browser']['reports'],exists=True)
        paths.extend(path.relative_to(root).as_posix() for path in sorted(reports.rglob('*.json')))
        if len(paths)>2000:
            raise ValueError('native browser evidence exceeds its file bound')
    return paths


def project(root, config):
    values = config.get('components', {}).get('godot', {})
    directory = relative(root, values.get('project', 'app'), exists=True)
    if not (directory / 'project.godot').is_file():
        raise ValueError('Godot application component must own project.godot')
    return directory, values


def validate_config(root, config):
    directory, values = project(root, config)
    allowed = {'project', 'origin', 'exports', 'test_jobs', 'import_timeout', 'import_arguments',
               'import_ignored_patterns', 'addon_taint_markers', 'preserve_import_defaults', 'lint_arguments', 'forbidden_diagnostics', 'art_sources',
               'prepare', 'postprocess', 'packs', 'web_releases', 'templates', 'import_stage', 'rust_web', 'extensions'}
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
    if values.get('templates') is not None:
        import godot_template
        godot_template.declaration(root,config)
    components = config.get('components', {})
    for component in cargo_records(config, {'APP_VERSION': '0' * 40}):
        rust_component.validate(component, root)
    for path in rust_staging.artifact_paths(rust_components(config)):
        relative(root, path)
    godot_static.declaration(root,config)
    for package in packages(config):
        node_component.validate(package, root)
    godot_sdk.inputs(root, config)
    godot_package.validate(root, config, verification_scope(root, config)['artifacts'])
    for record in config.get('components', {}).get('static_images', []):
        if (not isinstance(record, dict) or set(record) - {'component', 'source', 'installed'}
                or not {'component', 'source'} <= set(record)):
            raise ValueError('static image contracts require component and source data')
        relative(root, record['source'])
        installed = record.get('installed', '/srv/public')
        if not isinstance(installed, str) or not installed.startswith('/') or '..' in Path(installed).parts or '\\' in installed:
            raise ValueError('static image contracts require literal installed paths')
    if components.get('browser'):
        import godot_browser
        godot_browser.declaration(root, config)
    if components.get('image_security'):
        import godot_security
        godot_security.declaration(root, config)
    if config.get('security',{}).get('auth_policy'):
        relative(root,config['security']['auth_policy'],exists=True)
    development=config.get('dev')
    if development is not None:
        if not isinstance(development,dict) or development.get('kind') not in {None,'native'}:
            raise ValueError('Godot development requires an editor or owned native application driver')
        if development.get('kind')=='native':
            if set(development)-{'kind','identity','project_prefix','program','cleanup','cleanup_compose','capture_output'}:
                raise ValueError('unsupported native application development setting')
            if type(development.get('capture_output',False)) is not bool:
                raise ValueError('development output capture must be explicit')
            relative(root,development.get('identity','ghenv.json'),exists=True)
            development_project(root,config)
            for entry in [development.get('program'),*([development['cleanup']] if development.get('cleanup') else [])]:
                application_development.native_entry(root,entry,{'APP_VERSION':'0'*40},long_running=True,
                    allowed_roots={'app','api','core','tests'})
            for name in development.get('cleanup_compose',[]):relative(root,name,exists=True)
        elif set(development)-{'mode','state','log'} or development.get('mode','editor') not in {'editor','run'}:
            raise ValueError('unsupported Godot editor session setting')
    for name,entry in config.get('database',{}).items():
        if name not in {'status','migrate','seed','reset'} or config.get('dev',{}).get('kind')!='native':
            raise ValueError('database operations require an implemented native application lifecycle')
        application_development.native_entry(root,entry,{'APP_VERSION':'0'*40},allowed_roots={'app','api','core','tests'})
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
    if stage in {'full','local'}:
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
    if config.get('security',{}).get('auth_policy') and stage in {'full','security'}:
        selected.append('bun')
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
    values['APP_VERSION'] = source
    if config.get('components', {}).get('images'):
        definition = images.definition(root, config)
        for key, expected in [('IMAGE_BASE', definition['image_base']), ('GATE_IMAGE', definition['image_base'] + '-gate:' + source)]:
            if os.environ.get(key, expected) != expected:
                raise ValueError('image selection differs from the caller release authority: ' + key)
            values[key] = expected
    return values


def configure_environment(root, config):
    return environment(root, config)



def development_api():
    from types import SimpleNamespace
    return SimpleNamespace(**{name:globals()[name] for name in [
        'run','native','select_port','pinned_ghenv','development_project',
        'isolated_operation','stop_services','helper']})


def native(root, entries, environment, *, sdk=False, long_running=False):
    return application_development.native(root,entries,environment,api=development_api(),
        sdk=sdk,long_running=long_running,allowed_roots={'app','api','core','tests'},
        protected_environment={'SOURCE_COMMIT','APP_VERSION','CI','TOOLS_ROOT','GH_TOKEN','GITHUB_TOKEN','TOOLS_VERIFICATION_SESSION','GOTOOLCHAIN','RUSTUP_AUTO_INSTALL'})
def development_project(root, config=None):
    return application_development.development_project(root, config)

def select_port(preferred, attempts=100):
    return application_development.select_port(preferred, attempts)

def pinned_ghenv(root, environment):
    return application_development.pinned_ghenv(root, environment)

def launch_development(root, config, environment):
    return application_development.launch_development(root, config, environment, api=development_api())

def clean_outputs(root, config, environment):
    return application_development.clean_outputs(root, config, environment, api=development_api())

def stop_services(root, config, environment):
    return application_development.stop_services(root, config, environment, api=development_api())

def isolated_operation(root, config, environment, operation):
    return application_development.isolated_operation(root, config, environment, operation, api=development_api())

def development(root, config, environment, operation):
    return application_development.development(root, config, environment, operation, api=development_api())

def database(root, config, environment, operation):
    return application_development.database(root, config, environment, operation, api=development_api())


def check_development_sdks(root, config, environment, operation, suite=None):
    pins=tomllib.loads((root/'mise.toml').read_text()).get('tools',{})
    if pins.get('python')!='.'.join(map(str,sys.version_info[:3])):
        raise ValueError('use the exact caller-pinned Python SDK; run make setup first')
    for name in required_tools(root,config,'local'):
        run(['mise','where',name],root,environment)

def test_suites(root, config):
    if (not config.get('tests', {}).get('unit') and not config.get('components', {}).get('rust')
            and not any(value.get('test') or value.get('test_paths') for value in packages(config))):
        raise ValueError('Godot applications require real native unit checks')
    return ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if config.get('tests', {}).get(name)]]


def run(argv, root, env, timeout=None):
    subprocess.run(argv, cwd=root, env=env, check=True, timeout=timeout)


def setup(root, config, env):
    if env.get('COOKBOOK_GODOT_HOST'):
        import godot_ci
        matrix = godot_ci.host_matrix(root, config, env)
        entry = next(value for value in matrix['requirements'] if value['kind'] == 'host')
        godot_ci.setup_worker(root, config, env, entry)
        return
    requested_stage = os.environ.get('COOKBOOK_STAGE', 'full')
    stage = 'full' if requested_stage == 'local' else requested_stage
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
            rust_component.setup(root, cargo_records(config, env), env, sdk)
        elif stage == 'fast':
            pins = tomllib.loads((root / 'mise.toml').read_text())['tools']
            sdk(['rustup', 'component', 'add', '--toolchain', pins['rust'], 'rustfmt'], root, env)
    if stage in {'full', 'security', 'fast'}:
        node_component.install(root, package_linters(config) if stage == 'fast' else packages(config), env, sdk)
    if components.get('image_security') and stage in {'full','security'} and env.get('COOKBOOK_GODOT_ENTRY')!='build' and requested_stage!='local':
        import godot_security
        godot_security.setup(root,config,env,run)
    if stage != 'full':
        return
    if components.get('browser') and not env.get('COOKBOOK_GODOT_ENTRY') and requested_stage!='local':
        import godot_browser
        godot_browser.setup(root, config, env)
    directory, settings = project(root, config)
    pins = godot_pins(root)
    (root / '.artifacts').mkdir(exist_ok=True)
    (root / '.artifacts/.gdignore').touch()
    (root / '.artifacts/tmp').mkdir(exist_ok=True)
    run([sys.executable, str(TOOLS / 'helpers/godot-setup.py'), '--version', pins['GODOT_VERSION'],
         '--expected-version', pins['GODOT_EXPECTED_VERSION'], '--binary-checksum', pins['GODOT_BINARY_CHECKSUM'],
         '--templates-checksum', pins['GODOT_TEMPLATES_CHECKSUM'], '--templates',
         '--root', str(root / '.artifacts/godot'), '--origin', settings.get('origin', 'godot')], root, env)
    if settings.get('templates') is not None:
        import godot_template
        godot_template.setup(root,config,env)
    if requested_stage != 'local':
        godot_static.setup(root,config,env,run)
    if (directory / 'gdam.json').exists():
        helper('godot-addons').restore(directory, taint_markers=settings.get('addon_taint_markers', []))
    if settings.get('import_stage', 'setup') == 'setup':
        import_native(root, config, env)


def import_native(root, config, env):
    directory, settings = project(root, config)
    strict = helper('godot-game-runner')
    helper('godot-project').import_project(directory, env['GODOT_BIN'],
        lambda argv, timeout: strict.main(argv, timeout=timeout, environment=env),
        timeout=settings.get('import_timeout', 180), arguments=settings.get('import_arguments', []),
        ignored_patterns=settings.get('import_ignored_patterns', []), preserve_defaults=settings.get('preserve_import_defaults', False))


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
    import godot_ci
    entries = godot_ci.selected_entries(root, config, env, suite)
    _, settings = project(root, config)
    godot_entries = [entry for entry in entries if entry['runner'] == 'godot-check']
    others = [entry for entry in entries if entry['runner'] != 'godot-check']
    if others:
        import godot_browser
        custom = {'godot-browser': lambda checkout, entry, environment: godot_browser.journeys(checkout, config, environment, entry)}
        run_entries(root, {**config, 'tests': {suite: others}}, env, suite, custom_runners=custom)
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
        rust_component.lint(root, cargo_records(config, env), env, run)
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
    if config.get('security',{}).get('auth_policy'):
        run(['bun',str(TOOLS/'helpers/auth-boundary.mjs'),'--policy',config['security']['auth_policy']],root,env)
    if config.get('components',{}).get('image_security'):
        import godot_security
        godot_security.scan(root,config,env,run)
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
        validate(root, cargo_records(config, env), env, run)
    if compiler:
        for source in settings.get('art_sources', []):
            run(['bun', '--no-macros', str(TOOLS / 'helpers/godot-art-sources.mjs'), str(relative(root, source, exists=True))], root, env)
    if components.get('images'):
        images.validate_release(root, config, env)
        if compiler:
            definition = images.definition(root, config)
            for record in components.get('static_images', []):
                if record['component'] not in images.components(definition):
                    raise ValueError('static image contract must name a declared shipping component')
                images.static_image(definition['image_base'] + '-' + record['component'] + ':' + env['APP_VERSION'],
                    relative(root, record['source'], exists=True), record.get('installed', '/srv/public'))
    godot_static.verify(root, config, env, run)
    helper('verification').artifacts(verification_scope(root, config))


def compile_outputs(root, config, env):
    compiler_environment = godot_sdk.activate(root, config, env)
    node_component.build(root, packages(config), env, run)
    directory, settings = project(root, config)
    if (directory / 'gdam.json').exists():
        helper('godot-addons').verify(directory)
    build_rust(root, config, compiler_environment)
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
    if settings.get('templates') is not None:
        import godot_template
        godot_template.verify(root,config,env)
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
    if config.get('components', {}).get('images'):
        if config.get('components', {}).get('compose'):
            images.validate_compose(root, config, env, run)
        images.build(root, config, env, run)
        godot_static.stage(root, config, env, run)
        images.package(root, config, env, run)
    validate_outputs(root, config, env, compiler=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['setup', 'fmt', 'build', 'validate', '_build', 'test',
        'test:lint', 'test:unit', 'test:integration', 'test:e2e', 'test:security', '_test', 'clean', 'dev', 'dev:stop', 'dev:logs', '_dev', '_stop-services', '_clean', '_database',
        'db:status','db:migrate','db:seed','db:reset'])
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
        if args.operation not in {'setup', 'clean', '_clean', 'dev:stop', 'dev:logs', '_stop-services'} and (root / 'mise.toml').is_file():
            pins = tomllib.loads((root / 'mise.toml').read_text()).get('tools', {})
            if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
                raise ValueError('use the exact caller-pinned Python SDK; run make setup first')
            stage = 'fast' if args.operation == 'test:lint' or (args.operation == '_test' and args.suite == 'lint') else 'full'
            if args.operation == 'test:security' or (args.operation == '_test' and args.suite == 'security'):
                stage = 'security'
            if env.get('COOKBOOK_GODOT_ENTRY') or env.get('COOKBOOK_GODOT_HOST'):
                import godot_ci
                selected = godot_ci.worker_tools(root, config, env)
            else:
                selected = required_tools(root, config, 'local' if args.operation in {'dev','_dev','_database','db:status','db:migrate','db:seed','db:reset'} else stage)
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
                rust_component.format(root, cargo_records(config, env), env, run)
        elif args.operation == 'clean':
            if config.get('dev',{}).get('kind')=='native':
                run([sys.executable,str(TOOLS/'helpers/dev.py'),'clean','--state','.local/dev','--',
                     *isolated_operation(root,config,env,'_clean')],root,env)
            elif config.get('dev'):
                with helper('dev').checkout_lease(root):
                    helper('clean').clean(root, config.get('clean', {}).get('paths', []))
            else:
                helper('clean').clean(root, config.get('clean', {}).get('paths', []))
        elif args.operation == '_clean':
            clean_outputs(root,config,env)
        elif args.operation == '_dev':
            launch_development(root,config,env)
        elif args.operation == '_stop-services':
            stop_services(root,config,env)
        elif args.operation == '_database':
            entry=config.get('database',{}).get(args.suite)
            if not isinstance(entry,dict):raise ValueError('database operation is not implemented')
            native(root,[entry],env)
        elif args.operation.startswith('db:'):
            database(root,config,env,args.operation)
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
            if env.get('COOKBOOK_GODOT_ENTRY'):
                native_tests(root, config, env, args.suite)
            elif args.suite == 'lint':
                lint(root, config, env)
            elif args.suite == 'security':
                security(root, config, env)
            else:
                if args.suite == 'unit':
                    node_component.test(root, packages(config), env, run)
                if args.suite == 'unit' and config.get('components', {}).get('rust'):
                    rust_component.test(root, cargo_records(config, env), env, run)
                directory, _ = project(root, config)
                if (directory / 'gdam.json').exists():
                    helper('godot-addons').verify(directory)
                native_tests(root, config, env, args.suite)
            if env.get('COOKBOOK_GODOT_HOST'):
                import godot_ci
                godot_ci.require_group(root, config, env, args.suite)
        elif args.operation.startswith('test:'):
            suite = args.operation.split(':', 1)[1]
            if suite in {'integration', 'e2e'}:
                current_build(root, config, verify)
            verify.run_command('test', suite, declaration, [sys.executable, str(Path(__file__).resolve()), '_test', '--suite', suite], Path('.artifacts/verification'))
        elif config.get('dev',{}).get('kind')=='native':
            development(root,config,env,args.operation)
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
