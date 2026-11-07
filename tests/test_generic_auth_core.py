"""Generic auth exports never discover or execute a recipe adapter."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1]/'helpers/auth-boundary.mjs'


@unittest.skipUnless(shutil.which('bun'), 'prepared caller-pinned Bun is required')
class GenericAuthCore(unittest.TestCase):
    def invoke(self, root, expression):
        program = 'const m=await import('+json.dumps(str(HELPER))+'); const root='+json.dumps(str(root))+'; console.log(JSON.stringify('+expression+'));'
        result = subprocess.run(['bun','--eval',program],cwd=root,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_generic_core_keeps_provider_refusals_without_loading_present_recipe_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'cookbook.toml').write_text('invalid recipe data must never be read by a generic tool')
            self.assertEqual(self.invoke(root,'m.checkAuthBoundaryCore(root,{provider:"none",api:false})'),[])
            refused = self.invoke(root,'m.checkAuthBoundaryCore(root,null)')
            self.assertEqual(len(refused),1)
            self.assertEqual(refused[0]['level'],'fail')
            self.assertIn('no auth declaration',refused[0]['text'])

    def test_generic_native_Preview_rule_does_not_invoke_a_Cookbook_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'cookbook.toml').write_text('invalid recipe data must never be read by this generic rule')
            findings = self.invoke(root,'m.previewNamesItsOwnOrigin(root,[],{provider:"clerk",api:true})')
            self.assertEqual(findings,[{'level':'info','text':'no preview workflow; the preview half of the contract does not apply yet'}])


if __name__ == '__main__':
    unittest.main()
