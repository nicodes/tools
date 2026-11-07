"""Native JSON is authenticated generated I/O, never a second source contract."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PATH = Path(__file__).resolve().parents[1] / 'helpers/runtime_config.py'
SPEC = importlib.util.spec_from_file_location('runtime_config_controls', PATH)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeConfig(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('.artifacts/\n/app/gdam.json\n')
        (self.root / 'app').mkdir()
        self.source = self.root / 'app/gdam.toml'
        runtime.source_config.write(self.source, {'addons': {'@example/test': {'tag': 'v1.2.3'}}})
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)

    def test_prepare_verify_current_bytes_and_changed_source_refusal(self):
        target = runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        self.assertEqual(target, self.root / 'app/gdam.json')
        self.assertEqual(runtime.verify(self.root, 'app/gdam.toml', 'gdam'), target)
        self.assertEqual(json.loads(target.read_bytes()), runtime.source_config.read(self.source))
        runtime.source_config.write(self.source, {'addons': {'@example/test': {'tag': 'v1.2.4'}}})
        with self.assertRaisesRegex(ValueError, 'prepare current'):
            runtime.verify(self.root, 'app/gdam.toml', 'gdam')
        runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        self.assertEqual(json.loads(target.read_bytes())['addons']['@example/test']['tag'], 'v1.2.4')

    def test_foreign_tracked_unignored_and_modified_outputs_refuse(self):
        target = self.root / 'app/gdam.json'
        target.write_text('{"foreign":true}')
        with self.assertRaisesRegex(ValueError, 'foreign'):
            runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        self.assertEqual(target.read_text(), '{"foreign":true}')
        target.unlink()
        runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        target.write_text('{"foreign":true}')
        with self.assertRaisesRegex(ValueError, 'foreign'):
            runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        subprocess.run(['git', '-C', str(self.root), 'add', '--force', 'app/gdam.json'], check=True)
        with self.assertRaisesRegex(ValueError, 'tracked'):
            runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        subprocess.run(['git', '-C', str(self.root), 'rm', '--cached', '--quiet', 'app/gdam.json'], check=True)
        target.unlink()
        (self.root / '.gitignore').write_text('.artifacts/\n')
        with self.assertRaisesRegex(ValueError, 'ignored'):
            runtime.prepare(self.root, 'app/gdam.toml', 'gdam')

    def test_source_and_output_links_refuse(self):
        other = self.root / 'other.toml'
        other.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'links'):
            runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        self.source.unlink()
        self.source.write_bytes(other.read_bytes())
        (self.root / 'app/gdam.json').symlink_to(self.root / 'foreign.json')
        with self.assertRaisesRegex(ValueError, 'links'):
            runtime.prepare(self.root, 'app/gdam.toml', 'gdam')

    def test_absent_optional_source_cannot_enable_foreign_or_owned_orphan_links(self):
        links = self.root / 'app/gdam.link.json'
        links.write_text('{"links":{"@example/test":"../other"}}')
        for command in [runtime.prepare, runtime.verify]:
            with self.subTest(state='foreign', command=command.__name__), self.assertRaisesRegex(ValueError, 'obsolete or foreign'):
                command(self.root, 'app/gdam.toml', 'gdam')
        self.assertEqual(json.loads(links.read_bytes()), {'links': {'@example/test': '../other'}})
        links.unlink()
        (self.root / '.gitignore').write_text('.artifacts/\n/app/gdam.json\n/app/gdam.link.json\n')
        optional = self.root / 'app/gdam.link.toml'
        runtime.source_config.write(optional, {'links': {'@example/test': '../other'}})
        subprocess.run(['git', '-C', str(self.root), 'add', 'app/gdam.link.toml'], check=True)
        runtime.prepare(self.root, 'app/gdam.link.toml', 'gdam-link')
        runtime.prepare(self.root, 'app/gdam.toml', 'gdam')
        original = links.read_bytes()
        optional.unlink()
        for command in [runtime.prepare, runtime.verify]:
            with self.subTest(state='owned-orphan', command=command.__name__), self.assertRaisesRegex(ValueError, 'obsolete or foreign'):
                command(self.root, 'app/gdam.toml', 'gdam')
        self.assertEqual(links.read_bytes(), original)

    def test_explicit_temp_assertion_wire_is_private_and_removed(self):
        with runtime.temporary(self.root, 'app/gdam.toml') as target:
            self.assertEqual(json.loads(target.read_bytes()), runtime.source_config.read(self.source))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, 'changed'):
            with runtime.temporary(self.root, 'app/gdam.toml') as target:
                target.write_text('{}')
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, 'changed'):
            with runtime.temporary(self.root, 'app/gdam.toml') as target:
                self.source.write_text('changed=true\n')
        self.assertFalse(target.exists())

    def test_explicit_child_keeps_argv_status_and_cleans_projection(self):
        recorder = self.root / 'record.py'
        record = self.root / 'record.json'
        recorder.write_text('import json,pathlib,sys\n'
                            'path=pathlib.Path(sys.argv[1])\n'
                            'pathlib.Path(sys.argv[2]).write_text(json.dumps([str(path),json.loads(path.read_bytes()),sys.argv[3:]]))\n'
                            'raise SystemExit(7)\n')
        result = subprocess.run([sys.executable, '-I', '-B', str(PATH), 'run', '--root', str(self.root),
            '--source', 'app/gdam.toml', '--', sys.executable, '-I', '-B', str(recorder),
            '@NATIVE_CONFIG@', str(record), '--literal', 'a;b'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        path, data, argv = json.loads(record.read_bytes())
        self.assertFalse(Path(path).exists())
        self.assertEqual(data, runtime.source_config.read(self.source))
        self.assertEqual(argv, ['--literal', 'a;b'])


if __name__ == '__main__':
    unittest.main()
