"""The pinned parser build cannot load a product's Cargo hooks or credentials."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ParserBuildIsolation(unittest.TestCase):
    def test_build_uses_trusted_source_private_cache_and_clean_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            binaries = root / "bin"
            binaries.mkdir()
            record = root / "record.json"
            cargo = binaries / "cargo-real"
            rustc = binaries / "rustc-real"
            rustc.write_text("#!/bin/sh\nexit 99\n")
            rustc.chmod(0o700)
            cargo.write_text(f"#!{sys.executable}\n" + f'''import json,os,sys
from pathlib import Path
Path({str(record)!r}).write_text(json.dumps({{'cwd':os.getcwd(),'args':sys.argv[1:],'cargo_home':os.environ['CARGO_HOME'],'rustc':os.environ['RUSTC'],'hooks':[name for name in ['GH_TOKEN','RUSTC_WRAPPER','RUSTFLAGS','CARGO_BUILD_RUSTC_WRAPPER'] if name in os.environ]}}))
output=Path(os.environ['CARGO_TARGET_DIR'])/'debug/tools-operator'
output.parent.mkdir(parents=True)
output.write_bytes(b'fixture parser')
''')
            cargo.chmod(0o700)
            rustup = binaries / "rustup"
            rustup.write_text(f"#!{sys.executable}\nimport sys\nprint({str(cargo)!r} if sys.argv[-1]=='cargo' else {str(rustc)!r})\n")
            rustup.chmod(0o700)
            product = root / "untrusted-product"
            (product / ".cargo").mkdir(parents=True)
            (product / ".cargo/config.toml").write_text('[build]\nrustc-wrapper="must-not-run"\n')
            env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                   "GH_TOKEN": "synthetic-must-not-reach-compiler", "RUSTC_WRAPPER": "must-not-run",
                   "RUSTFLAGS": "must-not-load", "CARGO_HOME": str(product / ".cargo"),
                   "CARGO_BUILD_RUSTC_WRAPPER": "must-not-run"}
            subprocess.run(["bash", str(ROOT / "dependencies/build-parser.sh"), str(stage), "a" * 40],
                           cwd=product, env=env, check=True, capture_output=True, timeout=10)
            observed = json.loads(record.read_text())
            self.assertEqual(observed["cwd"], str(ROOT))
            self.assertEqual(observed["cargo_home"], str(stage / "cargo"))
            self.assertEqual(observed["rustc"], str(rustc))
            self.assertEqual(observed["hooks"], [])
            self.assertIn("--locked", observed["args"])
            self.assertEqual((stage / "tools-operator").read_bytes(), b"fixture parser")
            self.assertEqual(stat.S_IMODE((stage / "tools-operator").stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
