"""Fixed serial native imports restore owned settings and retain strict failures."""
import importlib.util
from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'helpers' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load('fixed_import', 'godot_import.py')
exporter = load('fixed_export', 'godot-export.py')
project = load('fixed_project', 'godot-project.py')


class FixedNativeImport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / 'project.godot'
        self.original = b'[application]\nconfig/name="native fixture"\n[gui]\ntheme/custom="res://theme.tres"\n'
        self.config.write_bytes(self.original)

    def test_existing_settings_features_escaped_keys_and_native_values_are_preserved(self):
        raw = (b'editor/import/use_multiple_threads.linux=true\n'
            b'[application]\nconfig/name="[editor]\\nimport/use_multiple_threads.fake=true"\n'
            b'config/example={"key": Array[String]([";escaped\\\"value"])}\n'
            b'[editor]\nother=true\nimport/use_multiple_threads=true\n'
            b'"import/use_multiple_threads.editor"=true "import/use_multiple_threads.\\u0064ebug"=true\n')
        self.config.write_bytes(raw)
        mode = stat.S_IMODE(self.config.stat().st_mode)
        with helper.serial_import(self.root):
            current = self.config.read_bytes()
            self.assertTrue(current.startswith(raw))
            self.assertIn(b'\nimport/use_multiple_threads=false\n', current[len(raw):])
            for feature in ['linux', 'editor', 'debug']:
                self.assertIn(('"import/use_multiple_threads.' + feature + '"=false\n').encode(), current[len(raw):])
            self.assertNotIn(b'import/use_multiple_threads.fake', current[len(raw):])
        self.assertEqual(self.config.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), mode)

    def test_line_endings_and_missing_terminal_newline_restore_exact_bytes(self):
        for newline in [b'\n', b'\r\n', b'\r']:
            for terminal in [True, False]:
                with self.subTest(newline=newline, terminal=terminal):
                    raw = b'[editor]' + newline + b'import/use_multiple_threads=true' + (newline if terminal else b'')
                    self.config.write_bytes(raw)
                    with helper.serial_import(self.root):
                        self.assertIn(newline + b'import/use_multiple_threads=false' + newline, self.config.read_bytes())
                    self.assertEqual(self.config.read_bytes(), raw)

    def test_valid_padded_native_editor_section_cannot_reenable_parallel_feature_import(self):
        raw = b'config_version=5\n[ \teditor \r]\nimport/use_multiple_threads.linux=true\n'
        self.config.write_bytes(raw)
        with helper.serial_import(self.root):
            current = self.config.read_bytes()
            self.assertTrue(current.startswith(raw))
            self.assertIn(b'"import/use_multiple_threads.linux"=false\n', current[len(raw):])
        self.assertEqual(self.config.read_bytes(), raw)
        # Native strip_edges keeps non-ASCII whitespace and internal spaces.
        for section in ['\u2003editor\u2003', 'ed itor']:
            body = ('[' + section + ']\nimport/use_multiple_threads.linux=true\n').encode()
            self.assertEqual(list(helper._properties(body)), [section + '/import/use_multiple_threads.linux'])
            self.assertNotIn(b'"import/use_multiple_threads.linux"=false', helper._serial_settings(body)[len(body):])

    def test_native_empty_section_keeps_prior_section_and_root_remains_root(self):
        raw = b'[editor]\n[]\nimport/use_multiple_threads.linux=true\n'
        self.assertEqual(list(helper._properties(raw)), ['editor/import/use_multiple_threads.linux'])
        self.assertIn(b'"import/use_multiple_threads.linux"=false\n', helper._serial_settings(raw)[len(raw):])
        body = b'[]\neditor/import/use_multiple_threads.debug=true\n'
        self.assertEqual(list(helper._properties(body)), ['editor/import/use_multiple_threads.debug'])
        body = b'[other]\n[ \t]\neditor/import/use_multiple_threads.linux=true\n'
        self.assertEqual(list(helper._properties(body)), ['other/editor/import/use_multiple_threads.linux'])
        self.assertNotIn(b'"import/use_multiple_threads.linux"=false', helper._serial_settings(body)[len(body):])

    def test_raised_error_and_timeout_restore_original_configuration(self):
        for error in [RuntimeError('native failure'), subprocess.TimeoutExpired(['godot'], 180)]:
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                with helper.serial_import(self.root):
                    raise error
            self.assertEqual(self.config.read_bytes(), self.original)

    def test_native_content_mutation_is_refused_and_owned_original_is_restored(self):
        with self.assertRaisesRegex(ValueError, 'native import modified'):
            with helper.serial_import(self.root):
                self.config.write_bytes(b'[application]\nconfig/name="changed by plugin"\n')
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_replaced_file_and_link_are_refused_without_overwriting_foreign_content(self):
        for link in [False, True]:
            with self.subTest(link=link):
                self.config.write_bytes(self.original)
                foreign = self.root / ('foreign-link' if link else 'foreign-file')
                foreign.write_bytes(b'foreign source')
                with self.assertRaises(ValueError):
                    with helper.serial_import(self.root):
                        if link:
                            self.config.unlink()
                            self.config.symlink_to(foreign)
                        else:
                            os.replace(foreign, self.config)
                if link:
                    self.assertTrue(self.config.is_symlink())
                    self.assertEqual(foreign.read_bytes(), b'foreign source')
                    self.config.unlink()
                else:
                    self.assertEqual(self.config.read_bytes(), b'foreign source')

    def test_hardlinks_and_oversized_or_unterminated_values_refuse_before_mutation(self):
        linked = self.root / 'linked'
        os.link(self.config, linked)
        with self.assertRaises(ValueError):
            with helper.serial_import(self.root):
                self.fail('hardlinked settings admitted')
        self.assertEqual(linked.read_bytes(), self.original)
        linked.unlink()
        for raw in [b'x' * (helper.MAXIMUM + 1), b'[editor]\nvalue={"unterminated": [1,2\n', b'[editor]\nvalue=true\0']:
            self.config.write_bytes(raw)
            with self.assertRaises(ValueError):
                with helper.serial_import(self.root):
                    self.fail('unsafe settings admitted')
            self.assertEqual(self.config.read_bytes(), raw)

    def test_binary_configuration_cannot_bypass_the_fixed_text_import_policy(self):
        (self.root / 'project.binary').write_bytes(b'foreign binary settings')
        with self.assertRaisesRegex(ValueError, 'binary override'):
            with helper.serial_import(self.root):
                self.fail('binary override admitted')
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_outer_bootstrap_restoration_does_not_follow_a_replacement_link(self):
        for kind in ['project', 'export']:
            self.config.write_bytes(self.original)
            foreign = self.root / ('foreign-' + kind)
            foreign.write_bytes(b'foreign configuration')
            copied = []
            def run(argv, timeout=180):
                directory = Path(argv[argv.index('--path') + 1])
                copied.append(directory)
                current = directory / 'project.godot'
                current.unlink()
                current.symlink_to(foreign)
                return 0
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                if kind == 'project':
                    with patch.object(project.subprocess, 'check_output', return_value='4.7.2.stable.fixture'):
                        project.import_project(self.root, 'godot', run)
                else:
                    exporter.export(self.root, [], run=run)
            self.assertEqual(foreign.read_bytes(), b'foreign configuration')
            if kind == 'project':
                self.assertTrue(self.config.is_symlink())
                self.config.unlink()
            else:
                self.assertTrue(all(not directory.exists() for directory in copied))
                self.assertEqual(self.config.read_bytes(), self.original)

    def test_all_project_and_export_imports_are_serial_but_export_sees_original(self):
        observed = []
        def run(argv, timeout=180):
            directory = Path(argv[argv.index('--path') + 1])
            raw = (directory / 'project.godot').read_bytes()
            observed.append((argv, timeout, raw))
            if '--import' in argv:
                self.assertIn(b'\nimport/use_multiple_threads=false\n', raw)
            else:
                self.assertEqual(raw, self.original)
                Path(argv[-1]).write_bytes(b'export')
            return 0
        with patch.object(project.subprocess, 'check_output', return_value='4.7.2.stable.fixture'):
            project.import_project(self.root, 'godot', run, timeout=180)
        exporter.export(self.root, [('Web', self.root / 'build/index.html')], binary='godot', run=run)
        self.assertEqual(len(observed), 5)
        for argv, timeout, _ in observed[:4]:
            self.assertEqual(argv[:4], ['godot', '--headless', '--editor', '--quit'])
            self.assertEqual(argv[-1], '--import')
            self.assertEqual(timeout, 180)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_both_export_import_failures_restore_copy_before_cleanup_and_leave_source_exact(self):
        for index in [0, 1]:
            calls = []
            copied = []
            def run(argv):
                directory = Path(argv[argv.index('--path') + 1])
                copied.append(directory)
                self.assertIn(b'\nimport/use_multiple_threads=false\n', (directory / 'project.godot').read_bytes())
                calls.append(argv)
                if len(calls) == index + 1:
                    raise subprocess.TimeoutExpired(argv, 180)
            with self.subTest(index=index), self.assertRaises(subprocess.TimeoutExpired):
                exporter.export(self.root, [('Web', self.root / 'build/index.html')], run=run)
            self.assertEqual(len(calls), index + 1)
            self.assertTrue(all(not directory.exists() for directory in copied))
            self.assertEqual(self.config.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
