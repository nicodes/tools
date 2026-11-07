"""Actual protected-parent preparation precedes generic Tools first writers."""
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest

import test_sdk_bootstrap as bootstrap_controls

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('private_directory_control', ROOT/'helpers/private_directory.py')
private = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(private)


class PrivateDirectoryControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='Private Tools output ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_reviewed_literal_creates_only_missing_private_parent_then_real_policy_accepts(self):
        self.assertEqual(hashlib.sha256(private.SCRIPT.encode()).hexdigest(),
                         'd60eacb11a7b401fd71189acdc82d96f2e222cf11023becafb6af2a85c1c4887')
        original = Path.cwd()
        private.prepare(self.root)
        self.assertEqual(Path.cwd(), original)
        parent = self.root/'.artifacts'
        self.assertTrue(parent.is_dir())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        self.assertEqual(list(parent.iterdir()), [])
        private.protect(self.root)

    def test_execution_guard_never_creates_a_missing_artifact_parent(self):
        private.protect(self.root)
        self.assertFalse((self.root/'.artifacts').exists())

    @unittest.skipIf(os.name == 'nt', 'native Windows owner/DACL controls use actual NTFS policies')
    def test_existing_foreign_writable_parent_refuses_without_permission_repair(self):
        parent = self.root/'.artifacts'
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'not protected'):
            private.prepare(self.root)
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o777)
        self.assertEqual(list(parent.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'link creation may require platform privileges')
    def test_linked_parent_refuses_before_writing_to_its_target(self):
        target = self.root/'foreign'
        target.mkdir()
        (self.root/'.artifacts').symlink_to(target, target_is_directory=True)
        with self.assertRaises(subprocess.CalledProcessError):
            private.prepare(self.root)
        self.assertEqual(list(target.iterdir()), [])

    def test_source_policy_existing_unsafe_parent_refuses_before_native_preparation(self):
        if os.name == 'nt':
            self.skipTest('native Windows DACL mutation remains covered by verification controls')
        spec = importlib.util.spec_from_file_location('source_policy_parent_control', ROOT/'helpers/source_policy.py')
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        (self.root/'mise.toml').write_text('[tools]\npython="'+'.'.join(map(str,sys.version_info[:3]))+'"\n')
        parent = self.root/'.artifacts'
        parent.mkdir()
        parent.chmod(0o777)
        calls = []
        with self.assertRaisesRegex(ValueError, 'not protected'):
            policy.setup(self.root, {}, lambda *args, **kwargs: calls.append((args, kwargs)))
        self.assertEqual(calls, [])
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o777)

    def test_original_full_install_and_native_tasks_follow_python_only_private_preparation(self):
        data = tomllib.loads((ROOT/'mise.toml').read_text())
        run = data['tasks']['install']['run']
        self.assertEqual(run[:3], ['mise install python',
            'python3 -I -B helpers/pinned-tools.py exec --tool python=python3 -- python3 -I -B helpers/private_directory.py prepare',
            'mise install'])
        self.assertEqual(data['tasks']['setup']['run'], [{'task': 'install'}])
        for name in ['MISE_AUTO_INSTALL', 'MISE_EXEC_AUTO_INSTALL', 'MISE_TASK_RUN_AUTO_INSTALL']:
            self.assertEqual(data['env'][name], 'false')
        for name in ['build', 'unit', 'lint', 'test', 'clean', 'check']:
            self.assertNotIn('private_directory.py prepare', str(data['tasks'][name]))
        action = (ROOT/'setup/action.yml').read_text()
        self.assertLess(action.index('Prepare the private artifact parent'), action.index('uses: jdx/mise-action@'))
        self.assertIn("if: runner.os == 'Windows'", action)
        self.assertLess(action.index('id: windows_python'), action.index('Select reviewed SDK binary'))
        self.assertIn("steps.windows_python.outputs.python-path || 'python3'", action)
        self.assertIn('steps.python.outputs.python-path', action)
        self.assertIn('"$BOOTSTRAP_PYTHON" -I -B', action)

    def bootstrap(self):
        control = bootstrap_controls.StandaloneToolsBootstrap()
        control.setUp()
        self.addCleanup(control.doCleanups)
        return control

    def test_actual_setup_with_verified_supplied_source_creates_private_parent_and_runs_original_helper(self):
        control = self.bootstrap()
        result = control.run_bootstrap('setup', 'helpers/probe.py', 'setup literal')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((control.root/'executed').read_text(), 'trusted')
        self.assertTrue((control.root/'.artifacts').is_dir())
        self.assertFalse(control.git('status', '--porcelain', '--untracked-files=all', '--ignored=matching'))

    def test_actual_exec_with_verified_supplied_source_preserves_noncreating_behavior(self):
        control = self.bootstrap()
        result = control.run_bootstrap('exec', 'helpers/probe.py')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((control.root/'.artifacts').exists())

    @unittest.skipIf(os.name == 'nt', 'native Windows DACL refusal is checked by verification controls')
    def test_actual_setup_unsafe_parent_refuses_before_helper_with_original_permissions(self):
        control = self.bootstrap()
        parent = control.root/'.artifacts'
        parent.mkdir()
        parent.chmod(0o777)
        result = control.run_bootstrap('setup', 'helpers/probe.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not protected', result.stderr)
        self.assertFalse((control.root/'executed').exists())
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o777)

    def test_original_seed_statements_and_generic_identity_are_preserved(self):
        tree = ast.parse(bootstrap_controls.bootstrap.SEED)
        setup = next(node for node in tree.body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == "mode == 'setup'")
        script = ast.literal_eval(setup.body[0].value.args[0].elts[-1])
        self.assertEqual(script, 'exec(' + repr(private.SCRIPT) + ')')
        self.assertEqual(hashlib.sha256((ROOT/'helpers/sdk_identity.py').read_bytes()).hexdigest(),
                         'c0baf6fa7bb55afbef62bce160333a154210b04898533e65a68f9c9ad59d4f93')


if __name__ == '__main__':
    unittest.main()
