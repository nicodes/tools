"""The documentation policy reads only the checked-out commit and ignores legacy history tables."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'helpers/documentation-policy.py'
SPEC = importlib.util.spec_from_file_location('documentation_policy', SCRIPT)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)

LEGACY = '[history]\nforbidden_commits = ["' + 'a' * 40 + '"]\n'


class DocumentationPolicy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='documentation policy ')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'source'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Documentation control')
        self.git('config', 'user.email', 'documentation-control@example.invalid')
        (self.root / 'README.md').write_text('# Public usage\n', encoding='utf-8')
        self.write_policy('')
        self.commit('Create source')

    def git(self, *arguments, root=None):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        return subprocess.check_output(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgSign=false', *arguments],
            cwd=root or self.root, env=environment, text=True, stderr=subprocess.PIPE, timeout=15).strip()

    def write_policy(self, extra, version='1', digest=None):
        directory = self.root / '.github'
        directory.mkdir(exist_ok=True)
        digest = digest or hashlib.sha256((self.root / 'README.md').read_bytes()).hexdigest()
        (directory / 'readme-source.toml').write_text(
            'version = ' + version + '\nsha256 = "' + digest + '"\n' + extra, encoding='utf-8')

    def commit(self, message):
        self.git('add', '--all')
        self.git('commit', '-q', '-m', message)

    def shallow_clone(self):
        self.git('commit', '-q', '--allow-empty', '-m', 'Second commit')
        clone = self.directory / 'shallow'
        self.git('clone', '-q', '--depth', '1', self.root.as_uri(), str(clone), root=self.directory)
        self.assertEqual(self.git('rev-parse', '--is-shallow-repository', root=clone), 'true')
        return clone

    def run_main(self, root, **environment):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(environment)
        return subprocess.run(['/usr/bin/python3', '-I', '-B', str(SCRIPT)], cwd=root, env=env,
            capture_output=True, text=True, timeout=30)

    def test_depth_one_checkout_with_legacy_history_table_passes(self):
        self.write_policy(LEGACY)
        self.commit('Add legacy history table')
        clone = self.shallow_clone()
        self.assertEqual(policy.check(clone), [])
        self.assertEqual(self.run_main(clone).returncode, 0)

    def test_any_legacy_history_table_shape_is_ignored(self):
        for extra in ['[history]\n', '[history]\nforbidden_commits = ""\n', '[history]\nforbidden_commits = []\nextra = true\n']:
            with self.subTest(extra=extra):
                self.write_policy(extra)
                self.assertEqual(policy.check(self.root), [])

    def test_historical_markdown_and_forbidden_ancestors_are_not_inspected(self):
        note = self.root / 'NOTE.md'
        note.write_text('# Note\n', encoding='utf-8')
        self.commit('Add Markdown')
        note.unlink()
        self.write_policy('[history]\nforbidden_commits = ["' + self.git('rev-parse', 'HEAD') + '"]\n')
        self.commit('Remove Markdown')
        self.assertEqual(policy.check(self.root), [])

    def test_clean_history_requirement_is_ignored_without_a_table(self):
        clone = self.shallow_clone()
        for value in ['true', 'false', '']:
            with self.subTest(value=value):
                result = self.run_main(clone, REQUIRE_CLEAN_HISTORY=value)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bad_readme_digest_still_fails(self):
        self.write_policy(LEGACY, digest='0' * 64)
        errors = policy.check(self.root)
        self.assertTrue(any('README differs from its reviewed publication' in error for error in errors), errors)
        self.assertEqual(self.run_main(self.root, REQUIRE_CLEAN_HISTORY='true').returncode, 1)

    def test_malformed_policy_still_fails(self):
        self.write_policy('', version='2')
        self.assertTrue(any('unsupported README source policy' in error for error in policy.check(self.root)))

    def test_stray_markdown_still_fails(self):
        (self.root / 'extra.md').write_text('# Extra\n', encoding='utf-8')
        self.git('add', 'extra.md')
        self.assertIn('documentation must live in the private docs repository: extra.md', policy.check(self.root))

    def test_tools_own_policy_passes_in_a_single_commit_checkout(self):
        for name in ['README.md', '.github/readme-source.toml']:
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        self.assertEqual(policy.check(self.root), [])


if __name__ == '__main__':
    unittest.main()
