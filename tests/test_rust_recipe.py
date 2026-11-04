import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / 'cookbook'))
spec = importlib.util.spec_from_file_location('rust_recipe_generation', TOOLS / 'cookbook/generate.py')
generate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate)


@unittest.skipIf(os.name == 'nt', 'orchestration fixtures use POSIX executable stubs; native Windows jobs exercise the actual compiler')
class RustRecipe(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='rust recipe ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ['tests', '.github', 'commands']:
            (self.root / directory).mkdir()
        readme = '# Native recipe fixture\n'
        (self.root / 'README.md').write_text(readme)
        (self.root / '.github/readme-source.json').write_text(json.dumps({'version': 1,
            'sha256': hashlib.sha256(readme.encode()).hexdigest()}))
        (self.root / '.gitignore').write_text('/.artifacts/\n/dist/\n')
        (self.root / 'Cargo.toml').write_text('[package]\nname="example"\nversion="0.0.1"\n')
        (self.root / 'Cargo.lock').write_text('version=4\n')
        (self.root / 'tests/boundary.py').write_text('assert 1 + 1 == 2\n')
        (self.root / 'tests/other_boundary.py').write_text('assert 3 + 1 == 4\n')
        compiler = self.root / 'commands/cargo'
        compiler.write_text('#!' + sys.executable + '\n' + r'''
import json,os
from pathlib import Path
import sys
root=Path.cwd();output=root/'.artifacts';output.mkdir(exist_ok=True)
with (output/'commands.jsonl').open('a') as stream:stream.write(json.dumps(sys.argv[1:])+'\n')
if sys.argv[1]=='build':
    binary=Path(os.environ['CARGO_TARGET_DIR'])/'release/example';binary.parent.mkdir(parents=True,exist_ok=True)
    binary.write_text('#!'+sys.executable+'\nprint('+repr('example '+os.environ['VERSION']+' ('+os.environ['COMMIT']+')')+')\n');binary.chmod(0o755)
''')
        compiler.chmod(0o755)
        (self.root / 'cookbook.toml').write_text('''version=1
recipe="rust-cli"
[[components.rust]]
name="cli"
manifest="Cargo.toml"
binaries=["example"]
[package]
binary="example"
output=".artifacts/build/example"
files=["README.md"]
version_arguments=["--version"]
version_template="{binary} {version} ({commit})"
[[package.native_targets]]
platform="Linux"
arch="x86_64"
extension="tar.gz"
target="x86_64-unknown-linux-gnu"
checks=["tests/boundary.py"]
[artifacts]
paths=["dist", ".artifacts/build", ".artifacts/native-package"]
[[tests.integration]]
runner="python"
path="tests/boundary.py"
[[tests.integration]]
runner="python"
path="tests/other_boundary.py"
[clean]
paths=["dist", ".artifacts/build", ".artifacts/native-package"]
''')
        (self.root / 'Makefile').write_text(generate.makefile(self.root, 'a' * 40))
        for command in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Native Recipe',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture']]:
            subprocess.run(['git', '-C', str(self.root), *command], check=True)
        self.environment = {**os.environ, 'PATH': str(self.root / 'commands') + os.pathsep + os.environ['PATH'],
                            'CARGO_TARGET_DIR': str(self.root / '.artifacts/rust-target')}
        for key in ['COMMIT', 'VERSION', 'DATE', 'RUST_TARGET', 'COOKBOOK_TEST_PATHS', 'NATIVE_CLI_ARCHIVES']:
            self.environment.pop(key, None)

    def make(self, name, selection=None):
        environment = self.environment | ({'COOKBOOK_TEST_PATHS': json.dumps(selection)} if selection else {})
        return subprocess.run(['make', '--no-print-directory', name, 'COOKBOOK_ROOT=' + str(TOOLS), 'COOKBOOK_EXEC='],
                              cwd=self.root, env=environment, capture_output=True, text=True)

    def passed(self, name, selection=None):
        result = self.make(name, selection)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_build_runs_compiler_validation_and_binds_closed_outputs(self):
        self.passed('build')
        commands = [json.loads(line)[0] for line in (self.root / '.artifacts/commands.jsonl').read_text().splitlines()]
        self.assertEqual(commands, ['build', 'clippy'])
        self.assertTrue((self.root / '.artifacts/verification/build.json').is_file())
        self.assertEqual(len(list((self.root / '.artifacts/native-package').iterdir())), 2)

    def test_boundary_tests_require_existing_outputs_without_building(self):
        self.assertNotEqual(self.make('test:integration').returncode, 0)
        self.assertFalse((self.root / '.artifacts/commands.jsonl').exists())
        self.passed('build')
        before = (self.root / '.artifacts/commands.jsonl').read_bytes()
        self.passed('test:integration')
        self.assertEqual(before, (self.root / '.artifacts/commands.jsonl').read_bytes())
        (self.root / '.artifacts/build/example').write_text('changed after build')
        self.assertNotEqual(self.make('test:integration').returncode, 0)

    def test_filtered_native_boundary_never_claims_full_group_or_aggregate(self):
        self.passed('build')
        self.passed('test')
        self.passed('test:integration', ['tests/boundary.py'])
        directory = self.root / '.artifacts/verification'
        self.assertFalse((directory / 'test.json').exists())
        name = 'native-' + hashlib.sha256(b'tests/boundary.py').hexdigest()[:24] + '.json'
        self.assertTrue((directory / name).is_file())
        self.assertNotEqual(self.make('test', ['tests/boundary.py']).returncode, 0)
        self.assertNotEqual(self.make('test:integration', ['tests/other_boundary.py']).returncode, 0)


if __name__ == '__main__':
    unittest.main()
