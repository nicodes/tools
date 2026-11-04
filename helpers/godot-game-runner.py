"""Reject Godot failures, isolate per-process user data and retain test timings."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def main(argv, timeout=180, sentinel=None, forbidden=(), environment=None, cwd=None):
    if not argv or not 0 < timeout <= 3600:
        raise SystemExit("Usage: run_godot_test.py GODOT [ARGS...]")
    started = time.monotonic()
    code = 1
    timed_out = False
    try:
        with tempfile.TemporaryDirectory(prefix="godot-game-test-") as directory:
            environment = dict(environment or os.environ)
            # Godot's Linux user:// location is below XDG_DATA_HOME. Each
            # process gets a fresh profile, including tests using the default.
            # mise also follows XDG directories. Keep its installed toolchain
            # and configuration in the parent's locations before isolating Godot.
            for key, xdg, fallback in (("MISE_DATA_DIR", "XDG_DATA_HOME", ".local/share"),
                                       ("MISE_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")):
                if not environment.get(key):
                    environment[key] = str(Path(environment.get(xdg) or str(Path.home()/fallback))/"mise")
            environment["XDG_DATA_HOME"] = str(Path(directory)/"data")
            environment["XDG_CONFIG_HOME"] = str(Path(directory)/"config")
            try:
                result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, timeout=timeout, check=False, env=environment, cwd=cwd)
            except subprocess.TimeoutExpired as error:
                timed_out = True
                output = error.stdout or b""
                print(output.decode(errors="replace") if isinstance(output, bytes) else output)
                print(f"Godot test exceeded {timeout:g} seconds", file=sys.stderr)
            else:
                print(result.stdout, end="")
                plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)
                failed = re.search(r"(?:SCRIPT ERROR:|(?:^|\n)\s*(?:ERROR:|FAIL:))", plain)
                missing = sentinel is not None and sentinel not in plain
                rejected = any(value in plain for value in forbidden)
                code = result.returncode or int(failed is not None or missing or rejected)
        return code
    finally:
        record = {"seconds": round(time.monotonic()-started, 3), "exit_code": code,
                  "timeout": timed_out, "source_commit": os.environ.get("SOURCE_COMMIT", os.environ.get("GITHUB_SHA")),
                  "test": argv[argv.index("--script")+1] if "--script" in argv and argv.index("--script")+1 < len(argv) else "runner-control"}
        try:
            target = Path(os.environ.get("GODOT_TEST_TIMINGS", str(Path.cwd()/".artifacts/godot-tests.jsonl")))
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, (json.dumps(record)+"\n").encode())
            finally:
                os.close(descriptor)
        except OSError:
            print("Test timing evidence unavailable", file=sys.stderr)


if __name__ == "__main__":
    status = main(sys.argv[1:])
    sys.exit(status if status >= 0 else 128-status)
