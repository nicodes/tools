import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('clean', Path(__file__).parents[1] / 'helpers/clean.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class GeneratedCleanup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'source.rs').write_text('source')
        subprocess.run(['git', '-C', str(self.root), 'add', 'source.rs'], check=True)
        (self.root / 'dist').mkdir()
        (self.root / 'dist/output').write_text('generated')

    def test_removes_owned_outputs_and_is_idempotent(self):
        helper.clean(self.root, ['dist', 'missing'])
        helper.clean(self.root, ['dist'])
        self.assertFalse((self.root / 'dist').exists())
        self.assertEqual((self.root / 'source.rs').read_text(), 'source')

    def test_validates_every_path_before_removing_any_output(self):
        for unsafe in ['source.rs', '.', '../other', '/tmp/other', '.git', '.artifacts']:
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                helper.clean(self.root, ['dist', unsafe])
            self.assertTrue((self.root / 'dist/output').is_file())

    def test_linked_output_and_ancestor_preserve_external_files(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('preserved')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        for path in ['linked', 'linked/keep']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                helper.clean(self.root, [path])
        self.assertEqual((outside / 'keep').read_text(), 'preserved')

    def test_nested_tracked_source_and_non_root_invocation_refuse(self):
        (self.root / 'src').mkdir()
        (self.root / 'src/keep').write_text('source')
        subprocess.run(['git', '-C', str(self.root), 'add', 'src'], check=True)
        with self.assertRaises(ValueError):
            helper.clean(self.root, ['src'])
        with self.assertRaises(ValueError):
            helper.clean(self.root / 'dist', ['output'])

    def test_link_inside_generated_tree_is_unlinked_without_following(self):
        outside = Path(self.temp.name) / 'keep'
        outside.write_text('preserved')
        (self.root / 'dist/link').symlink_to(outside)
        helper.clean(self.root, ['dist'])
        self.assertEqual(outside.read_text(), 'preserved')


if __name__ == '__main__':
    unittest.main()
