#!/usr/bin/env python3
"""Run native Godot assertions with deadlines, isolated homes and exact sentinels."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile


def pattern(value, *, count=False):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024 or not value.startswith('^') or not value.endswith('$'):
        raise ValueError('assertion policies require bounded anchored expressions')
    try:
        compiled = re.compile(value)
    except re.error as error:
        raise ValueError('invalid native assertion expression') from error
    if (count and compiled.groups != 1) or compiled.groups > 1:
        raise ValueError('assertion expressions may capture one positive assertion count')
    return compiled


def run_command(command, sentinel=None, timeout=30, environment=None, *, sentinel_regex=None,
                assertions_regex=None, allow_errors=(), editor=False, audio_driver=None,
                allow_error_version=None):
    if sentinel is not None and (not isinstance(sentinel, str) or not sentinel or '\n' in sentinel or '\r' in sentinel):
        raise ValueError('one exact assertion sentinel is required')
    reached = pattern(sentinel_regex)
    assertions = pattern(assertions_regex, count=True)
    if sentinel is None and reached is None:
        raise ValueError('a reached assertion sentinel is required')
    if type(editor) is not bool or audio_driver not in {None, 'Dummy'}:
        raise ValueError('invalid native Godot execution policy')
    if not isinstance(allow_errors, (list, tuple)) or len(allow_errors) > 16:
        raise ValueError('engine diagnostic allowances must be bounded')
    if any(not isinstance(value, str) for value in allow_errors):
        raise ValueError('engine diagnostic allowances must be explicit expressions')
    allowed = [pattern(value) for value in allow_errors]
    selected_environment = environment if environment is not None else os.environ
    if allowed and (not editor or not allow_error_version
                    or selected_environment.get('GODOT_VERSION') != allow_error_version):
        raise ValueError('editor diagnostic allowances must match the reviewed pinned Godot version')
    if not 0 < timeout <= 3600:
        raise ValueError('Godot assertion timeout must be bounded')
    with tempfile.TemporaryDirectory(prefix='godot-test-') as directory:
        isolated = {**selected_environment, 'HOME': directory,
                    'XDG_CONFIG_HOME': str(Path(directory)/'config'),
                    'XDG_CACHE_HOME': str(Path(directory)/'cache')}
        if os.name == 'nt':
            isolated.update(USERPROFILE=directory, APPDATA=str(Path(directory)/'roaming'),
                            LOCALAPPDATA=str(Path(directory)/'local'))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, env=isolated,
                                   start_new_session=os.name != 'nt')
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                output, _ = process.communicate()
            print(output, end='', flush=True)
            raise ValueError('native assertion exceeded its deadline') from None
    print(output, end='', flush=True)
    plain = re.sub(r'\x1b\[[0-9;]*m', '', output)
    lines = plain.splitlines()
    diagnostic_lines = [line for line in lines if not any(rule.fullmatch(line) for rule in allowed)]
    if process.returncode or any(re.search(r'(?:^|\s)(?:ERROR:|SCRIPT ERROR:|FAIL:)', line) for line in diagnostic_lines):
        raise ValueError('native test failed or reported an engine error')
    if sentinel is not None and lines.count(sentinel) != 1:
        raise ValueError('native test did not reach exactly one assertion sentinel')
    for expression in [reached, assertions]:
        if expression is None:
            continue
        matches = [match for line in lines if (match := expression.fullmatch(line))]
        if len(matches) != 1:
            raise ValueError('native test did not reach exactly one assertion counter')
        if expression.groups and (not matches[0][1].isdigit() or int(matches[0][1]) <= 0):
            raise ValueError('native test must reach a positive assertion count')


def run(binary, project, program, sentinel=None, timeout=30, arguments=(), environment=None, **policy):
    command = [str(binary), '--headless']
    if policy.get('editor'):
        command.append('--editor')
    if policy.get('audio_driver'):
        command += ['--audio-driver', policy['audio_driver']]
    command += ['--path', str(project), '--script', str(program), *arguments]
    run_command(command, sentinel, timeout, environment, **policy)


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
    programs = parser.add_mutually_exclusive_group(required=True)
    programs.add_argument('--directory', type=Path)
    programs.add_argument('--program', '--script', type=Path)
    programs.add_argument('--command', nargs=argparse.REMAINDER)
    parser.add_argument('--sentinel-prefix')
    parser.add_argument('--sentinel')
    parser.add_argument('--sentinel-regex')
    parser.add_argument('--assertions-regex')
    parser.add_argument('--policy', type=json.loads, default={})
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        policy = dict(args.policy)
        if args.sentinel_regex:
            policy['sentinel_regex'] = args.sentinel_regex
        if args.assertions_regex:
            policy['assertions_regex'] = args.assertions_regex
        if args.directory is not None:
            if not args.sentinel_prefix or policy:
                raise ValueError('suite discovery requires its exact prefix; use program entries for count/editor policies')
            suite(args.binary, args.project.resolve(), args.directory.resolve(), args.sentinel_prefix, args.timeout)
        elif args.program is not None:
            run(args.binary, args.project.resolve(), args.program.resolve(), args.sentinel, args.timeout, **policy)
        else:
            if not args.command:
                raise ValueError('a native command is required')
            run_command(args.command, args.sentinel, args.timeout, **policy)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
