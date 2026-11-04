import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('shared_github_release', Path(__file__).resolve().parents[1] / 'helpers/github_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ImmutablePublication(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        content = b'accepted release bytes'
        (self.directory / 'addon.zip').write_bytes(content)
        self.expected = {'addon.zip': hashlib.sha256(content).hexdigest()}
        self.source = 'b' * 40
        self.environment = {'GITHUB_SHA': self.source, 'GITHUB_REF': 'refs/heads/main',
                            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REPOSITORY': 'owner/project'}
        self.uploaded = {'draft': True, 'tag_name': 'v1.2.3', 'target_commitish': self.source,
                         'assets': [{'name': 'addon.zip', 'digest': 'sha256:' + self.expected['addon.zip']}]}
        self.ref = {'object': {'type': 'commit', 'sha': self.source}}
        self.operations = []

    def api(self, *argv, **_options):
        self.operations.append(argv)
        if 'POST' in argv:
            return '{}'
        if argv[:3] == ('gh', 'release', 'view'):
            return '7'
        if argv[-1] == 'repos/owner/project/releases/7':
            return json.dumps(self.uploaded)
        if argv[-1] == 'repos/owner/project/git/ref/tags/v1.2.3':
            return json.dumps(self.ref)
        return ''

    def write(self, argv, **_options):
        self.operations.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0)

    def publish(self):
        with patch.dict(os.environ, self.environment, clear=True), \
                patch.object(release, 'run', side_effect=self.api), \
                patch.object(release, 'absent'), \
                patch.object(release.subprocess, 'run', side_effect=self.write):
            release.publish(self.directory, self.expected, 'v1.2.3', self.source, 'owner/project')

    def test_exact_ref_is_claimed_before_upload_and_verified_before_publication(self):
        self.publish()
        claim = next(i for i, args in enumerate(self.operations) if 'POST' in args)
        draft = next(i for i, args in enumerate(self.operations) if args[:3] == ('gh', 'release', 'create'))
        published = next(i for i, args in enumerate(self.operations) if '--draft=false' in args)
        self.assertLess(claim, draft)
        self.assertLess(draft, published)
        self.assertIn('--verify-tag', self.operations[draft])

    def test_wrong_uploaded_digest_or_tag_source_never_publishes_the_draft(self):
        for mutation in ['digest', 'source']:
            with self.subTest(mutation=mutation):
                self.operations.clear()
                if mutation == 'digest':
                    self.uploaded['assets'][0]['digest'] = 'sha256:' + 'a' * 64
                else:
                    self.uploaded['assets'][0]['digest'] = 'sha256:' + self.expected['addon.zip']
                    self.ref['object']['sha'] = 'c' * 40
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertFalse(any('--draft=false' in args for args in self.operations))

    def test_changed_or_extra_local_bytes_fail_before_remote_mutation(self):
        (self.directory / 'addon.zip').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.operations, [])
        (self.directory / 'extra.txt').write_text('unreviewed')
        with self.assertRaises(ValueError):
            release.payload(self.directory, self.expected)

    def test_release_request_requires_exact_source_repository_and_main_dispatch(self):
        for override in [{'GITHUB_SHA': 'c' * 40}, {'GITHUB_REF': 'refs/heads/feature'},
                         {'GITHUB_EVENT_NAME': 'push'}, {'GITHUB_REPOSITORY': 'other/project'}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                release.validate_request('owner/project', self.source, 'v1.2.3', self.environment | override)

    def test_unknown_remote_status_cannot_establish_an_unused_identity(self):
        for code in ['403', '500']:
            result = subprocess.CompletedProcess([], 1, stdout='HTTP/2 ' + code, stderr='')
            with patch.object(release.subprocess, 'run', return_value=result), self.assertRaises(ValueError):
                release.absent('owner/project', 'git/ref/tags/v1.2.3')

    def test_version_selection_preserves_prefix_and_initial_version_policies(self):
        self.assertEqual(release.version('minor', 'v', []), ('0.0.1', 'v0.0.1'))
        self.assertEqual(release.version('minor', 'gd-v', [], 'bump'), ('0.1.0', 'gd-v0.1.0'))
        self.assertEqual(release.version('patch', 'gd-v', ['gd-v0.2.9', 'cli-v9.9.9']), ('0.2.10', 'gd-v0.2.10'))
        for bump, prefix in [('unknown', 'v'), ('patch', 'unknown')]:
            with self.assertRaises(ValueError):
                release.version(bump, prefix, [])


if __name__ == '__main__':
    unittest.main()
