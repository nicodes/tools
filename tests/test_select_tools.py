import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('select_tools', Path(__file__).resolve().parents[1] / 'helpers/select-tools.py')
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)


class SelectedTools(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nrust="1.94.0"\n"github:owner/compiler"={version="v1.2.3"}\n')

    def test_subset_resolves_only_identifiers_from_existing_pins(self):
        self.assertEqual(tools.select(self.root, 'python rust'), ['python', 'rust'])
        self.assertEqual(tools.select(self.root, 'github:owner/compiler'), ['github:owner/compiler'])

    def test_flags_overrides_unlisted_tools_and_shell_text_are_refused(self):
        for value in ['python@latest', '--help', 'node', 'python;echo secret', '$(command)', 'python python', '']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tools.select(self.root, value)

    def test_missing_pin_file_is_not_replaced_by_global_defaults(self):
        (self.root / 'mise.toml').unlink()
        with self.assertRaisesRegex(ValueError, 'caller-owned'):
            tools.select(self.root, 'python')


if __name__ == '__main__':
    unittest.main()
