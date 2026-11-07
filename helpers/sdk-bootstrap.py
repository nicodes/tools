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
    os.execv(interpreter, python_entry(interpreter, entry, *argv))
elif mode != "setup":
    raise SystemExit("an explicit SDK entrypoint is required")
'''


# This reviewed literal transports the readable seed unchanged. Compression is
# performed only when updating this source; rendering never depends on the
# local zlib encoder version. The loader is shared with standalone consumers.
SEED_LENGTH = 9011
SEED_SHA256 = 'f60b8808b08a5fcba21d7e1777f7c1db094480b03e616923cb910c1c992559f0'
ENCODED_SEED = (
    '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'
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
