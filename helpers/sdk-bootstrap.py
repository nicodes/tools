#!/usr/bin/env python3
"""Render the fixed SDK identity bootstrap used by Mise tasks."""
import argparse
from pathlib import Path
import re
import shlex

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


def command(revision, *, pinned_python=False):
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('SDK bootstrap source must be one full immutable commit')
    # The host Python seed reads only bounded source data. A requested caller
    # Python is selected by its literal version before loading any SDK module.
    return 'exec python3 -I -B -c ' + shlex.quote(SEED) + ' ' + revision + (' pinned' if pinned_python else ' host')



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
