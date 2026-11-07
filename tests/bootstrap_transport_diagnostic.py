"""Observe finite public-source shell transport without executing the seed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SHELL_SPEC = importlib.util.spec_from_file_location('native_transport_shell', Path(__file__).with_name('native_shell.py'))
native_shell = importlib.util.module_from_spec(SHELL_SPEC)
SHELL_SPEC.loader.exec_module(native_shell)
SPEC = importlib.util.spec_from_file_location('native_transport_diagnostic', ROOT / 'helpers/sdk-bootstrap.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def observe(arguments, *, source=None):
    result = subprocess.run(arguments, input=source, capture_output=True, timeout=15)
    return {'exit': result.returncode, 'stdout_bytes': len(result.stdout),
            'stdout_sha256': hashlib.sha256(result.stdout).hexdigest(),
            'stderr': result.stderr.decode('utf-8', 'replace')[:1024]}, result.stdout


def main():
    executable = native_shell.bash()
    original = 'exec python3 -I -B -c ' + shlex.quote(bootstrap.SEED) + ' ' + 'a' * 40 + ' host'
    current = bootstrap.command('a' * 40)
    original_argument, _ = observe([executable, '-n', '-c', original])
    original_stdin, _ = observe([executable, '-n'], source=original.encode('utf-8'))
    current_argument, _ = observe([executable, '-n', '-c', current])
    literal, received = observe([executable, '-euc', 'printf "%s" "$1"', 'public-transport-control', original])
    record = {'platform': os.name, 'selected_bash': executable,
              'readable_seed_bytes': len(bootstrap.SEED.encode()),
              'readable_seed_sha256': hashlib.sha256(bootstrap.SEED.encode()).hexdigest(),
              'original_command_bytes': len(original.encode()), 'current_command_bytes': len(current.encode()),
              'original_parse_through_argument': original_argument,
              'original_parse_through_stdin': original_stdin,
              'current_parse_through_argument': current_argument,
              'unexecuted_literal_transport': {**literal,
                  'expected_bytes': len(original.encode()),
                  'expected_sha256': hashlib.sha256(original.encode()).hexdigest(),
                  'prefix_of_expected': received == original.encode()[:len(received)],
                  'exact': received == original.encode()}}
    print(json.dumps(record, sort_keys=True), flush=True)
    if original_stdin['exit'] or current_argument['exit']:
        raise SystemExit('readable script and compact native command must both remain valid Bash source')


if __name__ == '__main__':
    main()
