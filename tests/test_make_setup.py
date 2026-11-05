import hashlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('windows_make_setup', Path(__file__).resolve().parents[1]/'helpers/make-setup.py')
make = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make)


class MakeBootstrap(unittest.TestCase):
    def archive(self, payload, duplicate=False):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr(make.MEMBER, payload)
            if duplicate:
                archive.writestr(make.MEMBER, payload)
        return output.getvalue()

    def test_package_and_executable_are_independently_bound(self):
        payload = b'fixture' * 47346 + b'abc'
        self.assertEqual(len(payload), 331425)
        data = self.archive(payload)
        with patch.object(make, 'PACKAGE_SHA256', hashlib.sha256(data).hexdigest()), patch.object(make, 'BINARY_SHA256', hashlib.sha256(payload).hexdigest()):
            self.assertEqual(make.binary(data), payload)
            with self.assertRaisesRegex(ValueError, 'package checksum'):
                make.binary(data+b'mutated')
        with patch.object(make, 'PACKAGE_SHA256', hashlib.sha256(data).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'executable checksum'):
                make.binary(data)

    def test_ambiguous_or_wrong_sized_members_are_rejected(self):
        for data in [self.archive(b'wrong size'), self.archive(b'x'*331425, duplicate=True)]:
            with patch.object(make, 'PACKAGE_SHA256', hashlib.sha256(data).hexdigest()):
                with self.assertRaisesRegex(ValueError, 'exact executable'):
                    make.binary(data)


if __name__ == '__main__':
    unittest.main()
