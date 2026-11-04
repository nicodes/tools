import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / 'cookbook'))
spec = importlib.util.spec_from_file_location('game_generation', TOOLS / 'cookbook/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class NativeGameContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='game recipe ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'app/tests').mkdir(parents=True)
        (self.root / 'app/project.godot').write_text('[application]\nconfig/name="fixture"\n')
        (self.root / 'app/tests/behavior.gd').write_text('native assertion fixture\n')
        (self.root / '.gitignore').write_text('/.artifacts/\n/app/build/\n')
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="godot-game"
[components.godot]
project="app"
exports=[{preset="Web",output="app/build/web/index.html"}]
[artifacts]
paths=["app/build/web"]
[[tests.unit]]
runner="godot-check"
path="app/tests/behavior.gd"
cwd="app"
timeout=2
[[tests.integration]]
runner="godot-check"
path="app/tests/behavior.gd"
cwd="app"
timeout=2
''')
        revision = subprocess.check_output(['git', '-C', str(TOOLS), 'rev-parse', 'HEAD'], text=True).strip()
        (self.root / 'Makefile').write_text(generator.makefile(self.root, revision))
        for args in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Recipe Controls', '-c',
                     'user.email=controls@example.invalid', 'commit', '-qm', 'native fixture']]:
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        binary = self.root / '.artifacts/godot/bin/godot'
        binary.parent.mkdir(parents=True)
        binary.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3]
with (root/'.artifacts/engine-calls.jsonl').open('a') as output:output.write(json.dumps(sys.argv[1:])+'\\n')
if '--export-release' in sys.argv:
    destination=Path(sys.argv[-1]);destination.parent.mkdir(parents=True,exist_ok=True)
    for suffix in ['.html','.wasm','.pck','.js']:destination.with_suffix(suffix).write_bytes(b'native export fixture')
print('native fixture reached')
''')
        binary.chmod(0o755)

    def make(self, operation):
        return subprocess.run(['make', '--no-print-directory', operation, 'COOKBOOK_ROOT=' + str(TOOLS), 'COOKBOOK_EXEC='],
                              cwd=self.root, capture_output=True, text=True)

    def succeeded(self, operation):
        result = self.make(operation)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_units_never_export_or_implicitly_prepare_a_product_build(self):
        self.succeeded('test:unit')
        commands = [json.loads(line) for line in (self.root / '.artifacts/engine-calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands), 1)
        self.assertIn('--script', commands[0])
        self.assertNotIn('--export-release', commands[0])
        self.assertFalse((self.root / 'app/build').exists())

    def test_integration_requires_existing_unchanged_outputs_and_never_repairs_them(self):
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertFalse((self.root / 'app/build').exists())
        self.succeeded('build')
        self.succeeded('test:integration')
        output = self.root / 'app/build/web/index.wasm'
        output.write_bytes(b'mutated artifact')
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertEqual(output.read_bytes(), b'mutated artifact')

    def test_missing_development_capability_is_absent_from_help(self):
        self.succeeded('help')
        self.assertNotIn('make dev ', self.make('help').stdout)
        self.assertNotEqual(self.make('dev').returncode, 0)


if __name__ == '__main__':
    unittest.main()
