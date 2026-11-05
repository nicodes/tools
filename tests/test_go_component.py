"""Owned Go compiler setup is explicit and all product operations are offline."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cookbook'))
import go_component as go

class GoComponentControls(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        directory=self.root/'deploy/native';directory.mkdir(parents=True)
        for name in ['go.mod','go.sum']:(directory/name).touch()
        (self.root/'mise.toml').write_text('[tools]\ngo="1.27.3"\n')
        self.config={'components':{'go':[{'directory':'deploy/native','package':'example.invalid/native/cmd/server',
            'output':'.artifacts/native/server','test_packages':['example.invalid/native/security/...']}]}}
        self.calls=[]
    def execute(self,argv,cwd,env):
        self.calls.append((argv,cwd,env))
        if 'build' in argv:
            path=Path(argv[argv.index('-o')+1]);path.write_bytes(b'\x7fELF native fixture');path.chmod(0o755)
    def test_only_setup_resolves_dependencies_build_and_tests_keep_the_reviewed_scope_offline(self):
        with patch.object(go.subprocess,'check_output',return_value='go1.27.3\n'):
            go.setup(self.root,self.config,{},self.execute);go.build(self.root,self.config,{},self.execute);go.test(self.root,self.config,{},self.execute)
        self.assertEqual(self.calls[0][0],['go','mod','download'])
        self.assertNotIn('GOPROXY',self.calls[0][2])
        self.assertEqual(self.calls[2][0],['go','test','-mod=readonly','example.invalid/native/security/...'])
        for _,cwd,env in self.calls[1:]:
            self.assertEqual(cwd,self.root/'deploy/native');self.assertEqual(env['GOPROXY'],'off');self.assertEqual(env['GOTOOLCHAIN'],'local')
        self.assertEqual(go.artifact_paths(self.root,self.config),['.artifacts/native/server'])
    def test_wrong_sdk_never_runs_the_compiler_or_repairs_its_pin(self):
        with patch.object(go.subprocess,'check_output',return_value='go9.0.0\n'),self.assertRaisesRegex(ValueError,'pinned Go'):
            go.build(self.root,self.config,{},self.execute)
        self.assertEqual(self.calls,[])
    def test_source_or_environment_authority_cannot_be_replaced(self):
        record=self.config['components']['go'][0]
        for changes in [{'environment':{'GOTOOLCHAIN':'auto'}},{'environment':{'CGO_ENABLED':'1'}},
            {'package':'example.invalid/native;curl'},{'output':'../outside'}, {'test_packages':['native;sh']}]:
            self.config['components']['go']=[record|changes]
            with self.subTest(changes=changes),self.assertRaises(ValueError):go.declaration(self.root,self.config)

if __name__=='__main__':unittest.main()
