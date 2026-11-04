#!/usr/bin/env python3
"""Run native Godot assertions with deadlines, isolated homes and exact sentinels."""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile


def run(binary, project, program, sentinel, timeout=30, arguments=(), environment=None):
    if not sentinel or '\n' in sentinel or '\r' in sentinel:
        raise ValueError('one exact assertion sentinel is required')
    if not 0 < timeout <= 3600:
        raise ValueError('Godot assertion timeout must be bounded')
    with tempfile.TemporaryDirectory(prefix='godot-test-') as directory:
        command = [str(binary), '--headless', '--path', str(project), '--script', str(program), *arguments]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, env={**(environment or os.environ), 'HOME': directory},
                                   start_new_session=os.name != 'nt')
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                output, _ = process.communicate()
            print(output, end='', flush=True)
            raise ValueError('Godot assertion exceeded its deadline: ' + str(program)) from None
    print(output, end='', flush=True)
    plain = re.sub(r'\x1b\[[0-9;]*m', '', output)
    if (process.returncode or re.search(r'(?m)(?:^|\s)(?:ERROR:|SCRIPT ERROR:|FAIL:)', plain)
            or plain.splitlines().count(sentinel) != 1):
        raise ValueError('Godot test failed or did not reach its assertion: ' + str(program))


def suite(binary, project, directory, prefix, timeout=30, environment=None):
    programs = sorted(Path(directory).glob('*_test.gd'))
    if not programs:
        raise ValueError('no native Godot test scripts found')
    for program in programs:
        if program.is_symlink() or not program.is_file():
            raise ValueError('native Godot tests must be regular source files')
        run(binary, project, program, prefix + ' ' + program.stem, timeout, environment=environment)
    print('Godot assertions reached: ' + str(len(programs)) + '/' + str(len(programs)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default=os.environ.get('GODOT_BIN', 'godot'))
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--sentinel-prefix', required=True)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        suite(args.binary, args.project.resolve(), args.directory.resolve(), args.sentinel_prefix, args.timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
