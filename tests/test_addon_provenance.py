"""Keep the native JavaScript publication controls enrolled in the Tools unit gate."""
from pathlib import Path
import subprocess
import unittest


class NativePublicationControls(unittest.TestCase):
    def test_native_identity_and_provenance_controls(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run(['node', '--test', str(root / 'tests/addon-provenance.test.mjs')],
                       cwd=root, timeout=30, check=True)


if __name__ == '__main__':
    unittest.main()
