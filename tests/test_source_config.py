"""Source TOML has a strict value domain and preserves real declaration shapes."""
from datetime import date
import importlib.util
import math
import os
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / 'helpers/source_config.py'
SPEC = importlib.util.spec_from_file_location('source_config_controls', PATH)
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)


# Exact read algorithm from the accepted predecessor 39666e8d26d2bd0e1c7e5fee6f289fd0fd3a5202.
# This is a regression counterfactual, never a supported source decoder.
def legacy_read_bytes(path, *, maximum=8 << 20):
    path = Path(path)
    if path.suffix != '.toml' or path.is_symlink():
        raise ValueError('source must be a regular TOML file')
    if type(maximum) is not int or maximum < 1:
        raise ValueError('source byte limit must be a positive integer')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not source.stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError('source must be a bounded unlinked regular file')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    current = path.stat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if (len(raw) > maximum or path.is_symlink() or identity(before) != identity(after)
            or identity(before) != identity(current) or current.st_nlink != 1):
        raise ValueError('source changed while it was read')
    return raw


class SourceConfig(unittest.TestCase):
    @staticmethod
    def changed_stat(original, **changes):
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        return SimpleNamespace(**{name: changes.get(name, getattr(original, name)) for name in fields})

    def test_real_rewritten_file_uses_stable_path_and_descriptor_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.toml'
            source.write(path, {'value': 'initial'})
            time.sleep(0.1)  # Distinct native creation/change timestamps, as in CPython #157671.
            source.write(path, {'value': 'current'})
            by_path = path.stat()
            with path.open('rb') as stream:
                by_descriptor = os.fstat(stream.fileno())
            self.assertEqual((by_path.st_dev, by_path.st_ino), (by_descriptor.st_dev, by_descriptor.st_ino))
            try:
                legacy_read_bytes(path)
            except ValueError as error:
                self.assertEqual(str(error), 'source changed while it was read')
                legacy_refused = True
            else:
                legacy_refused = False
            self.assertEqual(legacy_refused, by_path.st_ctime_ns != by_descriptor.st_ctime_ns)
            self.assertEqual(source.read(path), {'value': 'current'})
            print('Source metadata probe:', json.dumps({'platform': sys.platform, 'python': sys.version,
                'path_ctime_ns': by_path.st_ctime_ns, 'descriptor_ctime_ns': by_descriptor.st_ctime_ns,
                'path_mtime_ns': by_path.st_mtime_ns, 'descriptor_mtime_ns': by_descriptor.st_mtime_ns,
                'legacy_cross_api_ctime_equal': by_path.st_ctime_ns == by_descriptor.st_ctime_ns,
                'legacy_reader_refused': legacy_refused, 'corrected_reader_passed': True,
                'same_file': True}, sort_keys=True))

    def test_stable_cross_api_ctime_difference_is_admitted_without_dropping_mutation_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.toml'
            source.write(path, {'value': 'current'})
            original = os.fstat
            def descriptor(fd):
                value = original(fd)
                return self.changed_stat(value, st_ctime_ns=value.st_ctime_ns + 1000000000)
            with mock.patch.object(source.os, 'fstat', side_effect=descriptor):
                self.assertEqual(source.read(path), {'value': 'current'})

    def test_descriptor_ctime_mutation_and_cross_api_identity_mismatches_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.toml'
            source.write(path, {'value': 'current'})
            original = os.fstat
            for field in ('st_ctime_ns', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_nlink', 'st_mode'):
                calls = []
                def descriptor(fd):
                    value = original(fd)
                    calls.append(fd)
                    if field != 'st_ctime_ns' or len(calls) > 1:
                        return self.changed_stat(value, **{field: getattr(value, field) + 1})
                    return value
                with self.subTest(field=field), mock.patch.object(source.os, 'fstat', side_effect=descriptor):
                    with self.assertRaises(ValueError):
                        source.read(path)

    def test_path_ctime_mutation_refuses_even_when_descriptor_identity_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.toml'
            source.write(path, {'value': 'current'})
            original = Path.stat
            calls = []
            def pathname(current, *, follow_symlinks=True):
                value = original(current, follow_symlinks=follow_symlinks)
                if current == path and follow_symlinks:
                    calls.append(current)
                    if len(calls) > 1:
                        return self.changed_stat(value, st_ctime_ns=value.st_ctime_ns + 1)
                return value
            with mock.patch.object(Path, 'stat', pathname), self.assertRaisesRegex(ValueError, 'changed while'):
                source.read(path)

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
