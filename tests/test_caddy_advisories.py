import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('test_caddy', Path(__file__).resolve().parents[1]/'helpers/test-caddy.py')
caddy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caddy)


def report(*advisories):
    rows = [{'config': {'protocol_version': 'v1.0.0', 'scanner_name': 'govulncheck',
             'scanner_version': 'v1.7.0', 'scan_level': 'symbol', 'scan_mode': 'binary'}},
            {'SBOM': {'modules': [{'path': 'example.org/app', 'version': 'v1.0.0'}]}}]
    rows += [{'finding': {'osv': name, 'trace': [{'module': 'example.org/lib', 'version': 'v1.0.0',
                                                  'package': 'example.org/lib', 'function': 'Call'}]}}
             for name in advisories]
    return '\n'.join(json.dumps(row) for row in rows)


class CaddyAdvisoryGate(unittest.TestCase):
    def run_script(self, arguments, advisories=(), built='go1.27.4', build_fails=False):
        def check_output(command, **_):
            if command[:2] == ['go', 'env']:
                return '/fake/go\n'
            if command[1:3] == ['version', '-m']:
                return f'caddy: {built}\n'
            if command[1:] == ['version']:
                return 'go version go1.27.4 linux/amd64\n'
            if command[0] == 'govulncheck':
                return report(*advisories)
            raise AssertionError(command)

        def run(command, **_):
            if build_fails:
                raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(caddy.subprocess, 'check_output', check_output), \
                mock.patch.object(caddy.subprocess, 'run', run), \
                mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(Path(directory)/'summary')}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                caddy.main(arguments)
            finally:
                summary = Path(directory)/'summary'
                self.summary = summary.read_text() if summary.exists() else ''
                self.output = output.getvalue()

    def test_advisories_warn_without_the_flag(self):
        self.run_script([], advisories=['GO-2026-6974', 'GO-2026-6975'])
        self.assertIn('::warning', self.output)
        self.assertIn('GO-2026-6974', self.output)
        self.assertIn('GO-2026-6975', self.summary)

    def test_advisories_fail_with_the_flag(self):
        with self.assertRaisesRegex(ValueError, 'vulnerable linked symbols.*GO-2026-6974'):
            self.run_script(['--fail-on-advisories'], advisories=['GO-2026-6974'])

    def test_clean_binary_passes_either_way(self):
        for arguments in ([], ['--fail-on-advisories']):
            self.run_script(arguments)
            self.assertNotIn('::warning', self.output)
            self.assertEqual(self.summary, '')

    def test_genuine_problems_fail_regardless_of_the_flag(self):
        for arguments in ([], ['--fail-on-advisories']):
            with self.assertRaisesRegex(ValueError, 'different compiler'):
                self.run_script(arguments, built='go1.0.0')
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_script(arguments, build_fails=True)


if __name__ == '__main__':
    unittest.main()
