import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'workflow-library'/f'{name}.py')
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
contract=module('workflow_contract');operation=module('workflow_operation')

class CatalogueTests(unittest.TestCase):
    def fixture(self, root):
        github=root/'.github';(github/'workflows').mkdir(parents=True)
        names=['workflow_contract.py','workflow_operation.py','pins_compat.mjs']
        for name in names:(github/name).write_bytes((ROOT/'workflow-library'/name).read_bytes())
        (github/'workflow-contract.json').write_text(json.dumps({'version':1,'kind':'tool','required':['ci.yml'],'ci_gates':['Test'],'engine_sha256':{name:hashlib.sha256((github/name).read_bytes()).hexdigest() for name in names}}))
        (github/'workflows/ci.yml').write_text("name: CI\non:\n  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n  workflow_dispatch:\njobs:\n  test:\n    name: Test\n    runs-on: ubuntu-24.04\n    steps:\n      - run: make test\n")
    def test_missing_renamed_or_extra_workflow_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root);contract.validate(root)
            ci=root/'.github/workflows/ci.yml';ci.rename(ci.with_name('renamed.yml'))
            with self.assertRaises(ValueError):contract.validate(root)
    def test_engine_drift_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root)
            (root/'.github/workflow_operation.py').write_text('print("changed")')
            with self.assertRaises(ValueError):contract.validate(root)
    def test_privileged_head_checkout_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);self.fixture(root);ci=root/'.github/workflows/ci.yml'
            ci.write_text(ci.read_text().replace('  workflow_dispatch:', '  pull_request_target:\n  workflow_dispatch:').replace('      - run: make test','      - uses: actions/checkout@'+'a'*40+'\n        with:\n          ref: ${{ github.event.pull_request.head.sha }}'))
            with self.assertRaises(ValueError):contract.validate(root)
    def test_duplicate_yaml_keys_fail(self):
        with self.assertRaises(ValueError):contract.yaml.load('name: CI\nname: CD',Loader=contract.Loader)
    def test_generated_copy_matches_library(self):
        policy=json.loads((ROOT/'.github/workflow-contract.json').read_text())
        for name,digest in policy['engine_sha256'].items():
            self.assertEqual((ROOT/'workflow-library'/name).read_bytes(),(ROOT/'.github'/name).read_bytes())
            self.assertEqual(hashlib.sha256((ROOT/'.github'/name).read_bytes()).hexdigest(),digest)

class RecoveryProvenanceTests(unittest.TestCase):
    def fixtures(self):
        run={'id':11,'repository':{'full_name':'owner/repo'},'head_repository':{'full_name':'owner/repo'},'head_branch':'main','conclusion':'success','status':'completed','path':'.github/workflows/backup.yml','event':'schedule'}
        artifact={'id':22,'expired':False,'workflow_run':{'id':11},'name':'backup-11-1','digest':'sha256:'+'a'*64}
        return run,artifact
    def test_valid_and_invalid_sources(self):
        run,artifact=self.fixtures();operation.provenance(run,artifact,'owner/repo','11','22')
        changes={'head_branch':'feature','conclusion':'failure','status':'in_progress','path':'.github/workflows/ci.yml','event':'pull_request_target','head_repository':{'full_name':'fork/repo'},'id':12}
        for key,value in changes.items():
            with self.subTest(key=key),self.assertRaises(ValueError):operation.provenance({**run,key:value},artifact,'owner/repo','11','22')
        changes={'expired':True,'workflow_run':{'id':12},'name':'other','digest':'','id':23}
        for key,value in changes.items():
            with self.subTest(key=key),self.assertRaises(ValueError):operation.provenance(run,{**artifact,key:value},'owner/repo','11','22')
    def test_immutable_ids_required(self):
        for value in ['0','-1','latest','1;sh','01','']:
            with self.subTest(value=value),self.assertRaises(ValueError):operation.positive(value)
if __name__=='__main__':unittest.main()

class SnapshotBootstrapTests(unittest.TestCase):
    def test_verified_inventory_rejects_changed_or_extra_files(self):
        bootstrap=module('engineering_bootstrap')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'helpers').mkdir();(root/'tests').mkdir()
            helper=root/'helpers/watch-tools.py';helper.write_text('reviewed helper')
            manifest=root/'SOURCE.json';manifest.write_text(json.dumps({'repository':'https://github.com/nicodes/cicd','revision':'a'*40,'files':{'helpers/watch-tools.py':hashlib.sha256(helper.read_bytes()).hexdigest()}}))
            digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
            self.assertTrue(bootstrap.verify(root,'a'*40,digest))
            self.assertFalse(bootstrap.verify(root,'b'*40,digest))
            self.assertFalse(bootstrap.verify(root,'a'*40,'b'*64))
            helper.write_text('changed helper')
            self.assertFalse(bootstrap.verify(root,'a'*40,digest))
            helper.write_text('reviewed helper');(root/'tests/extra.py').write_text('extra')
            self.assertFalse(bootstrap.verify(root,'a'*40,digest))
