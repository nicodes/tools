"""The release artifact must be the vendored snapshot, byte-for-byte reproducible."""
import hashlib
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('engineering_artifact', ROOT/'helpers/engineering-artifact.py')
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


class EngineeringArtifact(unittest.TestCase):
    def git(self, repository, *arguments):
        if arguments == ('rev-parse', '--verify', 'b'*40 + '^{commit}'):
            return ('b'*40 + '\n').encode()
        if arguments == ('ls-tree', '-rz', '--full-tree', 'b'*40, '--', 'helpers/', 'tests/'):
            return b'100755 blob cccc\thelpers/new.py\0' + b'100644 blob dddd\ttests/test_new.py\0'
        if arguments == ('cat-file', 'blob', 'cccc'):
            return b'new helper\n'
        if arguments == ('cat-file', 'blob', 'dddd'):
            return b'new test\n'
        self.fail('unexpected Git operation: ' + repr(arguments))

    def build(self, directory, name='a.tar.gz'):
        vendor = artifact._vendor_snapshot()
        original = vendor.git
        vendor.git = self.git
        artifact._vendor_snapshot = lambda: vendor
        try:
            return artifact.build('source', 'b'*40, Path(directory)/name)
        finally:
            vendor.git = original

    def test_two_builds_of_one_revision_are_the_same_bytes(self):
        """A pinned checksum in .mise.toml is only worth anything if this holds."""
        with tempfile.TemporaryDirectory() as directory:
            first, _ = self.build(directory, 'a.tar.gz')
            second, _ = self.build(directory, 'b.tar.gz')
            self.assertEqual(first, second)
            self.assertEqual(
                hashlib.sha256((Path(directory)/'a.tar.gz').read_bytes()).hexdigest(), first)
            self.assertEqual((Path(directory)/'a.tar.gz').read_bytes(),
                             (Path(directory)/'b.tar.gz').read_bytes())

    def test_the_archive_carries_the_snapshot_and_its_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            _, names = self.build(directory)
            self.assertEqual(names, ['helpers/new.py', 'tests/test_new.py'])
            with tarfile.open(Path(directory)/'a.tar.gz') as opened:
                self.assertEqual(sorted(opened.getnames()),
                                 ['SOURCE.json', 'helpers/new.py', 'tests/test_new.py'])

    def test_git_file_modes_survive_the_archive(self):
        """A helper that was executable in the tree has to stay executable."""
        with tempfile.TemporaryDirectory() as directory:
            self.build(directory)
            with tarfile.open(Path(directory)/'a.tar.gz') as opened:
                modes = {member.name: member.mode for member in opened.getmembers()}
            self.assertEqual(modes['helpers/new.py'], 0o755)
            self.assertEqual(modes['tests/test_new.py'], 0o644)

    def test_nothing_records_a_timestamp_or_an_owner(self):
        """Those are what make two builds of one commit differ."""
        with tempfile.TemporaryDirectory() as directory:
            self.build(directory)
            with tarfile.open(Path(directory)/'a.tar.gz') as opened:
                for member in opened.getmembers():
                    with self.subTest(member=member.name):
                        self.assertEqual(member.mtime, 0)
                        self.assertEqual((member.uid, member.gid), (0, 0))
                        self.assertEqual((member.uname, member.gname), ('', ''))
            # ...including the gzip header's own mtime field, which tarfile
            # writes from the clock and no tar member exposes.
            self.assertEqual((Path(directory)/'a.tar.gz').read_bytes()[4:8], b'\x00\x00\x00\x00')


if __name__ == '__main__':
    unittest.main()
