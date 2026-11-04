from pathlib import Path
import os
import subprocess
import unittest


class ImmutableAddonRecovery(unittest.TestCase):
    def test_frozen_release_identity_and_mutation_tripwires(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['bash', str(root / 'tests/addon_release_recovery_test.sh')], cwd=root, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('controls=26 reached=10', result.stdout)

    def test_asset_identity_cannot_escape_the_download_directory(self):
        root = Path(__file__).resolve().parents[1]
        for asset in ['../addon.zip', 'addon.zip\nextra', '.hidden.zip', 'addon.zip;false']:
            result = subprocess.run(['bash', str(root / 'helpers/addon-release-recovery.sh'), 'assert-bump'], cwd=root, env={**os.environ, 'RELEASE_ASSET_NAME': asset, 'RETRY': 'false'}, text=True, capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0, asset)
            self.assertIn('safe ZIP basename', result.stderr)


if __name__ == '__main__':
    unittest.main()
