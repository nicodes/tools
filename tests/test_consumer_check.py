import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


spec = importlib.util.spec_from_file_location('consumer_check', Path(__file__).parents[1]/'helpers/consumer-check.py')
consumer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(consumer)


class InstalledArchive(unittest.TestCase):
    def archive(self, root, extra=None, change=False):
        payload = b'raise RuntimeError("must never execute while installing")\n'
        source = json.dumps({'repository': 'https://github.com/nicodes/tools', 'revision': 'a'*40,
                             'files': {'helpers/probe.py': hashlib.sha256(payload).hexdigest()}}).encode()
        archive = root/'package.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in [('SOURCE.json', source), ('helpers/probe.py', b'changed' if change else payload)]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            if extra:
                tar.addfile(extra)
        return archive, consumer.checksum(archive), hashlib.sha256(source).hexdigest()

    def test_install_uses_only_the_complete_pinned_archive_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, source = self.archive(root)
            consumer.install(archive, root/'installed', 'a'*40, digest, source)
            self.assertTrue((root/'installed/helpers/probe.py').is_file())

    def test_wrong_revision_independent_pin_or_changed_member_is_rejected(self):
        for case in ('revision', 'archive', 'source', 'member'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive, digest, source = self.archive(root, change=case == 'member')
                with self.assertRaises(ValueError):
                    consumer.install(archive, root/'installed', 'b'*40 if case == 'revision' else 'a'*40,
                                     '0'*64 if case == 'archive' else digest,
                                     '0'*64 if case == 'source' else source)

    def test_archive_paths_links_and_unlisted_files_are_rejected(self):
        for name in ('../escape', '/escape', 'helpers/link.py', 'helpers/unlisted.py', 'SOURCE.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                extra = tarfile.TarInfo(name)
                if name.endswith('link.py'):
                    extra.type = tarfile.SYMTYPE
                    extra.linkname = '/outside'
                archive, digest, source = self.archive(root, extra=extra)
                with self.assertRaises(ValueError):
                    consumer.install(archive, root/'installed', 'a'*40, digest, source)
                self.assertFalse((root/'escape').exists())


if __name__ == '__main__':
    unittest.main()
