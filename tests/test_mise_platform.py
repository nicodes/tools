import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('mise_platform', Path(__file__).resolve().parents[1] / 'helpers/mise-platform.py')
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)


class ReviewedSdk(unittest.TestCase):
    def test_windows_selects_the_executable_digest_and_never_the_macos_fallback(self):
        self.assertEqual(sdk.select('Windows', 'X64')['sha256'],
                         '8bc3a2c46246bb00fc49f467c678cf9ff321f707c533ad707061d0f8c7b639b2')
        self.assertEqual(sdk.select('Windows', 'ARM64')['sha256'],
                         '6beeb61b829ba60ef3bb1fdda5717a41b8fb91bd400109de8b05b1700a107d8e')
        for architecture in ['X64', 'ARM64']:
            self.assertNotEqual(sdk.select('Windows', architecture), sdk.select('macOS', architecture))

    def test_unsupported_runners_fail_instead_of_installing_another_platform(self):
        for system, architecture in [('FreeBSD', 'X64'), ('Windows', 'X86'), ('Linux', 'ARM')]:
            with self.subTest(system=system, architecture=architecture), self.assertRaises(ValueError):
                sdk.select(system, architecture)


if __name__ == '__main__':
    unittest.main()
