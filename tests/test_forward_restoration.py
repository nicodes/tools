from pathlib import Path
import unittest

from vendored import VENDORED


ROOT = Path(__file__).resolve().parents[1]


class ForwardRestoration(unittest.TestCase):
    def test_established_snapshot_and_restore_helpers_remain(self):
        for relative in (
            "helpers/snapshot.py",
            "helpers/restore-drill.py",
            "helpers/receive-backup.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_relanded_postgres_recovery_helpers_are_present(self):
        # The helper is vendored and must be present everywhere. The prose
        # beside it is this repository's, and a product has no copy -- one
        # that had one would be an uninventoried duplicate free to drift.
        self.assertTrue((ROOT / "helpers/postgres-recovery.py").is_file())
        if not VENDORED:
            self.assertTrue((ROOT / "docs/postgresql-recovery.md").is_file())
