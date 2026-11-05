#!/usr/bin/env python3
"""Export caller-selected presets from an isolated, strictly imported project."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def checked(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=600)
    print(result.stdout, end='')
    if result.returncode or re.search(r'(?m)^(?:SCRIPT ERROR:|ERROR:|FAIL:)', result.stdout):
        raise RuntimeError('Godot import/export failed; retained output above')


def export(project, presets, binary='godot', run=checked):
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
        command = [binary, '--headless', '--editor', '--quit', '--path', str(copied), '--import']
        if bootstrap != original:
            try:
                config.write_bytes(bootstrap)
                run(command)
            finally:
                config.write_bytes(original)
        run(command)
        for preset, output, kind in normalized:
            output.parent.mkdir(parents=True, exist_ok=True)
            run([binary, '--headless', '--path', str(copied), '--export-' + kind, preset, str(output)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--export', nargs=2, action='append', required=True, metavar=('PRESET', 'OUTPUT'))
    parser.add_argument('--binary', default='godot')
    args = parser.parse_args()
    export(args.project, args.export, args.binary)


if __name__ == '__main__':
    main()
