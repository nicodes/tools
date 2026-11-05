"""Read bounded recipe data; product code and test assertions remain caller-owned."""
from pathlib import Path
import re
import tomllib

RECIPES = {'rust-cli', 'react-native', 'godot-game', 'godot-addon', 'native'}
SUITES = {'lint', 'unit', 'integration', 'e2e', 'security'}
RUNNERS = {'python', 'python-unittest', 'node', 'node-test', 'bun', 'bun-test',
           'bash', 'godot', 'godot-suite', 'godot-check', 'playwright'}


def ci_settings(data):
    values = data.get('ci', {})
    if not isinstance(values, dict) or set(values)-{'runner', 'timeout_minutes', 'godot', 'preview_environment'}:
        raise ValueError('CI settings require literal runner and existing bounded job budget')
    if 'godot' in values and (data.get('recipe') != 'godot-game' or not isinstance(values['godot'], dict)):
        raise ValueError('Godot CI metadata belongs to its application recipe')
    preview = values.get('preview_environment', '')
    if preview and (data.get('recipe') != 'react-native' or preview != 'Preview'):
        raise ValueError('only the reviewed React Native Preview environment may bind candidate builds')
    if not isinstance(preview, str):
        raise ValueError('preview environment must be a literal name')
    result = {'runner': 'ubuntu-24.04', 'timeout_minutes': 30, **{key: value for key, value in values.items() if key not in {'godot', 'preview_environment'}}}
    if preview:
        result['preview_environment'] = preview
    if (not isinstance(result['runner'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', result['runner'])
            or type(result['timeout_minutes']) is not int or not 1 <= result['timeout_minutes'] <= 180):
        raise ValueError('CI runner and job budget must be bounded source data')
    return result


def relative(root, name, *, exists=False):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(ord(character) < 32 for character in name)):
        raise ValueError('recipe paths must be nonempty relative paths')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or '.git' in path.parts:
        raise ValueError('recipe paths must stay inside the checkout')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('recipe path escapes the checkout')
    if any(p.is_symlink() for p in [target, *target.parents] if p != root and p.is_relative_to(root)):
        raise ValueError('recipe input/output paths may not be symlinks')
    if exists and not target.exists():
        raise ValueError('recipe input is missing: ' + name)
    return target


def load(root):
    root = Path(root).resolve()
    path = root / 'cookbook.toml'
    if path.is_symlink() or path.stat().st_size > 262144:
        raise ValueError('recipe configuration must be a bounded regular source file')
    data = tomllib.loads(path.read_text())
    allowed = {'version', 'recipe', 'package', 'tests', 'lint', 'format', 'clean',
               'dev', 'database', 'components', 'node', 'security', 'artifacts', 'publication', 'ci'}
    if set(data) - allowed or data.get('version') != 1 or data.get('recipe') not in RECIPES:
        raise ValueError('unsupported cookbook configuration')
    if 'tasks' in data or 'commands' in data:
        raise ValueError('recipe data cannot define another task implementation')
    ci_settings(data)
    tests = data.get('tests', {})
    if set(tests) - SUITES:
        raise ValueError('unknown test suite')
    for suite, entries in tests.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError('declared test groups must contain real checks')
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {'runner', 'path', 'args', 'timeout', 'sentinel', 'cwd', 'pattern', 'policy', 'python_environment', 'name'}:
                raise ValueError('invalid native test entry')
            if 'name' in entry and (not isinstance(entry['name'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', entry['name'])):
                raise ValueError('native test coverage names must be bounded literal labels')
            if entry.get('runner') not in RUNNERS:
                raise ValueError('unsupported native test runner')
            policy = entry.get('policy', {})
            policy_keys = {'sentinel_regex', 'assertions_regex', 'allow_errors', 'editor', 'audio_driver', 'allow_error_version'}
            if (not isinstance(policy, dict) or set(policy)-policy_keys
                    or (policy and entry['runner'] not in {'godot', 'godot-check'})):
                raise ValueError('native assertion policy is unsupported for this runner')
            if entry['runner'] in {'godot', 'godot-suite'}:
                sentinel = entry.get('sentinel')
                regex_signal = entry['runner'] == 'godot' and isinstance(policy.get('sentinel_regex'), str)
                if not regex_signal and (not isinstance(sentinel, str) or not sentinel or '\n' in sentinel or '\r' in sentinel):
                    raise ValueError('Godot tests require one exact assertion sentinel')
            if 'python_environment' in entry:
                if entry['runner'] not in {'python', 'python-unittest'}:
                    raise ValueError('Python environments apply only to native Python test runners')
                relative(root, entry['python_environment'])
            program = relative(root, entry.get('path'), exists=True)
            discovery = entry['runner'] in {'godot-suite', 'python-unittest', 'node-test', 'bun-test'}
            if not (program.is_dir() if entry['runner'] == 'godot-suite' else
                    (program.is_file() or program.is_dir()) if discovery else program.is_file()):
                raise ValueError('test entry must name a source file')
            if not any(part in {'tests', 'test'} for part in Path(entry['path']).parts):
                raise ValueError('product test entry points belong with their tests')
            arguments = entry.get('args', [])
            if not isinstance(arguments, list) or any(not isinstance(value, str) or '\0' in value for value in arguments):
                raise ValueError('native test arguments must be literal strings')
            if entry['runner'] == 'godot-suite' and arguments:
                raise ValueError('Godot suite discovery does not accept script arguments')
            if 'cwd' in entry and not relative(root, entry['cwd'], exists=True).is_dir():
                raise ValueError('native test working directory must exist inside the checkout')
            if 'pattern' in entry:
                pattern = entry['pattern']
                if (entry['runner'] not in {'python-unittest', 'node-test'} or not isinstance(pattern, str)
                        or not pattern or '/' in pattern or '\\' in pattern or '\0' in pattern):
                    raise ValueError('native test discovery requires one filename pattern')
            if type(entry.get('timeout', 300)) is not int or not 1 <= entry.get('timeout', 300) <= 3600:
                raise ValueError('native tests require a bounded timeout')
    for name in data.get('clean', {}).get('paths', []):
        relative(root, name)
    return data


def godot_pins(root):
    config = tomllib.loads((Path(root) / 'mise.toml').read_text())
    values = config.get('env', {})
    names = ['GODOT_VERSION', 'GODOT_EXPECTED_VERSION', 'GODOT_BINARY_CHECKSUM', 'GODOT_TEMPLATES_CHECKSUM']
    pins = {name: values.get(name) for name in names}
    if not isinstance(pins['GODOT_VERSION'], str) or not re.fullmatch(r'\d+\.\d+\.\d+', pins['GODOT_VERSION']):
        raise ValueError('Godot requires an exact version in caller Mise pins')
    if not isinstance(pins['GODOT_EXPECTED_VERSION'], str) or not pins['GODOT_EXPECTED_VERSION'].startswith(pins['GODOT_VERSION'] + '.'):
        raise ValueError('Godot binary identity must match its pinned version')
    for name in ['GODOT_BINARY_CHECKSUM', 'GODOT_TEMPLATES_CHECKSUM']:
        if not isinstance(pins[name], str) or not re.fullmatch(r'(?:sha256:[a-f0-9]{64}|sha512:[a-f0-9]{128})', pins[name]):
            raise ValueError('Godot downloads require reviewed exact checksums')
    return pins
