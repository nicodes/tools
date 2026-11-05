"""Only explicit generated diagnostics can be retained independently of success."""
import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
spec=importlib.util.spec_from_file_location('native_diagnostics',Path(__file__).resolve().parents[1]/'helpers/native-diagnostics.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
class NativeDiagnostics(unittest.TestCase):
    def test_retains_only_selected_native_locations_and_does_not_execute_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'cookbook.toml').write_text('recipe="godot-game"\n[artifacts]\nevidence_paths=["tmp/fixture-proof","public-evidence"]\nevidence_retention_days={"tmp/fixture-proof"=14}\n')
            self.assertEqual(helper.read(root),(['tmp/fixture-proof'],14))
            identity=hashlib.sha256((root/'cookbook.toml').read_bytes()).hexdigest()
            self.assertEqual(helper.read(root,expected=identity),(['tmp/fixture-proof'],14))
            with self.assertRaises(ValueError):helper.read(root,expected='a'*64)
            (root/'tmp/fixture-proof').mkdir(parents=True)
            (root/'tmp/fixture-proof/failure.json').write_text('{"success":false}')
            self.assertEqual(helper.read(root,inspect=True),(['tmp/fixture-proof'],14))
    def test_rejects_undeclared_broad_source_injection_and_retention_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for path,days in [('tmp',14),('app',14),('api/source',14),('tmp/../app',14),('tmp/proof\nother',14),('tmp/proof/**',14),('tmp/proof',True),('tmp/proof',0),('tmp/proof',91)]:
                config={'recipe':'godot-game','artifacts':{'evidence_paths':[path],'evidence_retention_days':{path:days}}}
                with self.subTest(path=path,days=days),self.assertRaises(ValueError):helper.declaration(root,config)
            config={'recipe':'godot-game','artifacts':{'evidence_paths':['tmp/proof'],'evidence_retention_days':{'tmp/unregistered':14}}}
            with self.assertRaises(ValueError):helper.declaration(root,config)
    def test_refuses_symlink_parent_or_nested_payload_without_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tmp/proof').mkdir(parents=True)
            config={'recipe':'godot-game','artifacts':{'evidence_paths':['tmp/proof'],'evidence_retention_days':{'tmp/proof':14}}}
            (root/'tmp/proof/leak').symlink_to(root/'cookbook.toml')
            with self.assertRaises(ValueError):helper.declaration(root,config,inspect=True)
    def test_workers_always_inspect_and_upload_separate_from_success_proof(self):
        tools=Path(__file__).resolve().parents[1]
        for file,count in [('cookbook-godot.yml',3),('cookbook-validation.yml',1)]:
            text=(tools/'.github/workflows'/file).read_text()
            self.assertEqual(text.count('id: native-evidence'),count)
            self.assertEqual(text.count('id: inspect-evidence'),count)
            self.assertEqual(text.count('steps.inspect-evidence.outcome'),count)
            self.assertEqual(text.count('fromJSON(steps.native-evidence.outputs.retention-days)'),count)
