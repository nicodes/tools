"""Explicit empty ancestry sets retain every public documentation history check."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('documentation_history_policy', ROOT / 'helpers/documentation-policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class DocumentationHistory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='documentation history ')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'public-source'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Documentation control')
        self.git('config', 'user.email', 'documentation-control@example.invalid')
        (self.root / 'README.md').write_text('# Public usage\n', encoding='utf-8')
        self.write_policy('forbidden_commits = []\n')
        self.commit('Create public source with reviewed README')

    def git(self, *arguments, root=None):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        return subprocess.check_output(['git', '--no-replace-objects', '-c', 'core.hooksPath=' + os.devnull,
            '-c', 'core.fsmonitor=false', '-c', 'commit.gpgSign=false', *arguments],
            cwd=root or self.root, env=environment, text=True, stderr=subprocess.PIPE, timeout=15).strip()

    def write_policy(self, history):
        directory = self.root / '.github'
        directory.mkdir(exist_ok=True)
        digest = hashlib.sha256((self.root / 'README.md').read_bytes()).hexdigest()
        body = 'version = 1\nsha256 = "' + digest + '"\n'
        if history is not None:
            body += '[history]\n' + history
        (directory / 'readme-source.toml').write_text(body, encoding='utf-8')

    def commit(self, message):
        self.git('add', '--all')
        self.git('commit', '-q', '-m', message)

    def test_born_public_empty_set_still_reads_complete_history_and_markdown(self):
        calls = []
        original = subprocess.check_output
        def record(command, **options):
            calls.append(command)
            return original(command, **options)
        with patch.object(policy.subprocess, 'check_output', side_effect=record):
            self.assertEqual(policy.check(self.root, history_required=True), [])
        self.assertIn(['git', 'rev-parse', '--is-shallow-repository'], calls)
        self.assertIn(['git', 'rev-list', 'HEAD'], calls)
        scans = [command for command in calls if command[:2] == ['git', 'log']]
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0][2:7], ['--format=', '--name-only', '-z', 'HEAD', '--'])
        self.assertEqual(set(scans[0][7:]), {':(icase)*' + suffix for suffix in policy.MARKDOWN})

    def test_required_boundary_cannot_be_missing_or_malformed(self):
        for history in [None, '', 'forbidden_commits = ""\n', 'forbidden_commits = []\nextra = true\n']:
            with self.subTest(history=history):
                self.write_policy(history)
                self.assertTrue(policy.check(self.root, history_required=True))
        for value in ['"not-an-identity"', '7', 'true', '"' + 'A' * 40 + '"']:
            with self.subTest(identity=value):
                self.write_policy('forbidden_commits = [' + value + ']\n')
                self.assertTrue(policy.check(self.root, history_required=True))

    def test_empty_set_does_not_admit_a_shallow_checkout(self):
        shallow = self.directory / 'shallow'
        self.git('clone', '-q', '--depth', '1', self.root.as_uri(), str(shallow), root=self.directory)
        self.assertEqual(self.git('rev-parse', '--is-shallow-repository', root=shallow), 'true')
        errors = policy.check(shallow, history_required=True)
        self.assertTrue(any('fetch-depth: 0' in error for error in errors), errors)

    def test_empty_set_still_refuses_deleted_historical_markdown(self):
        note = self.root / 'PRIVATE.MD'
        note.write_text('# Private historical note\n', encoding='utf-8')
        self.commit('Record a historical extra Markdown file')
        note.unlink()
        self.commit('Remove the historical extra Markdown file')
        errors = policy.check(self.root, history_required=True)
        self.assertIn('historical Markdown must remain in private docs: PRIVATE.MD', errors)

    def test_nonempty_forbidden_set_retains_exact_ancestor_refusal(self):
        ancestor = self.git('rev-parse', 'HEAD')
        self.write_policy('forbidden_commits = ["' + ancestor + '"]\n')
        errors = policy.check(self.root, history_required=True)
        self.assertTrue(any('original history was reintroduced' in error for error in errors), errors)
        self.write_policy('forbidden_commits = ["' + 'f' * 40 + '"]\n')
        self.assertEqual(policy.check(self.root, history_required=True), [])

    def test_empty_set_does_not_change_current_layout_or_digest_checks(self):
        (self.root / 'README.md').write_text('# Unreviewed usage\n', encoding='utf-8')
        errors = policy.check(self.root, history_required=True)
        self.assertTrue(any('README differs from its reviewed publication' in error for error in errors), errors)
        self.write_policy('forbidden_commits = []\n')
        (self.root / 'extra.md').write_text('# Extra\n', encoding='utf-8')
        self.git('add', 'extra.md')
        errors = policy.check(self.root, history_required=True)
        self.assertIn('documentation must live in the private docs repository: extra.md', errors)

    def test_existing_tools_public_boundary_is_nonempty_and_unchanged(self):
        data = policy.tomllib.loads((ROOT / '.github/readme-source.toml').read_text())
        self.assertTrue(data['history']['forbidden_commits'])
        self.assertEqual(policy.check(ROOT, history_required=True), [])


if __name__ == '__main__':
    unittest.main()
