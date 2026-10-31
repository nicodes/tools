"""Exercise the operator's offline interface without installation or grant use."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("TOOLS_OPERATOR_EXECUTABLE", str(Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target")) / "debug/tools-operator"))).resolve()
ASSETS = ROOT / "operator/internal/operator/assets"


class OperatorCLI(unittest.TestCase):
    def cli(self, *args, body=b""):
        return subprocess.run([str(BINARY), *args], input=body, capture_output=True, timeout=10)

    def test_version_and_usage(self):
        version = self.cli("version")
        self.assertEqual(version.returncode, 0)
        self.assertTrue(version.stdout.strip())
        self.assertEqual(version.stderr, b"")
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"usage:", result.stderr)

    def test_scalar_metadata_is_json(self):
        result = self.cli("metadata", body=b"updated-dependencies:\n- dependency-name: example\n  dependency-version: 1.2.3\n  update-type: version-update:semver-patch\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["updated-dependencies"][0]["dependency-version"], "1.2.3")

    def test_metadata_refuses_ambiguity_indirection_and_multiple_documents(self):
        for raw in [b"updated-dependencies: []\nupdated-dependencies: []", b"updated-dependencies: &x []", b"updated-dependencies: [*x]",
                    b"updated-dependencies: !!seq []", b"updated-dependencies: []\n---\nupdated-dependencies: []",
                    b"updated-dependencies: [{dependency-name: example, dependency-name: other}]"]:
            result = self.cli("metadata", body=raw)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")

    def test_metadata_input_is_bounded(self):
        result = self.cli("metadata", body=b"x" * 65537)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")

    def test_export_is_private_and_identifies_every_original_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage"
            result = self.cli("export", "--directory", str(stage))
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["version"], 1)
            self.assertEqual(report["directory"], str(stage))
            self.assertEqual(len(report["files"]), 6)
            self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
            for name, expected in report["files"].items():
                self.assertEqual((stage / name).read_bytes(), (ASSETS / name).read_bytes())
                self.assertEqual(hashlib.sha256((ASSETS / name).read_bytes()).hexdigest(), expected)
                self.assertEqual(stat.S_IMODE((stage / name).stat().st_mode), 0o600)

    def test_export_cannot_overwrite_prior_reviewed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage"
            stage.mkdir(mode=0o700)
            (stage / "reviewed").write_bytes(b"keep")
            result = self.cli("export", "--directory", str(stage))
            self.assertEqual(result.returncode, 1)
            self.assertEqual((stage / "reviewed").read_bytes(), b"keep")
            self.assertEqual(set(p.name for p in stage.iterdir()), {"reviewed"})

    def test_invalid_exports_never_create_a_stage(self):
        for directory in ["/", "relative", "/tmp/./operator", "/tmp/../operator", "/tmp//operator", "/tmp/operator/"]:
            self.assertEqual(self.cli("export", "--directory", directory).returncode, 1)

    def test_invalid_flags_never_dispatch_lifecycle_or_consume_a_grant(self):
        for args in [("metadata", "extra"), ("unknown",), ("export", "--expected-plan", "a" * 64),
                     ("status", "--expected-plan", "a" * 64), ("plan", "--directory", "/unusable"),
                     ("export", "--unknown"), ("export", "positional")]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 1, args)
            self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
