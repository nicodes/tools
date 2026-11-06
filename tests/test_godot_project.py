import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('game_project', Path(__file__).resolve().parents[1] / 'helpers/godot-project.py')
project = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project)


class StrictProjectImport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = b'[gui]\ntheme/custom="res://theme.tres"\n'
        (self.root / 'project.godot').write_bytes(self.config)
        self.calls = []

    def engine(self, command, timeout):
        self.calls.append((command, timeout))
        imported = self.root / '.godot/imported'
        imported.mkdir(parents=True, exist_ok=True)
        (imported / 'texture.ctex').write_bytes(b'compiled texture')
        (self.root / '.godot/global_script_class_cache.cfg').write_bytes(b'classes')
        (self.root / '.godot/uid_cache.bin').write_bytes(b'uid cache')
        return 0

    def invoke(self, run=None):
        with patch.object(project.subprocess, 'check_output', return_value='4.7.2.stable.fixture'):
            project.import_project(self.root, 'godot', run or self.engine)

    def test_verified_cache_keeps_a_strict_native_import_without_repeating_bootstrap(self):
        self.invoke()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((self.root / 'project.godot').read_bytes(), self.config)
        self.invoke()
        self.assertEqual(len(self.calls), 3)

    def test_changed_compiled_cache_cannot_skip_cold_import(self):
        self.invoke()
        (self.root / '.godot/imported/texture.ctex').write_bytes(b'tampered')
        self.invoke()
        self.assertEqual(len(self.calls), 4)

    def test_failed_bootstrap_restores_exact_authored_configuration(self):
        with self.assertRaises(ValueError):
            self.invoke(lambda command, timeout: 1)
        self.assertEqual((self.root / 'project.godot').read_bytes(), self.config)
        self.assertFalse((self.root / '.godot/cookbook-import.json').exists())

    def test_gdignore_policy_is_an_import_input(self):
        self.invoke()
        (self.root / '.gdignore').touch()
        self.invoke()
        self.assertEqual(len(self.calls), 4)

    def test_generated_uid_is_bound_after_import_and_remains_warm_until_its_bytes_change(self):
        script = self.root / 'core/autoloads/generated.gd'
        script.parent.mkdir(parents=True)
        script.write_bytes(b'extends Node\n')
        uid = script.with_suffix('.gd.uid')
        def generate(command, timeout):
            uid.write_bytes(b'uid://generated\n')
            return self.engine(command, timeout)
        self.invoke(generate)
        receipt = json.loads((self.root / '.godot/cookbook-import.json').read_text())
        self.assertEqual(receipt['inputs']['files']['core/autoloads/generated.gd.uid'], hashlib.sha256(uid.read_bytes()).hexdigest())
        self.assertEqual(len(self.calls), 2)
        self.invoke(generate)
        self.assertEqual(len(self.calls), 3)
        uid.write_bytes(b'uid://different\n')
        self.invoke(generate)
        self.assertEqual(len(self.calls), 5)


if __name__ == '__main__':
    unittest.main()
