import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runner', Path(__file__).resolve().parents[1] / 'helpers/godot-game-runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def invoke(self, response):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)/'timings.jsonl'
            with patch.dict(os.environ, {'GODOT_TEST_TIMINGS': str(evidence)}), patch.object(runner.subprocess, 'run', side_effect=response) as process, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = runner.main(['godot', '--script', 'res://tests/example.gd'])
            record = json.loads(evidence.read_text())
            environment = process.call_args.kwargs['env']
            self.assertFalse(Path(environment['XDG_DATA_HOME']).parent.exists())
            self.assertNotEqual(environment['XDG_DATA_HOME'], os.environ.get('XDG_DATA_HOME'))
            return code, record

    def test_mise_keeps_parent_xdg_locations(self):
        def response(*args, **kwargs):
            environment = kwargs['env']
            self.assertEqual(environment['MISE_DATA_DIR'], '/parent-data/mise')
            self.assertEqual(environment['MISE_CONFIG_DIR'], '/parent-config/mise')
            return subprocess.CompletedProcess(args, 0, 'PASS')
        with patch.dict(os.environ, {'XDG_DATA_HOME': '/parent-data', 'XDG_CONFIG_HOME': '/parent-config'}, clear=True):
            self.invoke(response)

    def test_explicit_mise_locations_are_preserved(self):
        def response(*args, **kwargs):
            self.assertEqual(kwargs['env']['MISE_DATA_DIR'], '/explicit-data')
            self.assertEqual(kwargs['env']['MISE_CONFIG_DIR'], '/explicit-config')
            return subprocess.CompletedProcess(args, 0, 'PASS')
        with patch.dict(os.environ, {'MISE_DATA_DIR': '/explicit-data', 'MISE_CONFIG_DIR': '/explicit-config'}):
            self.invoke(response)

    def test_error_output_still_fails_with_zero_exit(self):
        code, record = self.invoke(lambda *a, **k: subprocess.CompletedProcess(a, 0, 'SCRIPT ERROR: bad'))
        self.assertEqual(code, 1)
        self.assertEqual(record['exit_code'], 1)

    def test_subprocess_status_is_preserved(self):
        code, record = self.invoke(lambda *a, **k: subprocess.CompletedProcess(a, 7, ''))
        self.assertEqual(code, 7)
        self.assertEqual(record['exit_code'], 7)

    def test_timeout_cleans_profile_and_records_failure(self):
        code, record = self.invoke(subprocess.TimeoutExpired('godot', 180, output=b'partial'))
        self.assertEqual(code, 1)
        self.assertTrue(record['timeout'])

    def test_success_has_fresh_profile_and_timing(self):
        code, record = self.invoke(lambda *a, **k: subprocess.CompletedProcess(a, 0, 'PASS'))
        self.assertEqual(code, 0)
        self.assertEqual(record['test'], 'res://tests/example.gd')
