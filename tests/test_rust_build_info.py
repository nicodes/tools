import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runtime_info', Path(__file__).resolve().parents[1] / 'helpers/rust-build-info.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class RuntimeInfoControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\n')
        (self.root / 'Cargo.lock').write_bytes(b'reviewed lock')
        (self.root / 'routes').write_bytes(b'reviewed host routing')
        self.binary = self.root / 'runtime'; self.binary.write_bytes(b'native fixture bytes')
        self.receipt = {'runtime': 'rust-axum', 'source_commit': 'a' * 40,
            'lock_sha256': hashlib.sha256((self.root / 'Cargo.lock').read_bytes()).hexdigest(),
            'routes_sha256': hashlib.sha256((self.root / 'routes').read_bytes()).hexdigest(),
            'rustc': 'rustc 1.94.0 reviewed'}

    def check(self, program='', dynamic=''):
        with patch.object(helper.subprocess, 'check_output', side_effect=[json.dumps(self.receipt), program, dynamic]):
            return helper.verify(self.binary, 'a' * 40, 'Cargo.lock', 'routes', root=self.root)

    def test_exact_native_bytes_and_source_contract_are_reported(self):
        value = self.check()
        self.assertEqual(value['binary_sha256'], hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual(value['source_commit'], 'a' * 40)

    def test_source_lock_routes_and_compiler_mutations_fail_then_restore(self):
        for key in ['source_commit', 'lock_sha256', 'routes_sha256', 'rustc']:
            with self.subTest(field=key):
                original = self.receipt[key]; self.receipt[key] = 'wrong'
                with self.assertRaises(ValueError): self.check()
                self.receipt[key] = original
                self.check()
        self.receipt['extra'] = 'undeclared'
        with self.assertRaises(ValueError): self.check()

    def test_dynamic_linking_cannot_pass_the_scratch_runtime_contract(self):
        for program, dynamic in [(' INTERP ', ''), ('', '(NEEDED) shared library')]:
            with self.subTest(program=program, dynamic=dynamic), self.assertRaisesRegex(ValueError, 'statically linked'):
                self.check(program, dynamic)
        self.check()


if __name__ == '__main__':
    unittest.main()
