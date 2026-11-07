"""Generic source-policy preparation stays isolated and separate from recipes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('generic_source_policy', ROOT/'helpers/source_policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class SourcePolicySDK(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'mise.toml').write_text('[tools]\npython="'+'.'.join(map(str,sys.version_info[:3]))+'"\n')
        self.calls = []

    def prepare(self, command, **options):
        self.calls.append((command,options))
        if 'venv' in command:
            _,python,_ = policy.inputs(self.root)
            python.parent.mkdir(parents=True,exist_ok=True)
            python.write_bytes(b'prepared finite SDK')

    def test_explicit_setup_keeps_hash_locked_isolated_commands_and_exact_receipt(self):
        policy.setup(self.root,{},self.prepare)
        first,second = [call[0] for call in self.calls]
        self.assertIn('--copies',first)
        self.assertIn('-I',first)
        for argument in ['-I','--require-hashes','--no-deps','--isolated','--only-binary']:
            self.assertIn(argument,second)
        directory,python,expected = policy.inputs(self.root)
        self.assertEqual(json.loads((directory/'.cookbook-source-policy.json').read_text()),expected)
        self.assertEqual(policy.interpreter(self.root),str(python))

    def test_stale_or_failed_preparation_cannot_admit_an_interpreter_or_install_during_reads(self):
        policy.setup(self.root,{},self.prepare)
        with patch.object(policy,'REQUIREMENTS',self.root/'changed'):
            policy.REQUIREMENTS.write_text('changed trusted requirements')
            with self.assertRaisesRegex(ValueError,'stale'):
                policy.interpreter(self.root)
        def fail(command, **options):
            if 'pip' in command:
                raise subprocess.CalledProcessError(1,command)
        with self.assertRaises(subprocess.CalledProcessError):
            policy.setup(self.root,{},fail)
        with self.assertRaises(ValueError):
            policy.interpreter(self.root)
        self.assertEqual(len(self.calls),2)

    def test_wrong_python_pin_refuses_before_any_preparation_command(self):
        (self.root/'mise.toml').write_text('[tools]\npython="0.0.0"\n')
        with self.assertRaisesRegex(ValueError,'exact caller-pinned Python'):
            policy.setup(self.root,{},self.prepare)
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
