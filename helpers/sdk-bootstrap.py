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
if mode == "setup":
    subprocess.run([str(interpreter), "-I", "-B", "-c", 'exec(\\'import os\\\\nfrom pathlib import Path\\\\nimport stat\\\\nimport sys\\\\nif os.name == "nt" and sys.version_info < (3, 13):\\\\n    raise SystemExit("Windows private SDK directories require the selected Python 3.13 or newer")\\\\nparent = Path.cwd() / ".artifacts"\\\\nfor candidate in [parent, *parent.parents]:\\\\n    try:\\\\n        info = candidate.lstat()\\\\n    except FileNotFoundError:\\\\n        continue\\\\n    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:\\\\n        raise SystemExit("SDK artifact parent must not traverse links or reparse points")\\\\nif parent.exists():\\\\n    if not parent.is_dir():\\\\n        raise SystemExit("SDK artifact parent must be a directory")\\\\nelse:\\\\n    parent.mkdir(mode=0o700)\\\\n\\')'],
        cwd=root, env=dict(os.environ, MISE_AUTO_INSTALL="false", MISE_EXEC_AUTO_INSTALL="false",
            MISE_TASK_RUN_AUTO_INSTALL="false"), check=True, timeout=30)
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
if (root / ".artifacts").exists() or (root / ".artifacts").is_symlink():
    protection = no_links(directory / "helpers/private_directory.py")
    if not protection.is_file():
        raise SystemExit("exact SDK source lacks its artifact protection helper")
    git(directory, "ls-files", "--error-unmatch", "--", "helpers/private_directory.py")
    subprocess.run(python_entry(interpreter, protection, "protect"), cwd=root,
        env=os.environ, check=True, timeout=30)
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
SEED_LENGTH = 10602
SEED_SHA256 = '9d75655b5ba40f74b3503f739d5d112887705667fb96a32224351e73ece93a31'
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
