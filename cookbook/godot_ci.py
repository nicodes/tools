"""Derive Godot job topology and verify complete, disjoint native suite selections."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re

from configuration import relative

RUNNERS = {'ubuntu-22.04', 'ubuntu-24.04', 'blacksmith-4vcpu-ubuntu-2204', 'blacksmith-4vcpu-ubuntu-2404'}
NAME = re.compile(r'(?:test-|suite-)[a-z0-9_-]{1,120}\Z')
SHA = re.compile(r'[a-f0-9]{40}\Z')
GROUPS = {'lint', 'unit', 'security', 'integration', 'e2e'}


def partition(targets, shard):
    if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)):
        raise ValueError('suite inventory must be nonempty and unique')
    if any(not isinstance(value, str) or not NAME.fullmatch(value) for value in targets):
        raise ValueError('invalid named native suite identity')
    if not isinstance(shard, str) or not re.fullmatch(r'[1-9][0-9]*/[1-9][0-9]*', shard):
        raise ValueError('shard must be INDEX/COUNT')
    index, count = map(int, shard.split('/'))
    if index > count or count > len(targets):
        raise ValueError('invalid or empty native test shard')
    return sorted(targets)[index - 1::count]


def run_cases(cases, jobs, execute):
    if type(jobs) is not int or not 1 <= jobs <= 4:
        raise ValueError('native per-runner workers must stay between one and four')
    partition(list(cases), '1/1')
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda name: execute(cases[name]), sorted(cases)))
    return next((value for value in results if value), 0)


def verify_selections(directory, source, targets, count, run_id, attempt):
    if not isinstance(source, str) or not SHA.fullmatch(source) or type(count) is not int or count < 1:
        raise ValueError('exact source and a positive shard count are required')
    inventory = partition(targets, '1/1')
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('test evidence requires an unlinked directory')
    paths = list(directory.rglob('godot-test-selection.json'))
    if len(paths) != count:
        raise ValueError('complete native shard evidence is required')
    seen = set()
    for path in paths:
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 1000000
                or any(parent.is_symlink() for parent in path.parents)):
            raise ValueError('native selection evidence requires a bounded regular file')
        row = json.loads(path.read_text())
        shard = row.get('shard')
        if shard in seen or shard not in {f'{number}/{count}' for number in range(1, count + 1)}:
            raise ValueError('unexpected or repeated native shard')
        seen.add(shard)
        if (row.get('source_commit') != source or row.get('run_id') != run_id
                or row.get('attempt') != attempt or row.get('status') != 'passed'
                or row.get('workers') != 4 or row.get('inventory') != inventory
                or row.get('selected') != partition(targets, shard)):
            raise ValueError('native selections differ from the current source, execution or full inventory')
    return {'source_commit': source, 'run_id': run_id, 'attempt': attempt,
            'shards': count, 'suite_targets': len(inventory), 'status': 'passed'}


def case_id(suite, entry):
    wire = json.dumps({'suite': suite, 'entry': entry}, sort_keys=True, separators=(',', ':'))
    return 'case-' + hashlib.sha256(wire.encode()).hexdigest()


def unit_groups(config):
    groups = {}
    executions = set()
    for entry in config.get('tests', {}).get('unit', []):
        if entry['runner'] not in {'godot', 'godot-check'}:
            continue
        name = entry.get('name', 'test-' + hashlib.sha256(entry['path'].encode()).hexdigest()[:24])
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('Godot shard entries require bounded literal native suite identities')
        signature = json.dumps({key: value for key, value in entry.items() if key != 'name'}, sort_keys=True)
        if signature in executions:
            raise ValueError('native Godot coverage repeats the same written assertion execution')
        executions.add(signature)
        groups.setdefault(name, []).append(case_id('unit', entry))
    if any(len(values) != len(set(values)) for values in groups.values()):
        raise ValueError('native Godot coverage repeats the same written assertion entry')
    return groups


def budget(value):
    if type(value) is not int or not 1 <= value <= 120:
        raise ValueError('Godot job deadlines must be bounded source integers')
    return value


def plan(root, config):
    if config.get('recipe') != 'godot-game':
        raise ValueError('Godot topology requires the native game recipe')
    values = config.get('ci', {}).get('godot')
    if values is None:
        return {'version': 1, 'enrolled': False, 'requirements': [], 'units': {'include': []}, 'gates': {'include': []}}
    if (not isinstance(values, dict) or set(values) - {'unit_shards', 'unit_workers', 'build_timeout_minutes',
            'unit_timeout_minutes', 'host_timeout_minutes', 'unit_artifacts', 'gates', 'host_unit_name'}):
        raise ValueError('unsupported Godot CI topology declaration')
    runner = config.get('ci', {}).get('runner', 'ubuntu-24.04')
    if runner not in RUNNERS:
        raise ValueError('Godot runners must use the reviewed native Linux platforms')
    workers = values.get('unit_workers', 4)
    if workers != 4:
        raise ValueError('accepted parallel Godot coverage requires four workers')
    groups = unit_groups(config)
    native_unit = (any(value['runner'] not in {'godot', 'godot-check'} for value in config.get('tests', {}).get('unit', []))
        or any(value.get('test', {}).get('enabled', True) for value in config.get('components', {}).get('rust', []))
        or any(isinstance(value, dict) and (value.get('test') or value.get('test_paths')) for value in config.get('node', {}).get('packages', [])))
    native_name = values.get('host_unit_name', 'test-native') if native_unit else None
    if native_name is not None and (not isinstance(native_name, str) or not NAME.fullmatch(native_name) or native_name in groups):
        raise ValueError('native host unit coverage requires one distinct literal group identity')
    if not native_unit and values.get('host_unit_name'):
        raise ValueError('native host unit identity cannot name absent assertions')
    shards = values.get('unit_shards', 1 if groups else 0)
    if type(shards) is not int or not 0 <= shards <= 8 or (groups and not 1 <= shards <= len(groups)) or (not groups and shards):
        raise ValueError('Godot shard count must cover nonempty actual native groups')
    artifacts = values.get('unit_artifacts', [])
    if not isinstance(artifacts, list) or len(artifacts) != len(set(artifacts)):
        raise ValueError('Godot unit inputs require distinct declared artifact paths')
    for path in artifacts:
        if root is not None:
            relative(root, path)
        elif not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts or '\\' in path:
            raise ValueError('Godot unit artifacts must stay inside the checkout')
    declared = {case_id(suite, entry): (suite, entry) for suite, entries in config.get('tests', {}).items() for entry in entries}
    if len(declared) != sum(len(entries) for entries in config.get('tests', {}).values()):
        raise ValueError('Godot coverage repeats an identical native assertion entry')
    requirements = [{'id': 'build', 'kind': 'build', 'job': 'Godot build', 'runner': runner,
        'timeout_minutes': budget(values.get('build_timeout_minutes', 30))}]
    for number in range(1, shards + 1):
        selected = partition(list(groups), f'{number}/{shards}')
        requirements.append({'id': f'unit-{number}-{shards}', 'kind': 'unit', 'job': f'Godot unit ({number}/{shards})',
            'runner': runner, 'timeout_minutes': budget(values.get('unit_timeout_minutes', 20)),
            'shard': f'{number}/{shards}', 'workers': workers, 'groups': selected,
            'cases': sorted(case for name in selected for case in groups[name]), 'artifacts': artifacts})
    gate_records = values.get('gates', [])
    if not isinstance(gate_records, list) or len(gate_records) > 16:
        raise ValueError('Godot auxiliary coverage requires bounded source records')
    assigned = set(case for value in requirements for case in value.get('cases', []))
    names = set()
    production = []
    for record in gate_records:
        allowed = {'name', 'runner', 'timeout_minutes', 'suite', 'tests', 'kind', 'entry', 'setup', 'packages', 'artifacts'}
        if not isinstance(record, dict) or set(record) - allowed or not {'name', 'runner', 'timeout_minutes'} <= set(record):
            raise ValueError('invalid native auxiliary gate')
        name, kind = record['name'], record.get('kind', 'test')
        if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{1,63}', name)
                or name in names or record['runner'] not in RUNNERS or kind not in {'test', 'capability', 'production'}):
            raise ValueError('auxiliary gates require distinct names, native runners and coverage roles')
        names.add(name)
        setup = record.get('setup', 'native')
        if setup not in {'native', 'godot', 'browser'}:
            raise ValueError('native gate setup must select a shared SDK capability')
        selected = record.get('tests', [])
        cases = []
        suite = record.get('suite', 'security' if kind == 'capability' else 'e2e')
        if suite not in GROUPS:
            raise ValueError('native gate must select an actual test group')
        if kind == 'test':
            if 'entry' in record or not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
                raise ValueError('product auxiliary gates select distinct existing written assertions')
            for selected_name in selected:
                found = [key for key, (group, entry) in declared.items()
                         if group == suite and entry.get('name', entry['path']) == selected_name]
                if len(found) != 1 or found[0] in assigned:
                    raise ValueError('auxiliary coverage must name one previously unassigned native assertion')
                cases += found
            assigned.update(cases)
        else:
            entry = record.get('entry')
            if (selected or not isinstance(entry, dict) or set(entry) - {'runner', 'path', 'args', 'timeout', 'name', 'python_environment'}
                    or entry.get('runner') not in {'python', 'node', 'bun', 'bash'}):
                raise ValueError('platform and production gates require one real owned native assertion program')
            path = Path(entry.get('path', ''))
            if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in str(path)
                    or not any(part in {'tests', 'test'} for part in path.parts)):
                raise ValueError('platform and production assertions belong to owned tests')
            if root is not None and not relative(root, path.as_posix(), exists=True).is_file():
                raise ValueError('native assertion entry must be a source program')
            arguments = entry.get('args', [])
            if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
                raise ValueError('native assertion arguments must be literal strings')
            timeout = entry.get('timeout', 120 if kind == 'capability' else 360)
            if type(timeout) is not int or not 1 <= timeout <= 3600:
                raise ValueError('native assertions require bounded original deadlines')
            cases = [case_id(suite, entry)]
        inputs = record.get('artifacts', [])
        packages = record.get('packages', [])
        if not isinstance(inputs, list) or not isinstance(packages, list):
            raise ValueError('native gate inputs and packages must be literal native paths')
        for path in [*inputs, *packages]:
            if not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts or '\\' in path:
                raise ValueError('native gate dependencies must stay inside the checkout')
        known_packages = {value if isinstance(value, str) else value['directory'] for value in config.get('node', {}).get('packages', [])}
        if not set(packages) <= known_packages:
            raise ValueError('native gate packages must select declared locked Node components')
        value = {'id': 'gate-' + name, 'kind': kind, 'job': 'Godot gate (' + name + ')',
            'runner': record['runner'], 'timeout_minutes': budget(record['timeout_minutes']),
            'suite': suite, 'cases': sorted(cases), 'setup': setup, 'packages': packages, 'artifacts': inputs}
        if kind != 'test':
            value['entry'] = entry
        (production if kind == 'production' else requirements).append(value)
    host_cases = sorted(set(declared) - assigned)
    host_groups = ['lint', 'unit', 'security', *[name for name in ['integration', 'e2e'] if config.get('tests', {}).get(name)]]
    requirements.append({'id': 'host', 'kind': 'host', 'job': 'Godot host', 'runner': runner,
        'timeout_minutes': budget(values.get('host_timeout_minutes', config.get('ci', {}).get('timeout_minutes', 30))),
        'groups': host_groups, 'cases': host_cases, 'native_unit_name': native_name})
    result = {'version': 1, 'enrolled': True, 'requirements': requirements, 'unit_inventory': sorted([*groups, *([native_name] if native_name else [])]),
        'godot_unit_inventory': sorted(groups),
        'host_groups': host_groups, 'written_case_inventory': sorted(declared),
        'units': {'include': [row for row in requirements if row['kind'] == 'unit']},
        'gates': {'include': [row for row in requirements if row['kind'] in {'test', 'capability'}]},
        'production': {'include': production}}
    if len(json.dumps(result)) > 200000:
        raise ValueError('Godot topology exceeds the bounded Actions matrix output')
    return result
