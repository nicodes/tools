#!/usr/bin/env python3
"""Render the fixed SDK identity bootstrap used by Mise tasks."""
import argparse
import base64
import hashlib
from pathlib import Path
import re
import shlex
import zlib

# This seed runs before loading any SDK code. Its exact bytes are generated into
# source tasks; it never executes a fetched product hook or caller program.
SEED = Path(__file__).with_name('sdk_identity.py').read_text() + '''
import sys
ref, python_mode, mode, *arguments = sys.argv[1:]
if (not re.fullmatch("[a-f0-9]{40}", ref) or mode not in {"setup", "exec"}
        or python_mode not in {"host", "pinned"}):
    raise SystemExit("SDK bootstrap requires one immutable source and explicit operation")
root = Path.cwd()
if identity(read_source(root))["revision"] != ref:
    raise SystemExit("SDK bootstrap differs from its caller-declared source")
interpreter = sys.executable
if python_mode == "pinned":
    data = tomllib.loads(read_source(root).decode().replace("\\r\\n", "\\n").replace("\\r", "\\n"))
    version = data.get("tools", {}).get("python")
    version = version.get("version") if isinstance(version, dict) else version
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\\.[0-9]+\\.[0-9]+", version):
        raise SystemExit("bootstrap Python requires the exact caller-pinned version")
    environment = dict(os.environ, MISE_AUTO_INSTALL="false", MISE_EXEC_AUTO_INSTALL="false",
                       MISE_TASK_RUN_AUTO_INSTALL="false")
    install = subprocess.check_output(["mise", "where", "python@" + version], cwd=root,
        env=environment, text=True, timeout=30).strip()
    directory = Path(install)
    if not directory.is_absolute() or not directory.is_dir():
        raise SystemExit("exact bootstrap Python is absent; run explicit setup")
    candidates = [directory / "bin/python3", directory / "python.exe"]
    executable = next((path.resolve() for path in candidates if path.is_file()
                       and path.resolve().is_relative_to(directory.resolve())), None)
    if executable is None:
        raise SystemExit("exact bootstrap Python executable is absent; run explicit setup")
    actual = subprocess.check_output([str(executable), "-I", "-B", "-c",
        "import sys; print('.'.join(map(str,sys.version_info[:3])))"],
        cwd=root, env=environment, text=True, timeout=30).strip()
    if actual != version:
        raise SystemExit("bootstrap Python version differs from the exact caller pin")
    interpreter = str(executable)
directory = location(root, ref, os.environ.get("TOOLS_ROOT"))
if not directory.exists():
    if mode != "setup" or os.environ.get("TOOLS_ROOT"):
        raise SystemExit("exact SDK source is absent; run explicit setup")
    directory.mkdir(parents=True)
    git(directory, "init", "-q")
    git(directory, "remote", "add", "origin", "https://github.com/nicodes/tools.git")
    git(directory, "fetch", "--quiet", "--depth=1", "https://github.com/nicodes/tools.git", ref)
    git(directory, "checkout", "--quiet", "--detach", ref)
verify(directory, ref)
os.environ.update(TOOLS_ROOT=str(directory),
    MISE_AUTO_INSTALL="false", MISE_EXEC_AUTO_INSTALL="false", MISE_TASK_RUN_AUTO_INSTALL="false",
    PYTHONDONTWRITEBYTECODE="1")
if arguments:
    name, *argv = arguments
    if not re.fullmatch(r"helpers/[a-z][a-z0-9_-]*\\.py", name):
        raise SystemExit("select an explicit generic SDK Python entrypoint")
    entry = directory / name
    if entry.is_symlink() or not entry.is_file():
        raise SystemExit("SDK entrypoint must be a tracked regular file")
    git(directory, "ls-files", "--error-unmatch", "--", name)
    if os.name == "nt":
        raise SystemExit(subprocess.run(python_entry(interpreter, entry, *argv)).returncode)
    os.execv(interpreter, python_entry(interpreter, entry, *argv))
elif mode != "setup":
    raise SystemExit("an explicit SDK entrypoint is required")
'''


# This reviewed literal transports the readable seed unchanged. Compression is
# performed only when updating this source; rendering never depends on the
# local zlib encoder version. The loader is shared with standalone consumers.
SEED_LENGTH = 9128
SEED_SHA256 = 'e2fbd9cfbf66a934bc13d07cf09b7bbb593e1e6e4b57c1eedd4d3a905478f970'
ENCODED_SEED = (
    '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'
    'PrDfNfNAu5CpDGJMQZX/AQO+o1k='
)
SEED_LOADER = (
    'import base64,hashlib,sys,zlib;'
    '_sdk_decoder=zlib.decompressobj();'
    '_sdk_seed=_sdk_decoder.decompress(base64.b64decode("{encoded}",validate=True),{limit});'
    '(_sdk_decoder.eof and not _sdk_decoder.unused_data and not _sdk_decoder.unconsumed_tail '
    'and len(_sdk_seed)=={length} and hashlib.sha256(_sdk_seed).hexdigest()=="{sha256}") '
    'or sys.exit("SDK bootstrap seed failed integrity validation");'
    'exec(compile(_sdk_seed,"<immutable SDK bootstrap>","exec"))'
)


def seed_transport(seed, *, encoded, length, sha256):
    """Render the one fixed, bounded transport; never compress source at runtime."""
    if (not isinstance(seed, str) or type(length) is not int or not 0 < length <= 65536
            or not isinstance(encoded, str) or len(encoded) > 65536
            or not isinstance(sha256, str) or not re.fullmatch('[a-f0-9]{64}', sha256)):
        raise ValueError('SDK bootstrap requires one reviewed bounded seed transport')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.b64decode(encoded, validate=True), length + 1)
    except (ValueError, zlib.error) as error:
        raise ValueError('SDK bootstrap seed failed integrity validation') from error
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or len(raw) != length or hashlib.sha256(raw).hexdigest() != sha256
            or raw != seed.encode('utf-8')):
        raise ValueError('SDK bootstrap seed failed integrity validation')
    source = SEED_LOADER.format(encoded=encoded, limit=length + 1, length=length, sha256=sha256)
    if '\n' in source or '\r' in source or len(source.encode('ascii')) > 7000:
        raise ValueError('SDK bootstrap transport must remain one bounded native command line')
    return source


def command(revision, *, pinned_python=False):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('SDK bootstrap source must be one full immutable commit')
    # The host Python seed reads only bounded source data. A requested caller
    # Python is selected by its literal version before loading any SDK module.
    source = seed_transport(SEED, encoded=ENCODED_SEED, length=SEED_LENGTH, sha256=SEED_SHA256)
    return 'exec python3 -I -B -c ' + shlex.quote(source) + ' ' + revision + (' pinned' if pinned_python else ' host')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['render'])
    parser.add_argument('--ref', required=True)
    parser.add_argument('--pinned-python', action='store_true')
    args = parser.parse_args()
    try:
        print(command(args.ref, pinned_python=args.pinned_python))
    except ValueError as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
