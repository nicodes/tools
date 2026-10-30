import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPER=Path(__file__).resolve().parents[1]/'helpers/verification.py'
spec=importlib.util.spec_from_file_location('verification',HELPER)
verification=importlib.util.module_from_spec(spec);spec.loader.exec_module(verification)


class Evidence(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous=Path.cwd();self.addCleanup(os.chdir,previous)
        self.root=Path(temporary.name);os.chdir(self.root)
        subprocess.run(['git','init','-q'],check=True)
        Path('source.txt').write_text('original source\n')
        Path('mise.toml').write_text('[tools]\n')
        subprocess.run(['git','add','.'],check=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.com',
                        '-c','commit.gpgsign=false','commit','-qm','Fixture'],check=True)
        self.output=Path('.artifacts/verification')
        self.declaration=verification.scope(['dist'])
        verification.protected_directory(self.output)

    def python(self,body):
        return [sys.executable,'-c',body]

    def test_build_binds_exact_outputs_and_never_serializes_environment_values(self):
        declaration=verification.scope(['dist'],environment=['PRIVATE_VALUE'])
        with patch.dict(os.environ,PRIVATE_VALUE='synthetic-private-value'):
            proof=verification.run_command('build','build',declaration,self.python(
                'from pathlib import Path;Path("dist").mkdir();Path("dist/app").write_text("compiled artifact")'),self.output)
        value=json.loads((self.output/'build.json').read_text())
        self.assertEqual(value['artifacts'],{'dist/app':verification.sha(Path('dist/app'))})
        self.assertTrue(proof['tracked_source_clean'])
        self.assertIn('mise.toml',proof['configuration_sha256'])
        self.assertNotIn('synthetic-private-value',json.dumps(value))

    def test_modified_source_or_missing_outputs_cannot_leave_a_build_receipt(self):
        for body in ['from pathlib import Path;Path("source.txt").write_text("changed")','pass']:
            Path('source.txt').write_text('original source\n')
            with self.subTest(body=body),self.assertRaises(ValueError):
                verification.run_command('build','build',self.declaration,self.python(body),self.output)
            self.assertFalse((self.output/'build.json').exists())

    def test_linked_artifacts_are_refused(self):
        with self.assertRaisesRegex(ValueError,'links'):
            verification.run_command('build','build',self.declaration,self.python(
                'from pathlib import Path;Path("dist").mkdir();Path("dist/app").symlink_to("../source.txt")'),self.output)
        self.assertFalse((self.output/'build.json').exists())

    def test_local_uncommitted_source_can_be_checked_without_claiming_clean_publication(self):
        Path('source.txt').write_text('local edit\n')
        proof=verification.run_command('test','unit',self.declaration,self.python('pass'),self.output)
        self.assertFalse(proof['tracked_source_clean'])

    def test_revision_and_variant_conflicts_fail_before_running_the_command(self):
        declaration=verification.scope(['dist'],revision_environment=['SOURCE_REVISION'])
        command=self.python('from pathlib import Path;Path("ran").touch()')
        for environment in [{'SOURCE_REVISION':'f'*40},{'BUILD_VARIANT':'fixture'}]:
            with patch.dict(os.environ,environment),self.assertRaises(ValueError):
                verification.run_command('build','build',declaration,command,self.output)
            self.assertFalse(Path('ran').exists())

    def stage(self,name):
        return [sys.executable,str(HELPER),'test','--name',name,'--artifact','dist','--',sys.executable,'-c','pass']

    def command_for(self,names,after=''):
        calls=[self.stage(name) for name in names]
        return self.python('import subprocess;'+''.join('subprocess.run('+repr(call)+',check=True);' for call in calls)+after)

    def test_session_collects_only_tests_from_its_own_source_and_context(self):
        proof=verification.run_session(['unit','integration'],self.command_for(['unit','integration']),self.output)
        self.assertEqual(set(proof['test_receipts']),{'unit','integration'})
        self.assertEqual(proof['tested_commit'],verification.git('rev-parse','HEAD'))
        self.assertEqual(json.loads((self.output/'test.json').read_text()),proof)

    def test_fresh_session_cannot_reuse_prior_partial_or_complete_test_receipts(self):
        verification.run_session(['unit','integration'],self.command_for(['unit','integration']),self.output)
        with self.assertRaisesRegex(ValueError,'every declared test'):
            verification.run_session(['unit','integration'],self.command_for(['unit']),self.output)
        self.assertFalse((self.output/'test.json').exists())

    def test_untracked_source_change_after_tests_invalidates_the_session(self):
        with self.assertRaisesRegex(ValueError,'changed after tests'):
            verification.run_session(['unit'],self.command_for(['unit'],
                'from pathlib import Path;Path("injected.py").write_text("changed source")'),self.output)
        self.assertFalse((self.output/'test.json').exists())

    def test_forged_session_and_other_job_context_never_produce_aggregate_proof(self):
        with patch.dict(os.environ,TOOLS_VERIFICATION_SESSION='.artifacts/verification/sessions/'+'a'*32),self.assertRaises(ValueError):
            verification.run_command('test','unit',self.declaration,self.python('pass'),self.output)
        command=self.python('import os,subprocess;subprocess.run('+repr(self.stage('unit'))+
                            ',env={**os.environ,"GITHUB_JOB":"foreign-job"},check=True)')
        with self.assertRaises(subprocess.CalledProcessError):
            verification.run_session(['unit'],command,self.output)
        self.assertFalse((self.output/'test.json').exists())

    def test_failing_command_clears_the_prior_test_aggregate(self):
        verification.run_session(['unit'],self.command_for(['unit']),self.output)
        with self.assertRaises(subprocess.CalledProcessError):
            verification.run_command('test','unit',self.declaration,self.python('raise SystemExit(7)'),self.output)
        self.assertFalse((self.output/'test.json').exists())

    def test_evidence_does_not_become_an_input_even_without_a_gitignore(self):
        before=verification.identity(self.declaration)
        verification.write(self.output/'unrelated-receipt.json',{'version':1})
        self.assertEqual(verification.identity(self.declaration),before)

    def test_release_metadata_accepts_inputs_and_test_names_without_a_runner_schema(self):
        Path('deploy').mkdir()
        file=Path('deploy/release.json')
        definition={'version':1,'project':'mailroom','image_base':'registry.example/mailroom',
                    'tests':['unit'],'inputs':{'artifacts':['dist'],'images':[],
                    'environment':[],'revision_environment':[]}}
        file.write_text(json.dumps(definition))
        with self.assertRaisesRegex(ValueError,'tracked'):
            verification.release_definition(file)
        subprocess.run(['git','add',str(file)],check=True)
        self.assertEqual(verification.release_definition(file),definition)
        for field,value in [('stages',{'unit':[['echo','untrusted']]}),('tools',{'python':'3.13.11'})]:
            file.write_text(json.dumps({**definition,field:value}))
            with self.assertRaises(ValueError):verification.release_definition(file)
        file.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(ValueError,'duplicate'):verification.release_definition(file)


if __name__=='__main__':
    unittest.main()
