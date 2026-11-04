import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('playwright_local', Path(__file__).resolve().parents[1] / 'helpers/playwright-local.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class LocalBrowser(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='local browser ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        package = self.root / 'tests/browser'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'dependencies': {'playwright': '1.63.0-alpha-2026-08-05', '@playwright/cli': '0.1.18'}}))
        (package / 'package-lock.json').write_text('{}')
        binaries = package / 'node_modules/.bin'
        binaries.mkdir(parents=True)
        for name in ['playwright', 'playwright-cli']:
            (binaries / name).touch()
        self.chromium = self.root / 'chromium'
        self.chromium.touch()
        self.commands = []

    def fake_run(self, argv, **kwargs):
        self.commands.append((argv, kwargs))
        output = '0.1.18\n' if argv[-1] == '--version' else str(self.chromium) + '\n'
        return subprocess.CompletedProcess(argv, 0, stdout=output)

    def test_setup_uses_only_the_locked_local_sdk_and_pinned_chromium(self):
        with patch.dict('os.environ', {'CI': 'true'}, clear=False):
            native.prepare(self.root, 'tests/browser', cli_version='0.1.18', run=self.fake_run)
        self.assertEqual(self.commands[0][0], [str(self.root / 'tests/browser/node_modules/.bin/playwright'), 'install', 'chromium', '--with-deps'])
        config = json.loads((self.root / '.artifacts/browser-config.json').read_text())
        self.assertEqual(config['browser']['launchOptions']['executablePath'], str(self.chromium))
        self.assertTrue(config['browser']['launchOptions']['headless'])
        launcher = (self.root / '.artifacts/playwright-cli').read_text()
        self.assertIn('"$@"', launcher)
        self.assertNotIn('npx', launcher)

    def test_cli_adds_config_only_to_open_without_reinterpreting_arguments(self):
        native.prepare(self.root, 'tests/browser', cli_version='0.1.18', run=self.fake_run)
        for args in [['-s=x', 'open', 'http://127.0.0.1/a b'], ['run-code', 'page => `literal $value`']]:
            with patch.object(native.os, 'execvpe') as execute:
                native.cli(self.root, 'tests/browser', args)
            actual = execute.call_args.args[1]
            self.assertEqual(actual[1:1+len(args)], args)
            self.assertEqual('--config' in actual, 'open' in args)
            self.assertFalse(any('install' == value for value in actual))

    def test_missing_lock_and_escape_are_rejected(self):
        for package in ['../outside', str(self.root), 'tests\\browser']:
            with self.assertRaises(ValueError):
                native.prepare(self.root, package, run=self.fake_run)
        (self.root / 'tests/browser/package-lock.json').unlink()
        with self.assertRaises(ValueError):
            native.prepare(self.root, 'tests/browser', run=self.fake_run)
        self.assertEqual(self.commands, [])

    def test_generated_browser_outputs_cannot_follow_a_symlink(self):
        elsewhere = self.root / "outside"
        elsewhere.mkdir()
        (self.root / ".artifacts").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "without symlinks"):
            native.prepare(self.root, "tests/browser", cli_version="0.1.18", run=self.fake_run)
        self.assertEqual(self.commands, [])

    def test_installed_cli_cannot_disagree_with_the_pinned_version(self):
        def wrong(argv, **kwargs):
            value = self.fake_run(argv, **kwargs)
            if argv[-1] == '--version':
                value.stdout = '9.9.9\n'
            return value
        with self.assertRaisesRegex(ValueError, 'version differs'):
            native.prepare(self.root, 'tests/browser', cli_version='0.1.18', run=wrong)
        self.assertFalse((self.root / '.artifacts/playwright-cli').exists())


if __name__ == '__main__':
    unittest.main()
