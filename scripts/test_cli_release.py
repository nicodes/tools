import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import cli_release as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = 'a' * 40

    def payload(self, member='example', kind=tarfile.REGTYPE):
        for _, _, system, arch in release.TARGETS:
            path = self.root / f'example_{system}_{arch}.tar.gz'
            with tarfile.open(path, 'w:gz') as archive:
                info = tarfile.TarInfo(member)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = 7
                    archive.addfile(info, io.BytesIO(b'example'))
                else:
                    info.linkname = 'elsewhere'
                    archive.addfile(info)
        self.rehash()
        return self.root

    def rehash(self):
        (self.root / 'checksums.txt').write_text(''.join(
            hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
            for p in sorted(self.root.glob('*.tar.gz'))))

    def test_complete_matrix(self):
        self.assertEqual(len(release.validate(self.payload(), 'example')), 5)

    def test_tampered_archive(self):
        self.payload()
        next(self.root.glob('*.tar.gz')).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            release.validate(self.root, 'example')

    def test_incomplete_matrix(self):
        self.payload()
        next(self.root.glob('*.tar.gz')).unlink()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            release.validate(self.root, 'example')

    def test_duplicate_checksum(self):
        self.payload()
        path = self.root / 'checksums.txt'
        path.write_text(path.read_text() * 2)
        with self.assertRaisesRegex(ValueError, 'duplicate checksum'):
            release.validate(self.root, 'example')

    def test_unsafe_members(self):
        for member, kind in [('../example', tarfile.REGTYPE), ('/example', tarfile.REGTYPE),
                             ('example', tarfile.SYMTYPE), ('example', tarfile.LNKTYPE),
                             ('different', tarfile.REGTYPE)]:
            with self.subTest(member=member, kind=kind):
                self.payload(member, kind)
                with self.assertRaises(ValueError):
                    release.validate(self.root, 'example')

    def test_unexpected_file_and_explicit_extra(self):
        self.payload()
        (self.root / 'acceptance.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            release.validate(self.root, 'example')
        self.assertIn('acceptance.json', release.validate(self.root, 'example', ['acceptance.json']))

    def test_symlinked_asset(self):
        self.payload()
        (self.root / 'install.sh').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'regular'):
            release.validate(self.root, 'example', ['install.sh'])

    @patch.object(release, 'run', return_value='v0.3.0\nv0.10.0\nsdk/v9.0.0\nv1.0.0-rc1')
    def test_version_bumps_ignore_other_modules_and_prereleases(self, _run):
        self.assertEqual(release.version('patch'), ('0.10.1', 'v0.10.1'))
        self.assertEqual(release.version('minor'), ('0.11.0', 'v0.11.0'))
        self.assertEqual(release.version('major'), ('1.0.0', 'v1.0.0'))

    @patch.object(release, 'run', return_value='gd-v1.0.0\ncli-v0.2.0')
    def test_namespaced_version(self, _run):
        self.assertEqual(release.version('patch', 'cli-v'), ('0.2.1', 'cli-v0.2.1'))

    @patch.object(release, 'run', return_value='')
    def test_initial_version(self, _run):
        self.assertEqual(release.version('major'), ('0.0.1', 'v0.0.1'))

    def test_build_target_and_source_identity(self):
        output = self.root / 'archives'
        calls = []

        def compile_binary(command, **kwargs):
            calls.append((command, kwargs['env']))
            Path(command[command.index('-o') + 1]).write_bytes(b'compiled executable')

        metadata = [f'GOOS={goos}\nGOARCH={goarch}\nvcs.revision={self.source}'
                    for goos, goarch, _, _ in release.TARGETS]
        with patch.object(release.subprocess, 'run', side_effect=compile_binary), \
                patch.object(release, 'run', side_effect=metadata):
            release.build('example', './cmd/example', '.', output, 'v0.0.1', self.source,
                          'main.version', 'main.revision')
        self.assertEqual(len(calls), 4)
        for command, env in calls:
            self.assertEqual(env['CGO_ENABLED'], '0')
            self.assertIn('main.version=v0.0.1', command[command.index('-ldflags') + 1])
            self.assertIn('main.revision=' + self.source, command[command.index('-ldflags') + 1])
        release.validate(output, 'example')

    def test_publication_requires_explicit_main_dispatch(self):
        self.payload()
        for ref, event, source in [('refs/pull/1/merge', 'pull_request', self.source),
                                   ('refs/heads/main', 'push', self.source),
                                   ('refs/heads/main', 'workflow_dispatch', 'b' * 40)]:
            with patch.dict(os.environ, GITHUB_SHA=self.source, GITHUB_REF=ref, GITHUB_EVENT_NAME=event), \
                    patch.object(release.subprocess, 'run') as command:
                with self.assertRaises(ValueError):
                    release.publish(self.root, 'example', 'v0.0.1', source, 'example/cli')
                command.assert_not_called()

    def test_publication_verifies_uploaded_draft_before_publishing(self):
        expected = release.validate(self.payload(), 'example')
        uploaded = {'draft': True, 'tag_name': 'v0.0.1', 'target_commitish': self.source,
                    'assets': [{'name': n, 'digest': 'sha256:' + d} for n, d in expected.items()]}
        for tamper in [False, True]:
            with self.subTest(tamper=tamper):
                candidate = json.loads(json.dumps(uploaded))
                if tamper:
                    candidate['assets'][0]['digest'] = 'sha256:' + '0' * 64
                with patch.dict(os.environ, GITHUB_SHA=self.source, GITHUB_REF='refs/heads/main',
                                GITHUB_EVENT_NAME='workflow_dispatch'), \
                        patch.object(release, 'absent') as absent, \
                        patch.object(release, 'run', side_effect=['', '42', json.dumps(candidate)]) as query, \
                        patch.object(release.subprocess, 'run') as command:
                    if tamper:
                        with self.assertRaisesRegex(ValueError, 'remains unpublished'):
                            release.publish(self.root, 'example', 'v0.0.1', self.source, 'example/cli')
                    else:
                        release.publish(self.root, 'example', 'v0.0.1', self.source, 'example/cli')
                    self.assertEqual(absent.call_count, 2)
                    self.assertEqual(query.call_args.args, ('gh', 'api', 'repos/example/cli/releases/42'))
                    self.assertIn('--draft', command.call_args_list[0].args[0])
                    self.assertEqual(command.call_count, 1 if tamper else 2)

    def test_existing_draft_is_refused_before_creating_a_release(self):
        self.payload()
        with patch.dict(os.environ, GITHUB_SHA=self.source, GITHUB_REF='refs/heads/main',
                        GITHUB_EVENT_NAME='workflow_dispatch'), \
                patch.object(release, 'run', return_value='v0.0.1'), \
                patch.object(release.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'including drafts'):
                release.publish(self.root, 'example', 'v0.0.1', self.source, 'example/cli')
            command.assert_not_called()

    def test_preflight_distinguishes_absence_existing_and_network_failure(self):
        for status, code, allowed in [('404', 1, True), ('200', 0, False), ('403', 1, False), ('500', 1, False)]:
            result = subprocess.CompletedProcess([], code, stdout='HTTP/2.0 ' + status + '\n', stderr='')
            with patch.object(release.subprocess, 'run', return_value=result):
                if allowed:
                    release.absent('example/cli', 'git/ref/tags/v0.0.1')
                else:
                    with self.assertRaises(ValueError):
                        release.absent('example/cli', 'git/ref/tags/v0.0.1')


if __name__ == '__main__':
    unittest.main()
