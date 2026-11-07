"""Pure future-Mise identity decoding for the installed merge controller.

The selected final SDK verifies exact task bytes during native validation.
This transition module inspects bounded source data and never executes tasks.
"""
import hashlib
import importlib.util
from pathlib import Path
import re
import tomllib

from configuration import validate_data
import orchestration

MAX_SOURCE = 262144
START = '# BEGIN GENERATED COOKBOOK TASKS v1\n'
END = '# END GENERATED COOKBOOK TASKS v1\n'
RECIPES = {'rust-cli', 'react-native', 'godot-game', 'godot-addon'}
SUITES = ['lint', 'unit', 'security', 'integration', 'e2e']
DATABASE = ['status', 'migrate', 'seed', 'reset']
BASE_TASKS = {'_sdk', 'help', 'setup', 'fmt', 'build', 'test', 'clean'}


def discovery():
    spec = importlib.util.spec_from_file_location('cookbook_sdk_identity',
        Path(__file__).resolve().parents[1] / 'helpers/sdk_identity.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(source):
    if isinstance(source, bytes):
        source = source.decode('utf-8')
    if not isinstance(source, str) or len(source.encode()) > MAX_SOURCE or '\0' in source:
        raise ValueError('Mise identity requires bounded UTF-8 source')
    return discovery().universal_text(source.encode())


def declared(source):
    # Any authority beyond the existing SDK/environment prefix must satisfy
    # the new identity. It cannot be hidden behind a still-present Makefile.
    return bool(set(tomllib.loads(text(source))) - {'tools', 'env'})


def decode(source):
    source = text(source)
    if source.count(START) != 1 or source.count(END) != 1 or not source.endswith(END):
        raise ValueError('Mise source requires one complete versioned cookbook task block')
    prefix, suffix = source.split(START)
    if not prefix.endswith('\n') or END in prefix or suffix.count(END) != 1:
        raise ValueError('Mise task identity must be the complete generated source suffix')
    if set(tomllib.loads(prefix)) - {'tools', 'env'}:
        raise ValueError('Mise source has another task or identity authority')
    data = tomllib.loads(source)
    if set(data) - {'tools', 'env', 'vars', 'settings', 'tasks'}:
        raise ValueError('Mise recipe has unsupported task discovery or control fields')
    values = data.get('vars')
    fields = {'tools_contract', 'tools_ref', 'cookbook_recipe', 'cookbook_sha256'}
    record = discovery().identity(source)
    if (set(values) != fields or not isinstance(values.get('cookbook_recipe'), str)
            or values['cookbook_recipe'] not in RECIPES
            or not isinstance(values.get('cookbook_sha256'), str)
            or not re.fullmatch('[a-f0-9]{64}', values['cookbook_sha256'])):
        raise ValueError('Mise identity requires the closed recipe and raw source hash')
    settings = data.get('settings')
    if (not isinstance(settings, dict) or set(settings) != {'task'}
            or not isinstance(settings['task'], dict) or set(settings['task']) != {'run_auto_install'}
            or settings['task']['run_auto_install'] is not False):
        raise ValueError('Mise recipe must disable implicit tool installation')
    tasks = data.get('tasks')
    allowed = BASE_TASKS | {'test:' + suite for suite in SUITES} | {'dev', 'dev:stop', 'dev:logs'} | {'db:' + value for value in DATABASE}
    if not isinstance(tasks, dict) or set(tasks) - allowed or not BASE_TASKS <= set(tasks):
        raise ValueError('Mise identity requires only implemented standard task names')
    groups = [suite for suite in SUITES if 'test:' + suite in tasks]
    if not {'lint', 'unit', 'security'} <= set(groups) or any(not isinstance(task, dict) for task in tasks.values()):
        raise ValueError('Mise identity must retain all mandatory native groups')
    dev = set(tasks) & {'dev', 'dev:stop', 'dev:logs'}
    if dev and dev != {'dev', 'dev:stop', 'dev:logs'}:
        raise ValueError('Mise development capability requires its complete lifecycle')
    capabilities = (['dev'] if dev else []) + ['db:' + name for name in DATABASE if 'db:' + name in tasks]
    return {**record, 'recipe': values['cookbook_recipe'],
            'configuration_sha256': values['cookbook_sha256'], 'test_groups': sorted(groups),
            'capabilities': capabilities}


def matches(record, declaration, source):
    expected = orchestration.contract(declaration)
    capabilities = (['dev'] if declaration.get('dev') else []) + [
        'db:' + name for name in DATABASE if name in declaration.get('database', {})]
    if (record['recipe'] != expected['recipe'] or record['test_groups'] != expected['test_groups']
            or record['configuration_sha256'] != hashlib.sha256(source).hexdigest()
            or record['capabilities'] != capabilities):
        raise ValueError('Mise identity differs from the exact native recipe declaration')
    return record


def read(root):
    root = Path(root)
    module = discovery()
    source = module.read_regular(root / 'cookbook.toml')
    declaration = validate_data(tomllib.loads(source.decode('utf-8')))
    mise = module.read_source(root)
    if declared(mise):
        return matches(decode(mise), declaration, source)
    record = module.source_identity(root)
    model = orchestration.contract(declaration)
    return {**record, 'recipe': model['recipe'], 'test_groups': model['test_groups'],
            'configuration_sha256': hashlib.sha256(source).hexdigest(),
            'capabilities': (['dev'] if declaration.get('dev') else []) + [
                'db:' + name for name in DATABASE if name in declaration.get('database', {})]}
