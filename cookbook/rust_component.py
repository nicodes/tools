"""Typed Cargo operations shared by recipes; caller data never supplies commands."""
from pathlib import Path
import re
import tomllib

from configuration import relative

NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.+/-]*\Z')
TARGET = re.compile(r'[a-z0-9_]+(?:-[a-z0-9_]+){2,}\Z')
BASE_FIELDS = {'name', 'manifest', 'binaries', 'packages', 'features', 'all_features',
               'no_default_features', 'workspace', 'all_targets', 'release',
               'targets', 'target', 'target_directory', 'profile', 'test', 'build', 'lint', 'environment'}
SELECTORS = {'binaries', 'packages', 'features', 'all_features', 'no_default_features',
             'workspace', 'all_targets', 'release', 'profile', 'lib', 'tests', 'enabled',
             'test_targets', 'test_filter', 'test_arguments'}


def names(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not NAME.fullmatch(item) for item in value):
        raise ValueError(label + ' must be literal native Cargo identifiers')
    if len(set(value)) != len(value):
        raise ValueError(label + ' cannot contain duplicates')
    return value


def validate(component, root=None):
    if not isinstance(component, dict) or set(component) - BASE_FIELDS:
        raise ValueError('unsupported Cargo component data')
    manifest = component.get('manifest', 'Cargo.toml')
    if not isinstance(manifest, str) or not manifest.endswith('Cargo.toml'):
        raise ValueError('Cargo components require a native Cargo.toml manifest')
    if root is not None:
        relative(root, manifest, exists=True)
    elif Path(manifest).is_absolute() or '..' in Path(manifest).parts or '\\' in manifest:
        raise ValueError('Cargo manifest must stay inside the checkout')
    for declaration in [component, *[component.get(name, {}) for name in ['test', 'build', 'lint']]]:
        if not isinstance(declaration, dict):
            raise ValueError('Cargo selectors must be tables of native options')
        if declaration is not component and set(declaration) - SELECTORS:
            raise ValueError('unsupported Cargo operation selector')
        for field in ['binaries', 'packages', 'features', 'test_targets']:
            if field in declaration:
                names(declaration[field], field)
        for field in ['all_features', 'no_default_features', 'workspace', 'all_targets', 'release', 'lib', 'tests', 'enabled']:
            if field in declaration and type(declaration[field]) is not bool:
                raise ValueError('Cargo switch values must be booleans')
        if declaration.get('profile', 'release') not in {'release', 'debug', 'dev'}:
            raise ValueError('Cargo profile must be release or native development/debug')
        if declaration.get('all_features') and declaration.get('features'):
            raise ValueError('select all features or explicit features, not both')
        if 'test_filter' in declaration and (not isinstance(declaration['test_filter'], str)
                or not re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_:.+-]*', declaration['test_filter'])):
            raise ValueError('Cargo test filter must be a literal native test name')
        if 'test_arguments' in declaration:
            arguments = declaration['test_arguments']
            if not isinstance(arguments, list) or len(arguments) > 16:
                raise ValueError('Cargo test arguments must be bounded native harness options')
            for value in arguments:
                if value not in {'--exact', '--ignored', '--include-ignored', '--nocapture', '--show-output'} and not (
                        isinstance(value, str) and re.fullmatch(r'--test-threads=[1-9][0-9]{0,2}', value)):
                    raise ValueError('unsupported native Rust test-harness option')
    for target in component.get('targets', []):
        if not isinstance(target, str) or not TARGET.fullmatch(target):
            raise ValueError('invalid Rust SDK target')
    if component.get('target') and not TARGET.fullmatch(component['target']):
        raise ValueError('invalid Cargo component target')
    if component.get('target_directory'):
        directory = component['target_directory']
        if root is not None:
            relative(root, directory)
        elif not isinstance(directory, str) or Path(directory).is_absolute() or '..' in Path(directory).parts or '\\' in directory:
            raise ValueError('Cargo target directory must stay inside the checkout')
    environment = component.get('environment', {})
    if (not isinstance(environment, dict) or any(not re.fullmatch('[A-Z][A-Z0-9_]*', key)
            or not isinstance(value, str) or '\0' in value for key, value in environment.items())):
        raise ValueError('Cargo component environment must contain literal named values')
    return component


