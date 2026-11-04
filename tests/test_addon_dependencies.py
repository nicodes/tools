from pathlib import Path
import hashlib
import io
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import addon_dependencies


class RegistrySdkControls(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'mise.toml').write_text('[tools]\n"github:owner/gdam" = {version="v0.1.5",asset_pattern="gdam_Linux_x86_64.tar.gz",bin="gdam"}\n')
        binary = b'verified native SDK bytes'
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            entry = tarfile.TarInfo('gdam')
            entry.size = len(binary)
            archive.addfile(entry, io.BytesIO(binary))
        self.archive = data.getvalue()
        self.config = {'components': {'addon_dependencies': dict(mise_tool='github:owner/gdam',
                       sha256=hashlib.sha256(self.archive).hexdigest(),destination='.artifacts/bin/gdam')}}
        self.downloads = []
        self.commands = []

    def fetch(self, url, destination):
        self.downloads.append(url)
        destination.write_bytes(self.archive)

    def execute(self, command, **options):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, 'gdam 0.1.5\n', '')

    def test_reviewed_bytes_are_installed_and_only_frozen_restore_is_run(self):
        addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertIn('/v0.1.5/gdam_Linux_x86_64.tar.gz', self.downloads[0])
        self.assertEqual(self.commands[-1][1:], ['install', '--frozen-lockfile'])
        addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(len(self.downloads), 1)
        (self.root / '.artifacts/bin/gdam').write_bytes(b'cache mutation')
        addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(len(self.downloads), 2)

    def test_checksum_mismatch_never_executes_unverified_sdk(self):
        self.config['components']['addon_dependencies']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'checksum'):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(self.commands, [])

    def test_sole_mise_version_authority_and_native_binary_type_are_checked(self):
        (self.root / 'mise.toml').write_text('[tools]\n"github:owner/gdam" = "latest"\n')
        with self.assertRaisesRegex(ValueError, 'exact Mise'):
            addon_dependencies.setup(self.root, self.config, {}, fetch=self.fetch, execute=self.execute)
        self.assertEqual(self.downloads, [])


if __name__ == '__main__':
    unittest.main()
