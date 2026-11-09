"""Cache identities survive private fixtures without another source traversal."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'podman-ci/action.yml'


def freeze_program():
    lines = ACTION.read_text().splitlines()
    start = lines.index('      id: cache_identity')
    body = lines.index('      run: |', start) + 1
    end = body
    while end < len(lines) and lines[end].startswith('        '):
        end += 1
    return '\n'.join(line[8:] for line in lines[body:end]) + '\n'


class CacheIdentities(unittest.TestCase):
    def freeze(self, web, go):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            output.touch(mode=0o600)
            environment = dict(os.environ, WEB_INPUTS=web, GO_INPUTS=go,
                               GITHUB_OUTPUT=str(output))
            result = subprocess.run(['bash', '-euo', 'pipefail'],
                                    input=freeze_program(), text=True,
                                    capture_output=True, env=environment, timeout=5)
            return result, output.read_text()

    def test_exact_source_identities_are_forwarded_without_rehashing(self):
        result, output = self.freeze('a' * 64, 'b' * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'web=' + 'a' * 64 + '\ngo=' + 'b' * 64 + '\n')
        self.assertEqual(result.stdout, '')

    def test_empty_original_input_sets_retain_their_existing_identity(self):
        result, output = self.freeze('', '')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'web=\ngo=\n')

    def test_malformed_or_multiline_input_cannot_inject_a_post_action_key(self):
        for malformed in ['x' * 64, 'a' * 63, 'A' * 64, 'a' * 64 + '\nforeign=value']:
            for web, go in [(malformed, 'b' * 64), ('a' * 64, malformed)]:
                with self.subTest(web=web, go=go):
                    result, output = self.freeze(web, go)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(output, '')

    def test_original_hash_inputs_and_cache_scope_are_frozen_once(self):
        action = ACTION.read_text()
        for selector in ['TOOLS_ROOT', 'TOOLS_REF', 'COOKBOOK_ROOT', 'COOKBOOK_REF']:
            self.assertNotIn(selector, action)
        self.assertEqual(action.count("hashFiles('mise.toml', '**/package.json', '**/bun.lock', '**/package-lock.json')"), 1)
        self.assertEqual(action.count("hashFiles('mise.toml', '**/go.mod', '**/go.sum')"), 1)
        self.assertLess(action.index('id: cache_identity'), action.index('uses: actions/cache@'))
        caches = action[action.index('    - uses: actions/cache@'):]
        self.assertNotIn('hashFiles(', caches)
        scope = '${{ runner.os }}-${{ runner.arch }}-${{ github.repository }}-${{ github.job }}-'
        for kind in ['web', 'go']:
            prefix = 'cookbook-' + kind + '-v1-' + scope + '${{ steps.cache_identity.outputs.' + kind + ' }}-'
            self.assertIn('key: ' + prefix + '${{ github.sha }}', caches)
            self.assertIn('restore-keys: ' + prefix + '\n', caches)
        self.assertIn('${{ runner.temp }}/fleet-webcompiler/*/metro-cache', caches)
        self.assertIn('${{ runner.temp }}/fleet-webcompiler/*/state.json', caches)
        self.assertIn('${{ runner.temp }}/fleet-go-compiler', caches)


if __name__ == '__main__':
    unittest.main()
