"""Platform CI gates use real changed paths without weakening complete CI."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ci_paths', ROOT / '.github/scripts/plan-ci.py')
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class CIPaths(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tools ci paths ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'CI selection control')
        self.git('config', 'user.email', 'ci-selection@example.invalid')
        for name in ['README.md', 'setup/action.yml', 'podman-ci/action.yml']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('initial\n')
        self.base = self.commit()

    def git(self, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        return subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'core.fsmonitor=false', '-c', 'commit.gpgSign=false', *arguments],
            cwd=self.root, env=environment, text=True, stderr=subprocess.PIPE, timeout=15).strip()

    def commit(self):
        self.git('add', '--all')
        self.git('commit', '-q', '-m', 'Record selection fixture')
        return self.git('rev-parse', 'HEAD')

    def changed(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('changed\n')
        return self.commit()

    def pull_request(self, head, base=None):
        return ci.selection('pull_request', {'pull_request': {'base': {'sha': base or self.base},
            'head': {'sha': head}}}, 'refs/pull/1/merge', self.root)

    def test_actual_path_groups_and_workflow_changes_select_required_platforms(self):
        for name, expected in [
            ('README.md', dict(composite=False, ubuntu=False, windows=False)),
            ('podman-ci/action.yml', dict(composite=True, ubuntu=False, windows=False)),
            ('setup/action.yml', dict(composite=False, ubuntu=True, windows=True)),
            ('helpers/sdk_identity.py', dict(composite=False, ubuntu=False, windows=True)),
            ('.github/workflows/build-and-test.yml', dict(composite=True, ubuntu=True, windows=True)),
            ('.github/scripts/plan-ci.py', dict(composite=True, ubuntu=True, windows=True)),
        ]:
            with self.subTest(path=name):
                self.git('checkout', '-q', '--detach', self.base)
                self.assertEqual(self.pull_request(self.changed(name)), expected)

    def test_manual_and_merge_queue_run_every_platform_control(self):
        for event in ['workflow_dispatch', 'merge_group']:
            self.assertEqual(ci.selection(event, {}, 'refs/heads/main', self.root),
                dict(composite=True, ubuntu=True, windows=True))

    def test_main_push_uses_the_exact_two_commit_range(self):
        head = self.changed('README.md')
        self.assertEqual(ci.selection('push', {'before': self.base, 'after': head}, 'refs/heads/main', self.root),
            dict(composite=False, ubuntu=False, windows=False))
        self.assertEqual(ci.selection('push', {'before': '0' * 40, 'after': head}, 'refs/heads/main', self.root),
            dict(composite=True, ubuntu=True, windows=True))

    def test_PR_scope_uses_merge_base_instead_of_unrelated_new_base_changes(self):
        advanced_base = self.changed('setup/action.yml')
        self.git('checkout', '-q', '--detach', self.base)
        head = self.changed('README.md')
        self.assertEqual(self.pull_request(head, advanced_base), dict(composite=False, ubuntu=False, windows=False))

    def test_deleted_and_renamed_platform_inputs_cannot_escape_path_gates(self):
        self.git('rm', '-q', 'setup/action.yml')
        self.assertEqual(self.pull_request(self.commit()), dict(composite=False, ubuntu=True, windows=True))
        self.git('checkout', '-q', '--detach', self.base)
        self.git('mv', 'podman-ci/action.yml', 'moved.yml')
        self.assertEqual(self.pull_request(self.commit()), dict(composite=True, ubuntu=False, windows=False))

    def test_invalid_event_and_source_identity_fail_closed(self):
        for event, ref in [('push', 'refs/heads/feature'), ('pull_request_target', 'refs/heads/main')]:
            with self.subTest(event=event), self.assertRaisesRegex(ValueError, 'unsupported CI event'):
                ci.selection(event, {}, ref, self.root)
        for source in ['main', '0' * 40, 'A' * 40, None]:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'exact nonzero source'):
                self.pull_request(source)


if __name__ == '__main__':
    unittest.main()
