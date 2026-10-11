import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('policy_generation', Path(__file__).parents[1]/'helpers/sync-workflow-policy.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class PolicyGenerationTests(unittest.TestCase):
    def test_cold_generation_and_offline_check_preserve_caller_catalogue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'.github'
            target.mkdir()
            catalogue = target/'workflow-contract.json'
            catalogue.write_text('{"fixture":"caller-owned"}\n')
            before = catalogue.read_bytes()
            generator.synchronize(root)
            generator.synchronize(root, check=True)
            import json
            self.assertEqual(json.loads(catalogue.read_text())["fixture"], "caller-owned")
            (target/'workflow_contract.py').write_text('local maintenance fork')
            with self.assertRaises(ValueError):
                generator.synchronize(root, check=True)

    def test_linked_late_destination_refuses_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'.github'
            target.mkdir()
            (target/'workflow-contract.json').write_text('{}')
            outside = root/'outside'
            outside.write_text('unchanged')
            (target/'pins_compat.mjs').symlink_to(outside)
            with self.assertRaises(ValueError):
                generator.synchronize(root)
            self.assertFalse((target/'workflow_contract.py').exists())
            self.assertEqual(outside.read_text(), 'unchanged')
