"""Keep reviewed engine patches closed and caller identifiers literal."""
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('godot_web_runtime', Path(__file__).resolve().parents[1] / 'helpers/godot-web-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeControls(unittest.TestCase):
    def handle_fixture(self):
        return runtime.ALLOCATION + ';' + ';'.join('GL.' + slot + '=null' for slot in runtime.DELETED_SLOTS)

    def test_handle_patch_preserves_monotonic_identity_and_fails_on_unreviewed_input(self):
        source = self.handle_fixture()
        with self.assertRaises(ValueError):
            runtime.release_gl_table_slots(source)
        with patch.object(runtime, 'REVIEWED_SHA256', {hashlib.sha256(source.encode()).hexdigest()}):
            result = runtime.release_gl_table_slots(source)
            self.assertIn('getNewId:table=>GL.counter++', result)
            self.assertNotIn('table[i]=null', result)
            self.assertTrue(all('delete GL.' + slot in result for slot in runtime.DELETED_SLOTS))
            with self.assertRaises(ValueError):
                runtime.release_gl_table_slots(result)

    def test_missing_duplicate_or_changed_native_operations_are_rejected(self):
        source = self.handle_fixture()
        for candidate in [source.replace(runtime.ALLOCATION, ''), source + runtime.ALLOCATION,
                          source.replace('GL.buffers[id]=null', '')]:
            with self.subTest(candidate=candidate), patch.object(runtime, 'REVIEWED_SHA256', {hashlib.sha256(candidate.encode()).hexdigest()}):
                with self.assertRaises(ValueError):
                    runtime.release_gl_table_slots(candidate)

    def test_extension_fetch_preserves_dlopen_identity_and_uses_declared_release_root(self):
        source = "'dynamicLibraries': [], const gdext = this.gdextensionLibs; 'locateFile': function (path) {return path;}"
        changed = runtime.version_extension_fetch(source, 'nativeReleaseRoot')
        self.assertIn('gdext.includes(path) && globalThis.nativeReleaseRoot', changed)
        self.assertIn('return globalThis.nativeReleaseRoot + path;', changed)
        self.assertIn('const gdext = this.gdextensionLibs;', changed)
        self.assertEqual(runtime.version_extension_fetch('static engine', 'nativeReleaseRoot'), 'static engine')
        for candidate in [source.replace('const gdext = this.gdextensionLibs;', ''), source + source]:
            with self.assertRaises(ValueError):
                runtime.version_extension_fetch(candidate, 'nativeReleaseRoot')
        for name in ['', 'x; injection()', 'obj.property']:
            with self.assertRaises(ValueError):
                runtime.version_extension_fetch(source, name)


if __name__ == '__main__':
    unittest.main()
