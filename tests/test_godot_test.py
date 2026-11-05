import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('godot_test', Path(__file__).resolve().parents[1] / 'helpers/godot-test.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class GodotAssertions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native godot ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / 'engine'
        self.binary.write_text('''#!/usr/bin/env python3
from pathlib import Path
import sys
exec(Path(sys.argv[sys.argv.index('--script') + 1]).read_text())
''')
        self.binary.chmod(0o755)

    def assertion(self, source, timeout=1):
        path = self.root / 'behavior_test.gd'
        path.write_text(source)
        with contextlib.redirect_stdout(io.StringIO()):
            native.run(self.binary, self.root, path, 'PASS native behavior', timeout)

    def test_zero_exit_cannot_replace_a_reached_assertion(self):
        for source in ['pass', 'print("PASS native behavior")\nprint("PASS native behavior")',
                       'print("PASS native behavior")\nprint("SCRIPT ERROR: failed")',
                       'print("PASS native behavior")\nprint("\\x1b[31mERROR: failed\\x1b[0m")']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.assertion(source)

    def test_success_requires_exactly_one_sentinel_and_a_clean_exit(self):
        self.assertion('print("PASS native behavior")')
        with self.assertRaises(ValueError):
            self.assertion('print("PASS native behavior")\nraise SystemExit(2)')

    def test_deadline_terminates_a_stalled_assertion(self):
        with self.assertRaisesRegex(ValueError, 'deadline'):
            self.assertion('import time\ntime.sleep(60)', timeout=0.1)

    def test_absent_suite_is_a_failure(self):
        with self.assertRaisesRegex(ValueError, 'no native Godot'):
            native.suite(self.binary, self.root, self.root, 'PASS native')

    def test_positive_assertion_policies_reject_zero_duplicate_and_unreached_counts(self):
        path = self.root/'counted_test.gd'
        for count in [0, 2]:
            path.write_text('print("ASSERTIONS ' + str(count) + '")\nprint("PASS native")\n')
            with contextlib.redirect_stdout(io.StringIO()):
                if count:
                    native.run(self.binary, self.root, path, 'PASS native', assertions_regex=r'^ASSERTIONS ([0-9]+)$')
                else:
                    with self.assertRaisesRegex(ValueError, 'positive assertion'):
                        native.run(self.binary, self.root, path, 'PASS native', assertions_regex=r'^ASSERTIONS ([0-9]+)$')
        path.write_text('print("PASS native assertions=3")\n')
        with contextlib.redirect_stdout(io.StringIO()):
            native.run(self.binary, self.root, path, sentinel_regex=r'^PASS native assertions=([1-9][0-9]*)$')

    def test_editor_allowances_are_exact_and_bound_to_the_reviewed_sdk_version(self):
        path = self.root/'editor_test.gd'
        path.write_text('print("ERROR: 2 reviewed objects leaked")\nprint("PASS native")\n')
        policy = dict(editor=True, allow_errors=[r'^ERROR: [0-9]+ reviewed objects leaked$'], allow_error_version='4.7.2')
        environment = dict(os.environ, GODOT_VERSION='4.7.2')
        with contextlib.redirect_stdout(io.StringIO()):
            native.run(self.binary, self.root, path, 'PASS native', environment=environment, **policy)
            with self.assertRaises(ValueError):
                native.run(self.binary, self.root, path, 'PASS native', environment=environment | {'GODOT_VERSION': '4.8.0'}, **policy)
            path.write_text('print("ERROR: another failure")\nprint("PASS native")\n')
            with self.assertRaises(ValueError):
                native.run(self.binary, self.root, path, 'PASS native', environment=environment, **policy)

    def test_native_home_config_and_cache_are_isolated_from_the_host(self):
        path = self.root/'isolated_test.gd'
        path.write_text('import os\nfrom pathlib import Path\nfor name in ["HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"]:\n assert Path(os.environ[name]).is_relative_to(Path(os.environ["HOME"]))\nassert os.environ["XDG_DATA_HOME"] == "pinned-template-location"\nprint("PASS native")\n')
        environment = dict(os.environ, XDG_CONFIG_HOME='/foreign-config', XDG_CACHE_HOME='/foreign-cache',
                           XDG_DATA_HOME='pinned-template-location')
        with contextlib.redirect_stdout(io.StringIO()):
            native.run(self.binary, self.root, path, 'PASS native', environment=environment)


if __name__ == '__main__':
    unittest.main()
