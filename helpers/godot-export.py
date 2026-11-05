#!/usr/bin/env python3
"""Export caller-selected presets from an isolated, strictly imported project."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def checked(command, timeout=600):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout)
    print(result.stdout, end='')
    if result.returncode or re.search(r'(?m)^(?:SCRIPT ERROR:|ERROR:|FAIL:)', result.stdout):
        raise RuntimeError('Godot import/export failed; retained output above')


def native_options(import_arguments=(), timeout=600, import_timeout=None):
    if (not isinstance(import_arguments, (list, tuple)) or len(import_arguments) > 16
            or any(not isinstance(value, str) or not value or '\0' in value for value in import_arguments)):
        raise ValueError('native import arguments must be bounded literal flags')
    position = 0
    while position < len(import_arguments):
        flag = import_arguments[position]
        if flag in {'--verbose', '--no-header'}:
            position += 1
        elif (flag == '--frame-delay' and position + 1 < len(import_arguments)
              and import_arguments[position + 1].isdigit() and 0 <= int(import_arguments[position + 1]) <= 10000):
            position += 2
        else:
            raise ValueError('native import flags may not replace the setup/import operation')
    for deadline in [timeout, import_timeout if import_timeout is not None else timeout]:
        if type(deadline) is not int or not 1 <= deadline <= 3600:
            raise ValueError('native export deadlines must be bounded')

def export(project, presets, binary='godot', run=None, *, import_arguments=(), timeout=600, import_timeout=None):
    native_options(import_arguments, timeout, import_timeout)
    execute = lambda command, importing=False: (run(command) if run is not None else
        checked(command, timeout=import_timeout if importing and import_timeout is not None else timeout))
    project = Path(project).resolve()
    if not (project / 'project.godot').is_file():
        raise ValueError('Expected a Godot project')
    normalized, destinations = [], set()
    for declaration in presets:
        if not isinstance(declaration, (tuple, list)) or len(declaration) not in {2, 3}:
            raise ValueError('Godot export requires preset, output and optional native kind')
        preset, output = declaration[:2]
        kind = declaration[2] if len(declaration) == 3 else 'release'
        if kind not in {'release', 'debug'}:
            raise ValueError('Godot export kind must be release or debug')
        output = Path(output).resolve()
        if output in destinations:
            raise ValueError('Godot export destinations must be distinct')
        destinations.add(output)
        normalized.append((preset, output, kind))
    with tempfile.TemporaryDirectory(prefix='godot-export-') as directory:
        copied = Path(directory) / 'project'
        shutil.copytree(project, copied, ignore=shutil.ignore_patterns('.godot', 'build', 'gdam.link.json'))
        config = copied / 'project.godot'
        original = config.read_bytes()
        bootstrap = re.sub(rb'(?m)^theme/custom="[^"\r\n]*"$', b'theme/custom=""', original)
        command = [binary, '--headless', '--editor', '--quit', '--path', str(copied), '--import', *import_arguments]
        if bootstrap != original:
            try:
                config.write_bytes(bootstrap)
                execute(command, True)
            finally:
                config.write_bytes(original)
        execute(command, True)
        for preset, output, kind in normalized:
            output.parent.mkdir(parents=True, exist_ok=True)
            execute([binary, '--headless', '--path', str(copied), '--export-' + kind, preset, str(output)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--export', nargs=2, action='append', required=True, metavar=('PRESET', 'OUTPUT'))
    parser.add_argument('--binary', default='godot')
    args = parser.parse_args()
    export(args.project, args.export, args.binary)


if __name__ == '__main__':
    main()