def cargo_args(component, operation, *, target=None):
    validate(component)
    commands = {'fetch': 'fetch', 'fmt': 'fmt', 'fmt-check': 'fmt', 'lint': 'clippy', 'unit': 'test', 'build': 'build'}
    if operation not in commands:
        raise ValueError('unsupported native Cargo operation')
    values = component | component.get({'unit': 'test', 'fmt-check': 'lint'}.get(operation, operation), {})
    if operation in {'unit', 'lint'} and 'binaries' not in component.get('test' if operation == 'unit' else 'lint', {}):
        # A package's distributable executables do not restrict its library,
        # workspace or integration-test coverage.
        values = {key: value for key, value in values.items() if key != 'binaries'}
    argv = ['cargo', commands[operation]]
    if operation not in {'fmt', 'fmt-check'}:
        argv.append('--locked')
        if operation != 'fetch':
            argv.append('--offline')
    argv += ['--manifest-path', component.get('manifest', 'Cargo.toml')]
    if operation in {'fmt', 'fmt-check'}:
        if values.get('workspace'):
            argv.append('--all')
        for package in values.get('packages', []):
            argv += ['--package', package]
        if operation == 'fmt-check':
            argv += ['--', '--check']
        return argv
    target = component.get('target', target)
    if target:
        if not TARGET.fullmatch(target):
            raise ValueError('Rust target must be a native target identifier')
        argv += ['--target', target]
    if operation == 'fetch':
        return argv
    if component.get('target_directory'):
        argv += ['--target-dir', component['target_directory']]
    if values.get('workspace'):
        argv.append('--workspace')
    if values.get('all_targets', operation == 'lint'):
        argv.append('--all-targets')
    for field, flag in [('packages', '--package'), ('binaries', '--bin')]:
        for value in values.get(field, []):
            argv += [flag, value]
    if values.get('all_features'):
        argv.append('--all-features')
    elif values.get('features'):
        argv += ['--features', ','.join(values['features'])]
    if values.get('no_default_features'):
        argv.append('--no-default-features')
    if operation == 'build':
        profile = values.get('profile', 'release' if values.get('release', True) else 'dev')
        if profile == 'release':
            argv.append('--release')
    if operation == 'unit':
        for field, flag in [('lib', '--lib'), ('tests', '--tests')]:
            if values.get(field):
                argv.append(flag)
        for name in values.get('test_targets', []):
            argv += ['--test', name]
        if values.get('test_filter'):
            argv.append(values['test_filter'])
        if values.get('test_arguments'):
            argv += ['--', *values['test_arguments']]
    if operation == 'lint':
        argv += ['--', '-D', 'warnings']
    return argv


def component_environment(component, environment):
    return {**environment, **component.get('environment', {})}


def invoke(root, components, environment, operation, run, *, target=None):
    if not components:
        raise ValueError('a Rust operation requires at least one real Cargo component')
    count = 0
    selector = {'unit': 'test', 'fmt-check': 'lint'}.get(operation, operation)
    for component in components:
        validate(component, root)
        if component.get(selector, {}).get('enabled') is False:
            continue
        run(cargo_args(component, operation, target=target), root,
            component_environment(component, environment))
        count += 1
    if not count:
        raise ValueError('the Cargo operation has no applicable native components')
    return count


def setup(root, components, environment, run):
    pins = tomllib.loads((Path(root) / 'mise.toml').read_text()).get('tools', {})
    version = pins.get('rust')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Rust SDK must be exactly pinned in mise.toml')
    run(['rustup', 'component', 'add', '--toolchain', version, 'rustfmt', 'clippy'], root, environment)
    targets = {target for component in components for target in validate(component, root).get('targets', [])}
    targets.update(component['target'] for component in components if component.get('target'))
    if environment.get('RUST_TARGET'):
        if not TARGET.fullmatch(environment['RUST_TARGET']):
            raise ValueError('invalid native CI Rust target')
        targets.add(environment['RUST_TARGET'])
    targets = sorted(targets)
    if targets:
        run(['rustup', 'target', 'add', '--toolchain', version, *targets], root, environment)
    invoke(root, components, environment, 'fetch', run, target=environment.get('RUST_TARGET'))


def format(root, components, environment, run):
    invoke(root, components, environment, 'fmt', run)


def lint(root, components, environment, run, *, target=None):
    invoke(root, components, environment, 'fmt-check', run)


def validate_build(root, components, environment, run, *, target=None):
    """Compiler-backed validation runs after required product assets exist."""
    invoke(root, components, environment, 'lint', run, target=target)


def test(root, components, environment, run, *, target=None):
    invoke(root, components, environment, 'unit', run, target=target)


def build(root, components, environment, run, *, target=None):
    invoke(root, components, environment, 'build', run, target=target)
