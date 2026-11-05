"""Real child processes cover suite scheduling, isolation and strict failures."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import importlib.util
spec=importlib.util.spec_from_file_location('shared_native_suite',Path(__file__).resolve().parents[1]/'helpers/godot-suite.py')
suite=importlib.util.module_from_spec(spec);spec.loader.exec_module(suite)
run_suite=suite.run_suite


class GodotSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        (self.project / 'tests').mkdir(parents=True)
        self.engine = self.root / 'engine'
        self.engine.write_text('#!' + sys.executable + '\n'
                               'import pathlib, runpy, sys\n'
                               'project = pathlib.Path(sys.argv[sys.argv.index("--path") + 1])\n'
                               'runpy.run_path(str(project / sys.argv[-1].removeprefix("res://")))\n')
        self.engine.chmod(0o755)

    def case(self, name, source):
        test = 'tests/' + name + '.gd'
        (self.project / test).write_text(source)
        return test

    def run_cases(self, tests, jobs=2, **environment):
        report = self.root / 'report'
        with patch.dict(os.environ, environment|{'CASE_OBSERVATIONS':str(self.root/'observations')}), contextlib.redirect_stdout(io.StringIO()):
            code = run_suite(str(self.engine), self.project, tests, report, jobs)
        return code, json.loads((report / 'summary.json').read_text())

    def test_all_cases_run_with_bounded_parallelism_and_unique_profiles(self):
        source = '''import json, os, pathlib, time
profile = pathlib.Path(os.environ['XDG_DATA_HOME'])
assert not (profile / 'claimed').exists()
(profile / 'claimed').write_text('owned')
started = time.monotonic()
time.sleep(.3)
destination=pathlib.Path(os.environ['CASE_OBSERVATIONS']);destination.mkdir(exist_ok=True)
(destination / (profile.parent.name+'.json')).write_text(json.dumps({
    'start': started, 'end': time.monotonic(), 'data': str(profile),
    'config': os.environ['XDG_CONFIG_HOME']}))
'''
        tests = [self.case(str(i), source) for i in range(5)]
        code, summary = self.run_cases(tests, SOURCE_COMMIT='fixture-source')
        self.assertEqual(code, 0)
        self.assertEqual(summary['inventory'], tests)
        self.assertEqual([r['test'] for r in summary['results']], tests)
        self.assertEqual(summary['source_commit'], 'fixture-source')
        rows = [json.loads(p.read_text()) for p in (self.root / 'observations').glob('*.json')]
        self.assertEqual(len(rows), len(tests))
        self.assertEqual(len({r['data'] for r in rows}), len(tests))
        self.assertEqual(len({r['config'] for r in rows}), len(tests))
        self.assertTrue(all(not Path(r['data']).exists() and not Path(r['config']).exists() for r in rows))
        overlap = max(sum(other['start'] <= row['start'] < other['end'] for other in rows) for row in rows)
        self.assertEqual(overlap, 2)

    def test_logged_error_nonzero_and_timeout_fail_without_omitting_other_cases(self):
        tests = [self.case('logged', "print('ERROR: fixture')"),
                 self.case('exit', 'raise SystemExit(7)'),
                 self.case('timeout', "import time; print('started', flush=True); time.sleep(10)"),
                 self.case('success', "print('PASS: fixture')")]
        code, summary = self.run_cases(tests, GODOT_TEST_TIMEOUT='1')
        self.assertEqual(code, 1)
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual([r['exit_code'] for r in summary['results']], [1, 7, 1, 0])
        self.assertIn('started', Path(summary['results'][2]['log']).read_text())
        self.assertIn('exceeded', Path(summary['results'][2]['log']).read_text())

    def test_serial_override_and_same_basename_keep_all_logs(self):
        (self.project / 'other').mkdir()
        tests = [self.case('same', "print('first')"), 'other/same.gd']
        (self.project / tests[1]).write_text("print('second')")
        code, summary = self.run_cases(tests, jobs=1)
        self.assertEqual(code, 0)
        self.assertEqual(summary['jobs'], 1)
        self.assertEqual(len({r['log'] for r in summary['results']}), 2)
        for previous, following in zip(summary['results'], summary['results'][1:]):
            self.assertGreaterEqual(following['started_seconds'] + .002,
                                    previous['started_seconds'] + previous['seconds'])

    def test_invalid_inventory_or_worker_count_cannot_pass(self):
        test = self.case('valid', 'pass')
        for tests, jobs in [([], 1), ([test, test], 1), (['../escape.gd'], 1),
                            (['missing.gd'], 1), ([test], 0), ([test], 5)]:
            with self.subTest(tests=tests, jobs=jobs), self.assertRaises(ValueError):
                run_suite(str(self.engine), self.project, tests, self.root / 'report', jobs)
        self.assertFalse((self.root / 'report').exists())
