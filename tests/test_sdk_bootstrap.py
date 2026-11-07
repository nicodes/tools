"""The standalone bootstrap authenticates Tools before any SDK Python loads."""
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import shutil

from native_shell import bash

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bootstrap_control', ROOT/'helpers/sdk-bootstrap.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class StandaloneToolsBootstrap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='Tools bootstrap source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sdk = self.root/'Tools source with spaces'
        (self.sdk/'helpers').mkdir(parents=True)
        for name in ['private_directory.py', 'verification.py', 'source_config.py']:
            shutil.copyfile(ROOT/'helpers'/name, self.sdk/'helpers'/name)
        (self.sdk/'helpers/peer.py').write_text('VALUE="trusted-sibling"\n')
        (self.sdk/'helpers/probe.py').write_text(
            'import json,os,sys\nfrom pathlib import Path\nimport peer\n'
            'Path("executed").write_text("trusted")\n'
            'print(json.dumps({"peer":peer.VALUE,"argv":sys.argv[1:],'
            '"isolated":sys.flags.isolated,"no_bytecode":sys.dont_write_bytecode,'
            '"tools":os.environ["TOOLS_ROOT"],"version":".".join(map(str,sys.version_info[:3])),'
            '"cookbook":os.environ.get("COOKBOOK_ROOT")}))\n')
        self.git('init','-q')
        self.git('remote','add','origin','https://github.com/nicodes/tools.git')
        self.git('add','helpers')
        self.git('-c','user.name=Source control','-c','user.email=source@example.invalid',
                 '-c','commit.gpgsign=false','commit','--quiet','-m','finite Tools source')
        self.ref = self.git('rev-parse','HEAD').strip()
        (self.root/'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="'+self.ref+'"\n')
        self.environment = dict(os.environ, TOOLS_ROOT=str(self.sdk))
        self.environment.pop('COOKBOOK_ROOT', None)

    def git(self, *args):
        return subprocess.check_output(['git','-c','core.hooksPath='+os.devnull,
            '-C',str(self.sdk),*args],text=True,stderr=subprocess.PIPE)

    def run_bootstrap(self, *args, environment=None, pinned_python=False):
        command = bootstrap.command(self.ref, pinned_python=pinned_python)
        return subprocess.run([bash(environment or self.environment),'-euc',command+' '+shlex.join(args)], cwd=self.root,
                              env=environment or self.environment, text=True,
                              capture_output=True, timeout=10)

    def test_clean_exact_Tools_exec_uses_isolated_Python_sibling_import_and_original_argv(self):
        import json
        for name in ['peer.py','sitecustomize.py','tomllib.py']:
            (self.root/name).write_text('raise RuntimeError("caller code must never load")\n')
        result = self.run_bootstrap('exec','helpers/probe.py','argument with spaces','--flag')
        self.assertEqual(result.returncode,0,result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['peer'],'trusted-sibling')
        self.assertEqual(value['argv'],['argument with spaces','--flag'])
        self.assertEqual(value['isolated'],1)
        self.assertTrue(value['no_bytecode'])
        self.assertEqual(value['tools'],str(self.sdk))
        self.assertIsNone(value['cookbook'])
        self.assertFalse(any(self.sdk.rglob('__pycache__')))

    def test_missing_source_exec_and_supplied_setup_refuse_without_fetch_or_execution(self):
        environment = dict(self.environment,TOOLS_ROOT=str(self.root/'absent SDK'))
        for operation in ['exec','setup']:
            result = self.run_bootstrap(operation,'helpers/probe.py',environment=environment)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('absent',result.stderr)
            self.assertFalse((self.root/'absent SDK').exists())
            self.assertFalse((self.root/'executed').exists())

    def test_changed_caller_pin_or_dirty_Tools_refuse_before_loading_helper(self):
        (self.root/'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="'+'a'*40+'"\n')
        result = self.run_bootstrap('exec','helpers/probe.py')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'executed').exists())
        (self.root/'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="'+self.ref+'"\n')
        (self.sdk/'helpers/probe.py').write_text('raise RuntimeError("dirty helper must not load")\n')
        result = self.run_bootstrap('exec','helpers/probe.py')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('exact clean pinned',result.stderr)
        self.assertFalse((self.root/'executed').exists())

    def test_recipe_entrypoint_and_linked_source_are_refused(self):
        result = self.run_bootstrap('exec','cookbook/run.py')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('explicit generic SDK Python entrypoint',result.stderr)
        link = self.root/'linked Tools'
        try:
            link.symlink_to(self.sdk,target_is_directory=True)
        except OSError:
            return  # Native link refusal is covered through path mocks on Windows.
        result = self.run_bootstrap('exec','helpers/probe.py',environment=dict(self.environment,TOOLS_ROOT=str(link)))
        self.assertNotEqual(result.returncode,0)
        self.assertIn('links',result.stderr)
        self.assertFalse((self.root/'executed').exists())

    def test_requested_bootstrap_Python_uses_exact_source_version_despite_environment_override(self):
        (self.root/'mise.toml').write_text('[tools]\npython="3.13.11"\n[vars]\ntools_contract=1\ntools_ref="'+self.ref+'"\n')
        environment = dict(self.environment, MISE_PYTHON_VERSION='0.0.0',
            MISE_TRUSTED_CONFIG_PATHS=str(self.root/'mise.toml'),
            MISE_CEILING_PATHS=str(self.root.parent))
        (self.root/'sitecustomize.py').write_text('raise RuntimeError("cwd must not supply bootstrap code")\n')
        (self.root/'tomllib.py').write_text('raise RuntimeError("caller parser must not load")\n')
        environment['PYTHONPATH'] = str(self.root)
        result = self.run_bootstrap('exec','helpers/probe.py',environment=environment,pinned_python=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'executed').read_text(),'trusted')
        import json
        self.assertEqual(json.loads(result.stdout)['version'],'3.13.11')

    def test_absent_requested_Python_refuses_before_loading_any_Tools_module(self):
        (self.root/'mise.toml').write_text('[tools]\npython="0.0.0"\n[vars]\ntools_contract=1\ntools_ref="'+self.ref+'"\n')
        environment = dict(self.environment, MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false',
            MISE_TASK_RUN_AUTO_INSTALL='false', MISE_TRUSTED_CONFIG_PATHS=str(self.root/'mise.toml'),
            MISE_CEILING_PATHS=str(self.root.parent))
        result = self.run_bootstrap('exec','helpers/probe.py',environment=environment,pinned_python=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'executed').exists())

    def test_actual_installed_Mise_executes_the_unchanged_rendered_command_and_literal_arguments(self):
        import json
        mise = shutil.which('mise')
        self.assertIsNotNone(mise,'existing CI Mise is required; this control never installs it')
        version = subprocess.run([mise,'--version'],text=True,capture_output=True,timeout=5)
        self.assertEqual(version.returncode,0,version.stderr)
        config = self.root/'mise.toml'
        config.write_text('[tools]\npython="3.13.11"\n[vars]\ntools_contract=1\ntools_ref="'+self.ref+'"\n'
            '[settings]\ntask.run_auto_install=false\n[tasks._sdk]\nhide=true\nraw=true\nshell="bash -euc"\nrun='
            +json.dumps(bootstrap.command(self.ref))+'\n')
        environment = dict(self.environment, MISE_CONFIG_DIR=str(self.root/'control/config'),
            MISE_CACHE_DIR=str(self.root/'control/cache'), MISE_STATE_DIR=str(self.root/'control/state'),
            MISE_CEILING_PATHS=str(self.root.parent), MISE_TRUSTED_CONFIG_PATHS=str(self.root), MISE_AUTO_INSTALL='false',
            MISE_EXEC_AUTO_INSTALL='false', MISE_TASK_RUN_AUTO_INSTALL='false')
        result = subprocess.run([mise,'run','--skip-tools','_sdk','exec','helpers/probe.py',
                                 'literal argument with spaces','--literal'],cwd=self.root,env=environment,
                                text=True,capture_output=True,timeout=30)
        print(json.dumps({'actual_Mise':mise,'Mise_version':version.stdout.strip(),'exit':result.returncode,'stdout':result.stdout[:8192],
                          'stderr':result.stderr[:8192]}),flush=True)
        self.assertEqual(result.returncode,0,result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record['argv'],['literal argument with spaces','--literal'])
        self.assertEqual(record['tools'],str(self.sdk))
        self.assertEqual(record['isolated'],1)
        self.assertTrue(record['no_bytecode'])
        self.assertEqual(self.git('status','--porcelain','--untracked-files=all','--ignored=matching').strip(),'')


if __name__ == '__main__':
    unittest.main()
