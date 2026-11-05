import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('runtime_inputs', Path(__file__).resolve().parents[1] / 'helpers/runtime-inputs.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class RuntimeInputs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'input.prompt').write_bytes(b'exact functional input')
        self.rows = [{'source': 'input.prompt', 'target': 'native/input.md'}]

    def prepare(self):
        (self.root / 'runtime.json').write_text(json.dumps(self.rows))
        return native.prepare(self.root, 'runtime.json')

    def test_idempotent_generation_preserves_source_bytes_and_refuses_user_edits(self):
        self.assertEqual(self.prepare(), 1)
        self.assertEqual((self.root / 'native/input.md').read_bytes(), (self.root / 'input.prompt').read_bytes())
        self.assertEqual(self.prepare(), 1)
        (self.root / 'native/input.md').write_bytes(b'edited runtime input')
        with self.assertRaisesRegex(ValueError, 'modified runtime'):
            self.prepare()
        self.assertEqual((self.root / 'native/input.md').read_bytes(), b'edited runtime input')

    def test_all_records_validate_before_any_native_file_is_created(self):
        self.rows += [{'source': '../outside', 'target': 'native/unsafe'}]
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / 'native/input.md').exists())

    def test_only_the_declared_internal_symlink_is_retained(self):
        self.rows[0]['symlink'] = '../input.prompt'
        self.prepare()
        self.assertTrue((self.root / 'native/input.md').is_symlink())
        self.assertEqual(self.prepare(), 1)
        self.rows[0]['symlink'] = '../../outside'
        with self.assertRaises(ValueError):
            self.prepare()


if __name__ == '__main__':
    unittest.main()
