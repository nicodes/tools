"""Keep the temporary bridge's legacy and Cookbook source authorities distinct."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cookbook'))
import namespace_bridge as bridge
import request_merge
import ci
import generate
import test_request_merge as merge_fixture


class NamespaceBridge(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='Namespace bridge source ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = merge_fixture.RequestedMerge()
        self.fixture.setUp()

    def commit(self, root, repository):
        for argv in [('init', '-q'), ('remote', 'add', 'origin', 'https://github.com/' + repository + '.git'),
                ('add', '.'), ('-c', 'user.name=Namespace control', '-c', 'user.email=control@example.invalid',
                             'commit', '-qm', 'finite source fixture')]:
            subprocess.run(['git', '-C', str(root), *argv], check=True)
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()

    def modern(self, record):
        return {**record, 'cookbook_repository': 'nicodes/cookbook',
            'tools_repository': 'nicodes/tools', 'tools_commit': 'f' * 40, 'entrypoint_contract': 'mise-v1'}

    def test_genuine_legacy_receipts_keep_the_old_abi_but_cannot_accept_modern_or_partial_proofs(self):
        f = self.fixture
        request_merge.workflow_acceptance(f.receipt, f.proof, f.run, f.jobs, f.environment)
        request_merge.workflow_acceptance(f.receipt, f.proof | {'entrypoint_contract': 'make-v0'}, f.run, f.jobs, f.environment)
        for changed in [self.modern(f.proof), f.proof | {'entrypoint_contract': None},
                f.proof | {'entrypoint_contract': 'unknown'}, f.proof | {'cookbook_repository': 'nicodes/cookbook'},
                f.proof | {'tools_commit': 'f' * 40}]:
            with self.subTest(proof=changed), patch.object(bridge, 'invoke') as invoke, self.assertRaises(ValueError):
                request_merge.workflow_acceptance(f.receipt, changed, f.run, f.jobs, f.environment)
            invoke.assert_not_called()

    def test_partial_or_command_task_authority_is_never_legacy_sdk_only_data(self):
        self.assertFalse(bridge.declared('[tools]\npython="3.13.11"\n'))
        bridge.legacy_source('[tools]\npython="3.13.11"\n')
        for source in ['[vars]\ncookbook_contract=1\n', '[vars]\ntools_contract=1\n',
                '[tasks.custom]\nrun="must never execute"\n', '[settings]\ntask.run_auto_install=true\n']:
            with self.subTest(source=source):
                self.assertTrue(bridge.declared(source))
                with self.assertRaises(ValueError):
                    bridge.legacy_source(source)
        for record in [{}, {'entrypoint_contract': 'mise-v1'}, {'entrypoint_contract': None}]:
            if record:
                with self.assertRaises(ValueError):
                    bridge.protocol(record)
            else:
                self.assertEqual(bridge.protocol(record), 'make-v0')

    def test_modern_snapshot_forces_original_head_base_and_preserves_source_tree_selection(self):
        f = self.fixture
        commit = f.commit | {'sha': 'c' * 40, 'parents': [{'sha': 'a' * 40}, {'sha': 'b' * 40}]}
        expected = request_merge.candidate.snapshot('owner/project', f.pull, commit)
        result = self.modern(expected) | {'cookbook_commit': 'e' * 40}
        for changed in [{}, {'tree': '9' * 40}, {'candidate': '9' * 40}]:
            with self.subTest(changed=changed), patch.object(request_merge, 'api', side_effect=[f.pull, commit]), \
                    patch.object(request_merge, 'source_file', return_value='[vars]\ncookbook_contract=1\n') as source, \
                    patch.object(bridge, 'invoke', return_value=result | changed) as invoke, \
                    patch.object(request_merge, 'status') as status:
                if changed:
                    with self.assertRaises(ValueError):
                        request_merge.snapshot('owner/project', 7)
                else:
                    self.assertEqual(request_merge.snapshot('owner/project', 7), result)
                source.assert_called_once_with('owner/project', 'c' * 40, 'mise.toml')
                invoke.assert_called_once_with('snapshot', 'owner/project', 7, 'b' * 40, 'a' * 40)
                status.assert_not_called()

    def sdk_pair(self):
        tools = self.root / 'Tools helper source'
        tools.mkdir()
        (tools / 'never_execute.py').write_text('raise RuntimeError("lower source must not execute")\n')
        tools_ref = self.commit(tools, 'nicodes/tools')
        cookbook = self.root / 'Cookbook template source'
        (cookbook / 'cookbook').mkdir(parents=True)
        (cookbook / 'mise.toml').write_text('[vars]\ntools_contract=1\ntools_ref="' + tools_ref + '"\n')
        (cookbook / 'cookbook/configuration.py').write_text('SOURCE="checked Cookbook sibling"\n')
        # This unit seam replaces only the admitted template's controller body;
        # the actual namespace/source guards and isolated native process run.
        (cookbook / 'cookbook/request_merge.py').write_text(
            'import configuration,os,sys\n'
            'assert configuration.SOURCE=="checked Cookbook sibling"\n'
            'assert sys.flags.isolated and sys.dont_write_bytecode\n'
            'def workflow_acceptance(receipt,proof,run,jobs,environment):\n'
            ' assert receipt["cookbook_commit"]==proof["cookbook_commit"]\n'
            ' assert receipt["tools_commit"]==proof["tools_commit"]\n'
            ' return {"selected_C":receipt["cookbook_commit"],"selected_T":receipt["tools_commit"],'
            '"running_C0":os.environ["COOKBOOK_REF"],"running_T0":os.environ["TOOLS_REF"]}\n')
        cookbook_ref = self.commit(cookbook, 'nicodes/cookbook')
        return cookbook, cookbook_ref, tools, tools_ref

    def test_real_isolated_template_child_keeps_actual_C1_T1_separate_from_running_C0_T0(self):
        cookbook, cookbook_ref, tools, tools_ref = self.sdk_pair()
        f = self.fixture
        receipt, proof = self.modern(f.receipt), self.modern(f.proof)
        (self.root / 'configuration.py').write_text('raise RuntimeError("foreign cwd module")\n')
        environment = {'COOKBOOK_ROOT': str(cookbook), 'COOKBOOK_REF': cookbook_ref,
            'TOOLS_ROOT': str(tools), 'TOOLS_REF': tools_ref, 'PYTHONPATH': str(self.root)}
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(bridge, 'COOKBOOK_TEMPLATE', cookbook_ref), patch.dict(os.environ, environment):
                result = request_merge.workflow_acceptance(receipt, proof, f.run, f.jobs, f.environment)
                self.assertEqual(result, {'selected_C': 'e' * 40, 'selected_T': 'f' * 40,
                    'running_C0': cookbook_ref, 'running_T0': tools_ref})
                self.assertEqual(bridge.prepared(), (cookbook, tools, tools_ref))
        finally:
            os.chdir(previous)

    def test_dirty_foreign_or_aliased_prepared_sources_refuse_before_controller_code(self):
        cookbook, cookbook_ref, tools, tools_ref = self.sdk_pair()
        with patch.object(bridge, 'COOKBOOK_TEMPLATE', cookbook_ref), patch.dict(os.environ,
                {'COOKBOOK_ROOT': str(cookbook), 'COOKBOOK_REF': cookbook_ref,
                 'TOOLS_ROOT': str(tools), 'TOOLS_REF': tools_ref}):
            for directory, origin in [(cookbook, 'nicodes/tools'), (tools, 'nicodes/cookbook')]:
                subprocess.run(['git', '-C', str(directory), 'remote', 'set-url', 'origin', 'https://github.com/' + origin + '.git'], check=True)
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    bridge.prepared()
                actual = 'nicodes/cookbook' if directory == cookbook else 'nicodes/tools'
                subprocess.run(['git', '-C', str(directory), 'remote', 'set-url', 'origin', 'https://github.com/' + actual + '.git'], check=True)
            (cookbook / 'untracked.py').write_text('raise RuntimeError("must not execute")\n')
            with self.assertRaises(ValueError):
                bridge.invoke('workflow_acceptance', {}, {}, {}, {}, {})

    def test_actual_legacy_native_identity_refuses_task_bearing_mise_with_unchanged_make(self):
        tools = self.root / 'legacy Tools SDK'
        tools.mkdir()
        (tools / 'sdk.txt').write_text('genuine legacy identity\n')
        tools_ref = self.commit(tools, 'nicodes/tools')
        app = self.root / 'legacy caller'
        (app / 'tests').mkdir(parents=True)
        (app / 'tests/check.py').write_text('raise RuntimeError("source must not execute")\n')
        (app / 'cookbook.toml').write_text('version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n')
        (app / 'mise.toml').write_text('[tools]\npython="3.13.11"\n')
        (app / 'Makefile').write_text(generate.makefile(app, tools_ref))
        for name, body in generate.workflow_files(app, tools_ref).items():
            path = app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        source = self.commit(app, 'owner/project')
        with patch.object(ci, 'TOOLS', tools):
            self.assertEqual(ci.identity(app, source, tools_ref), ci.git(app, 'rev-parse', 'HEAD^{tree}'))
            before = (app / 'Makefile').read_bytes()
            (app / 'mise.toml').write_text('[tools]\npython="3.13.11"\n[tasks.injected]\nrun="must never execute"\n')
            subprocess.run(['git', '-C', str(app), 'add', 'mise.toml'], check=True)
            subprocess.run(['git', '-C', str(app), '-c', 'user.name=Namespace control', '-c', 'user.email=control@example.invalid',
                            'commit', '-qm', 'finite counterfactual'], check=True)
            source = ci.git(app, 'rev-parse', 'HEAD')
            self.assertEqual((app / 'Makefile').read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'original Mise SDK authority'):
                ci.identity(app, source, tools_ref)
        self.assertEqual(generate.task_command('test'), ['make', 'test'])


if __name__ == '__main__':
    unittest.main()
