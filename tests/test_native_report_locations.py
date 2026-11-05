"""Retained native reports stay outside tracked source and broad upload roots."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('report_locations', TOOLS / 'helpers/native-diagnostics.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class NativeReportLocations(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'game').mkdir()
        (self.root / 'game/project.godot').write_text('config_version=5\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'game/project.godot'], check=True)

    def config(self, path, days=14):
        return {'recipe': 'godot-game', 'components': {'godot': {'project': 'game'}},
                'artifacts': {'evidence_paths': [path], 'evidence_retention_days': {path: days}}}

    def test_real_failure_reader_retains_only_registered_owned_reports(self):
        for name in ('release-reports/browser', 'game/build/browser-report'):
            with self.subTest(name=name):
                target = self.root / name
                target.mkdir(parents=True)
                (target / 'failure.json').write_text('{"passed":false}')
                (self.root / 'cookbook.toml').write_text(
                    'recipe="godot-game"\n[components.godot]\nproject="game"\n[artifacts]\n'
                    f'evidence_paths=["{name}"]\nevidence_retention_days={{"{name}"=14}}\n')
                output = self.root / 'outputs'
                output.unlink(missing_ok=True)
                identity = hashlib.sha256((self.root / 'cookbook.toml').read_bytes()).hexdigest()
                subprocess.run([sys.executable, str(TOOLS / 'helpers/native-diagnostics.py'), '--root', str(self.root),
                    '--inspect', '--expected-sha256', identity, '--status', 'failure'],
                    env=dict(os.environ, GITHUB_OUTPUT=str(output)), check=True, timeout=10)
                result = output.read_text()
                self.assertIn(name, result)
                self.assertIn('retention-days-1=14', result)
                self.assertNotIn('game/project.godot', result)

    def test_broad_roots_other_project_and_unregistered_targets_refuse(self):
        for name in ('release-reports', 'game/build', 'game', 'other/build/report', 'game/src/report',
                     'release-reports/../game', 'release-reports/browser/**', 'game/build/report\nother'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                helper.declaration(self.root, self.config(name))
        config = self.config('release-reports/browser')
        config['artifacts']['evidence_paths'] = []
        with self.assertRaises(ValueError):
            helper.declaration(self.root, config)

    def test_tracked_report_tree_refuses_before_make_or_upload(self):
        target = self.root / 'release-reports/browser'
        target.mkdir(parents=True)
        (target / 'private-source.py').write_text('source = True\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'release-reports/browser/private-source.py'], check=True)
        for inspect in (False, True):
            with self.subTest(inspect=inspect), self.assertRaisesRegex(ValueError, 'tracked source'):
                helper.declaration(self.root, self.config('release-reports/browser'), inspect=inspect)

    def test_parent_target_and_nested_links_refuse(self):
        outside = self.root / 'private'
        outside.mkdir()
        report = self.root / 'release-reports'
        report.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'links'):
            helper.declaration(self.root, self.config('release-reports/browser'))
        report.unlink()
        report.mkdir()
        target = report / 'browser'
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'links'):
            helper.declaration(self.root, self.config('release-reports/browser'))
        target.unlink()
        target.mkdir()
        (target / 'private').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unlinked'):
            helper.declaration(self.root, self.config('release-reports/browser'), inspect=True)

    def test_owned_build_report_requires_real_project_and_actual_checkout(self):
        (self.root / 'game/project.godot').unlink()
        with self.assertRaisesRegex(ValueError, 'owned Godot project'):
            helper.declaration(self.root, self.config('game/build/report'))
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'actual source checkout'):
            helper.declaration(Path(directory), self.config('release-reports/browser'))
        (self.root / 'untracked').mkdir()
        (self.root / 'untracked/project.godot').write_text('config_version=5\n')
        config = self.config('untracked/build/report')
        config['components']['godot']['project'] = 'untracked'
        with self.assertRaisesRegex(ValueError, 'actual source checkout'):
            helper.declaration(self.root, config)

    def test_root_project_build_report_is_narrow_and_caller_bound(self):
        (self.root / 'project.godot').write_text('config_version=5\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'project.godot'], check=True)
        config = self.config('build/browser-report', 7)
        config['components']['godot']['project'] = '.'
        self.assertEqual(helper.declaration(self.root, config), [{'days': 7, 'paths': ['build/browser-report']}])
        config['components']['godot']['project'] = 'game'
        with self.assertRaises(ValueError):
            helper.declaration(self.root, config)


if __name__ == '__main__':
    unittest.main()
