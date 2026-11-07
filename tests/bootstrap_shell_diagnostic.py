"""Print bounded original/explicit shell identity for the current native runner."""
import json
import importlib.util
import os
from pathlib import Path
import shutil

spec = importlib.util.spec_from_file_location('native_shell_diagnostic', Path(__file__).with_name('native_shell.py'))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
bash, probe = native.bash, native.probe


print(json.dumps({'platform': os.name, 'PATH_bash': shutil.which('bash'),
                  'PATH_git': shutil.which('git')}), flush=True)
selected = bash()
record = {'platform': os.name, 'PATH_bash': shutil.which('bash'),
          'PATH_git': shutil.which('git'), 'selected': selected,
          'original_bare': probe('bash'), 'explicit_native': probe(selected)}
print(json.dumps(record, sort_keys=True), flush=True)
assert record['explicit_native']['exit'] == 0, 'selected native Bash did not execute its finite probe'
assert record['explicit_native']['stdout'].startswith('BASH_PROBE:'), 'native Bash probe produced no identity'
if os.name == 'nt':
    assert '|msys' in record['explicit_native']['stdout'], 'the selected Windows shell is not Git Bash'
