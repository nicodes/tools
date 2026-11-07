"""Native SDK children retain argv boundaries, completion, status and isolation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import unittest

import test_sdk_bootstrap as original_controls
from native_shell import bash

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_child_bootstrap', ROOT / 'helpers/sdk-bootstrap.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
IDENTITY_SPEC = importlib.util.spec_from_file_location('native_child_identity', ROOT / 'helpers/sdk_identity.py')
identity = importlib.util.module_from_spec(IDENTITY_SPEC)
IDENTITY_SPEC.loader.exec_module(identity)


class BootstrapChild(unittest.TestCase):
    def setUp(self):
        self.fixture = original_controls.StandaloneToolsBootstrap(
            'test_clean_exact_Tools_exec_uses_isolated_Python_sibling_import_and_original_argv')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        (self.fixture.sdk / 'helpers/status.py').write_text('''import atexit,json,sys,time
from pathlib import Path
import peer
atexit.register(lambda:Path("child-finalized").write_text("complete"))
print(json.dumps({"argv":sys.argv[1:],"peer":peer.VALUE,"isolated":sys.flags.isolated,"no_bytecode":sys.dont_write_bytecode}),flush=True)
time.sleep(0.05)
if sys.argv[1]=="exception":raise RuntimeError("native child exception control")
raise SystemExit(23)
''')
        (self.fixture.sdk / 'helpers/native_signal.py').write_text('''import json,os,signal
print(json.dumps({"pid":os.getpid()}),flush=True)
os.kill(os.getpid(),signal.SIGTERM)
''')
        self.fixture.git('add', 'helpers')
        self.fixture.git('-c', 'user.name=Source control', '-c', 'user.email=source@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'finite native child controls')
        self.fixture.ref = self.fixture.git('rev-parse', 'HEAD').strip()
        self.write_config()
        self.arguments = ['literal argument with spaces', 'literal"quote', 'trailing\\']

    def write_config(self, *, task=False):
        body = '[vars]\ntools_contract=1\ntools_ref="' + self.fixture.ref + '"\n'
        if task:
            body = '[tools]\npython="3.13.11"\n' + body
            body += ('[settings]\ntask.run_auto_install=false\n[tasks._sdk]\nhide=true\nraw=true\nshell="bash -euc"\nrun='
                     + json.dumps(bootstrap.command(self.fixture.ref)) + '\n')
        (self.fixture.root / 'mise.toml').write_text(body)

    def assert_child(self, result, mode):
        self.assertEqual(result.returncode, 23 if mode == 'exit23' else 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['argv'], [mode, *self.arguments])
        self.assertEqual(value['peer'], 'trusted-sibling')
        self.assertEqual(value['isolated'], 1)
        self.assertTrue(value['no_bytecode'])
        self.assertEqual((self.fixture.root / 'child-finalized').read_text(), 'complete')
        self.assertFalse(any(self.fixture.sdk.rglob('__pycache__')))
        self.assertEqual(self.fixture.git('status', '--porcelain', '--untracked-files=all', '--ignored=matching').strip(), '')
        if mode == 'exception':
            self.assertIn('RuntimeError: native child exception control', result.stderr)
        self.assertNotIn('SyntaxError', result.stderr)
        (self.fixture.root / 'child-finalized').unlink()

    def test_real_helper_exit23_and_exception_propagate_after_completion(self):
        records = []
        for mode in ['exit23', 'exception']:
            result = self.fixture.run_bootstrap('exec', 'helpers/status.py', mode, *self.arguments)
            self.assert_child(result, mode)
            records.append({'mode': mode, 'exit': result.returncode, 'completed': True, 'argv_exact': True})
        print(json.dumps({'native_bootstrap_children': records}), flush=True)

    def test_actual_installed_Mise_preserves_native_child_exit23_and_exception_status(self):
        self.write_config(task=True)
        mise = shutil.which('mise')
        self.assertIsNotNone(mise, 'existing CI Mise is required; this control never installs it')
        environment = dict(self.fixture.environment,
            MISE_CONFIG_DIR=str(self.fixture.root / 'control/config'),
            MISE_CACHE_DIR=str(self.fixture.root / 'control/cache'),
            MISE_STATE_DIR=str(self.fixture.root / 'control/state'),
            MISE_CEILING_PATHS=str(self.fixture.root.parent), MISE_TRUSTED_CONFIG_PATHS=str(self.fixture.root),
            MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false', MISE_TASK_RUN_AUTO_INSTALL='false')
        records = []
        for mode in ['exit23', 'exception']:
            result = subprocess.run([mise, 'run', '--skip-tools', '_sdk', 'exec', 'helpers/status.py', mode, *self.arguments],
                cwd=self.fixture.root, env=environment, capture_output=True, text=True, timeout=30)
            self.assert_child(result, mode)
            records.append({'mode': mode, 'exit': result.returncode, 'completed': True, 'argv_exact': True})
        print(json.dumps({'actual_Mise_children': records}), flush=True)

    def test_native_execv_and_argv_wait_counterfactual_are_observed(self):
        argv = identity.python_entry(sys.executable, self.fixture.sdk / 'helpers/status.py', 'exit23', *self.arguments)
        records = []
        for name, launch in [('original-execv', 'os.execv(argv[0],argv)'),
                             ('waited-argv', 'raise SystemExit(subprocess.run(argv).returncode)')]:
            parent = 'import json,os,subprocess,sys;argv=json.loads(sys.argv[1]);' + launch
            result = subprocess.run([sys.executable, '-I', '-B', '-c', parent, json.dumps(argv)],
                cwd=self.fixture.root, env=self.fixture.environment, capture_output=True, text=True, timeout=10)
            records.append({'api': name, 'exit': result.returncode,
                'stdout': result.stdout[:1024], 'stderr': result.stderr[:1024]})
            if name == 'waited-argv' or os.name != 'nt':
                self.assert_child(result, 'exit23')
        print(json.dumps({'native_child_api_comparison': records}), flush=True)

    @unittest.skipIf(os.name == 'nt', 'POSIX exec replacement and signals use the POSIX process API')
    def test_posix_exec_keeps_process_identity_and_native_signal_status(self):
        command = bootstrap.command(self.fixture.ref) + ' exec helpers/native_signal.py'
        with subprocess.Popen([bash(self.fixture.environment), '-euc', command], cwd=self.fixture.root,
                env=self.fixture.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGTERM, stderr)
            self.assertEqual(json.loads(stdout)['pid'], child.pid)


if __name__ == '__main__':
    unittest.main()
