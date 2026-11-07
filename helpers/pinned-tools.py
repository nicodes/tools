#!/usr/bin/env python3
"""Execute native argv with only its selected, already-installed caller SDKs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

MAX_SOURCE = 262144
MAX_ENVIRONMENT = 1 << 20
PIN = re.compile(r'(?:[a-z][a-z0-9-]*-)?v?\d+\.\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9_.-]+)?|[a-f0-9]{40}\Z')
TOOL = re.compile(r'[A-Za-z0-9][A-Za-z0-9:/.@_-]{0,199}\Z')
EXECUTABLE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}\Z')
DISABLED = {'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false',
            'MISE_TASK_RUN_AUTO_INSTALL': 'false'}


def bindings(values):
    if not values or len(values) > 32:
        raise ValueError('select the actual installed SDK executables required by this stage')
    result = []
    for value in values:
        tool, separator, executable = value.partition('=')
        if not separator or not TOOL.fullmatch(tool) or not EXECUTABLE.fullmatch(executable):
            raise ValueError('SDK binding must be a literal tool=executable pair')
        if (tool, executable) in result or any(name == executable for _, name in result):
            raise ValueError('SDK executables must have one distinct caller-owned binding')
        result.append((tool, executable))
    return result


def pins(root):
    path = Path(root) / 'mise.toml'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE:
        raise ValueError('SDK activation requires bounded regular caller Mise source')
    return tomllib.loads(path.read_text()).get('tools', {})


def query(arguments, root, environment):
    result = subprocess.run(['mise', *arguments], cwd=root, env=environment,
                            text=True, capture_output=True, timeout=30)
    if result.returncode or len(result.stdout.encode()) > MAX_ENVIRONMENT:
        raise ValueError('required exact caller SDK is not prepared; run explicit setup')
    return result.stdout


def activation(root, required, environment=None, *, lookup=query):
    root = Path(root).resolve()
    inherited = dict(os.environ if environment is None else environment)
    base = {**inherited, **DISABLED}
    declared = pins(root)
    installs, selectors = {}, {}
    for tool, _ in required:
        if tool in installs:
            continue
        value = declared.get(tool)
        version = value.get('version') if isinstance(value, dict) else value
        if not isinstance(version, str) or not PIN.fullmatch(version):
            raise ValueError('required SDK needs an exact existing caller Mise pin: ' + tool)
        selector = tool + '@' + version
        selectors[tool] = selector
        text = lookup(['where', selector], root, base).strip()
        if not text or '\n' in text or '\0' in text:
            raise ValueError('required SDK must resolve to one installed directory: ' + tool)
        directory = Path(text)
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError('required exact caller SDK is not installed: ' + tool)
        installs[tool] = directory.resolve()
    raw = lookup(['env', '--json', *selectors.values()], root, base)
    values = json.loads(raw)
    if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                           or '\0' in k or '\0' in v for k, v in values.items()):
        raise ValueError('SDK activation must return bounded string environment data')
    result = {**base, **values, **DISABLED}
    for key, value in inherited.items():
        if key.startswith('GITHUB_') or key in {'GH_TOKEN', 'GITHUB_TOKEN', 'TOOLS_VERIFICATION_SESSION'}:
            if result.get(key) != value:
                raise ValueError('SDK activation must preserve source and execution identity')
    for tool, executable in required:
        path = shutil.which(executable, path=result.get('PATH', ''))
        if path is None or not Path(path).resolve().is_relative_to(installs[tool]):
            raise ValueError('required executable is absent from its exact installed SDK: ' + executable)
    return result


def execute(root, required, command, environment=None):
    if not command or any(not isinstance(value, str) or '\0' in value for value in command):
        raise ValueError('an unchanged literal native command is required')
    environment = activation(root, required, environment)
    return subprocess.run(command, cwd=root, env=environment).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['exec'])
    parser.add_argument('--tool', action='append', default=[])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    arguments = sys.argv[1:]
    if '--' not in arguments:
        parser.error('separate the unchanged native argv with --')
    separator = arguments.index('--')
    args = parser.parse_args(arguments[:separator])
    try:
        code = execute(args.root, bindings(args.tool), arguments[separator + 1:])
        if code < 0:
            import signal
            signal.signal(-code, signal.SIG_DFL)
            os.kill(os.getpid(), -code)
        raise SystemExit(code)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
