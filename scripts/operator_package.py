"""Compile native Rust operator archives and verify receipts before release assembly."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / ".artifacts/native-operator"
TARGETS = {"Linux_x86_64": "x86_64-unknown-linux-musl", "Linux_arm64": "aarch64-unknown-linux-musl",
           "Darwin_x86_64": "x86_64-apple-darwin", "Darwin_arm64": "aarch64-apple-darwin"}
ASSET_NAMES = {"fleet_operator.py", "fleet_broker.py", "fleet_publisher.py", "fleet-operator.service", "fleet-operator.socket", "fleet-operator.conf"}
SOURCE = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
VERSION = os.environ.get("VERSION", "dev")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native():
    label = platform.system() + "_" + ("arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64")
    target = TARGETS[label]
    subprocess.run(["rustup", "target", "add", "--toolchain", "1.94.0", target], check=True)
    env = {**os.environ, "VERSION": VERSION, "COMMIT": SOURCE}
    if label.startswith("Linux_"):
        env["CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER"] = "musl-gcc"
        env["CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER"] = "musl-gcc"
    subprocess.run(["cargo", "build", "--locked", "--release", "-p", "tools-operator", "--target", target], cwd=ROOT, env=env, check=True)
    binary = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target")) / target / "release/tools-operator"
    report = json.loads(subprocess.check_output([str(binary), "--build-info"], text=True))
    assert report["version"] == VERSION and report["commit"] == SOURCE and report["target"] == target
    assets = ROOT / "operator/internal/operator/assets"
    assert set(report["bundle"]) == ASSET_NAMES
    assert all(digest(assets / name) == value for name, value in report["bundle"].items())
    if label.startswith("Linux_"):
        linkage = subprocess.check_output(["readelf", "-l", "-d", str(binary)], text=True)
        assert "INTERP" not in linkage and "NEEDED" not in linkage, "Linux release must be static"
    NATIVE.mkdir(parents=True, exist_ok=True)
    archive = NATIVE / f"tools-operator_{label}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for path in [binary, *[ROOT / name for name in ("README.md", "LICENSE") if (ROOT / name).is_file()]]:
            info = output.gettarinfo(path, arcname=path.name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if path == binary else 0o644
            with path.open("rb") as body:
                output.addfile(info, body)
    archive.with_suffix(".json").write_text(json.dumps({"source": SOURCE, "version": VERSION,
        "target": target, "archive_sha256": digest(archive), "binary_sha256": digest(binary),
        "bundle": report["bundle"]}, sort_keys=True) + "\n")


def assemble():
    output = ROOT / ".artifacts/operator-release"
    if output.exists() or output.is_symlink():
        raise ValueError("operator release staging must be new")
    names = {f"tools-operator_{label}.tar.gz" for label in TARGETS}
    receipts = {Path(name).with_suffix(".json").name for name in names}
    assert {p.name for p in NATIVE.iterdir()} == names | receipts, "native operator set is incomplete or unexpected"
    verified = []
    for label, target in TARGETS.items():
        archive = NATIVE / f"tools-operator_{label}.tar.gz"
        receipt = json.loads(archive.with_suffix(".json").read_text())
        assert receipt["source"] == SOURCE and receipt["version"] == VERSION and receipt["target"] == target
        assert receipt["archive_sha256"] == digest(archive)
        with tarfile.open(archive, "r:gz") as payload:
            members = payload.getmembers()
            expected = {"tools-operator"} | {name for name in ("README.md", "LICENSE") if (ROOT / name).is_file()}
            assert {m.name for m in members} == expected and len(members) == len(expected)
            assert all(m.isfile() for m in members)
            binary = payload.getmember("tools-operator")
            assert binary.mode & 0o777 == 0o755 and binary.size < 64 << 20
            assert hashlib.sha256(payload.extractfile(binary).read()).hexdigest() == receipt["binary_sha256"]
        assets = ROOT / "operator/internal/operator/assets"
        assert set(receipt["bundle"]) == ASSET_NAMES and all(digest(assets / name) == value for name, value in receipt["bundle"].items())
        verified.append(archive)
    output.mkdir()
    for archive in verified:
        shutil.copyfile(archive, output / archive.name)
    (output / "checksums.txt").write_text("".join(f"{digest(archive)}  {archive.name}\n" for archive in sorted(verified)))


if __name__ == "__main__":
    if sys.argv[1:] == ["native"]:
        native()
    elif sys.argv[1:] == ["assemble"]:
        assemble()
    else:
        raise SystemExit("usage: operator_package.py native|assemble")
