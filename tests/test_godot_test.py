import contextlib
import importlib.util
import io
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


if __name__ == '__main__':
    unittest.main()
