"""The generic helper also loads without a repository import path."""
import importlib.util
from pathlib import Path
import unittest


class StandaloneImageImport(unittest.TestCase):
    def test_direct_file_loading_retains_native_admission(self):
        path = Path(__file__).parents[1] / 'helpers/image_import_cache.py'
        spec = importlib.util.spec_from_file_location('_standalone_image_cache', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertTrue(callable(module.reload))
        self.assertEqual(module.runtime.command(['version'], {})[0], ['podman','--remote=false','version'])
