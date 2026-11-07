"""Source TOML has a strict value domain and preserves real declaration shapes."""
from datetime import date
import importlib.util
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PATH = Path(__file__).resolve().parents[1] / 'helpers/source_config.py'
SPEC = importlib.util.spec_from_file_location('source_config_controls', PATH)
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)


class SourceConfig(unittest.TestCase):
    def test_mixed_health_checks_layout_numbers_and_empty_containers_roundtrip(self):
        data = {'health_urls': ['https://example.test/', {'url': 'https://example.test/api', 'contains': 'ready'}],
                'layout': [1, 1.0, -0.0, [], {}, {'empty': []}], 'args': ['literal;argument', 'line\nnext'],
                'pins': {'revision': '0123456789abcdef', 'version': '1.2.3', 'when': '2026-11-07'},
                'enabled': False, 'timeout': 45}
        observed = source.loads(source.dumps(data))
        self.assertEqual(observed, data)
        self.assertIs(type(observed['layout'][0]), int)
        self.assertIs(type(observed['layout'][1]), float)
        self.assertEqual(math.copysign(1, observed['layout'][2]), -1)

    def test_wrappers_are_explicit_and_array_roots_are_not_guessed(self):
        for name in ['sources', 'remediations', 'exports', 'evidence', 'publications']:
            value = {name: [{'path': 'owned/file'}, {}]}
            self.assertEqual(source.loads(source.dumps(value)), value)
        with self.assertRaises(ValueError):
            source.dumps([{'path': 'owned/file'}])

    def test_non_json_types_null_duplicate_fields_and_nonfinite_numbers_refuse(self):
        for value in [None, date(2026, 11, 7), float('nan'), float('inf'), 2 ** 63, -(2 ** 63)-1, '\ud800']:
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                source.dumps({'value': value})
        for raw in ['value=2026-11-07\n', 'value=nan\n', 'value=inf\n', 'value=9223372036854775808\n', 'value=1\nvalue=2\n']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                source.loads(raw)

    def test_bounded_regular_toml_file_and_link_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'source.toml'
            source.write(path, {'value': 'literal'})
            self.assertEqual(source.read(path), {'value': 'literal'})
            with self.assertRaises(ValueError):
                source.read(path, maximum=1)
            other = root / 'source.json'
            other.write_text('{"value":"literal"}')
            with self.assertRaises(ValueError):
                source.read(other)
            linked = root / 'linked.toml'
            linked.symlink_to(path)
            with self.assertRaises(ValueError):
                source.read(linked)
            linked.unlink()
            linked.hardlink_to(path)
            with self.assertRaises(ValueError):
                source.read(path)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO metadata is POSIX only')
    def test_fifo_is_refused_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.toml'
            os.mkfifo(path)
            result = subprocess.run([sys.executable, '-I', '-B', str(PATH), str(path)],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('regular file', result.stderr)
            self.assertEqual(result.stdout, '')

    def test_recipe_api_refuses_foreign_cached_source_and_native_compilers(self):
        program = '''import sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import configuration
module=types.ModuleType(sys.argv[2]);module.__file__='/foreign/'+sys.argv[2]+'.py'
sys.modules[sys.argv[2]]=module
try:
 if sys.argv[2]=='source_config': configuration.parse_source_data(b'value=1')
 else: configuration.runtime_configuration()
except ValueError as error: print(error)
else: raise AssertionError('foreign cached source authority was admitted')
'''
        for name in ['source_config', 'runtime_config']:
            result = subprocess.run([sys.executable, '-I', '-B', '-c', program,
                str(PATH.parents[1] / 'cookbook'), name], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('origin differs', result.stdout)

    def test_cli_emits_only_decoded_json_wire_and_retains_file_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owned.toml'
            source.write(path, {'sources': [{'source': 'input.prompt', 'target': 'output.md'}]})
            result = subprocess.run([sys.executable, '-I', '-B', str(PATH), str(path)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '{"sources": [{"source": "input.prompt", "target": "output.md"}]}\n')
            result = subprocess.run([sys.executable, '-I', '-B', str(PATH), str(path), '--maximum', '1'],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
