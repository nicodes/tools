#!/usr/bin/env python3
"""Export caller-selected presets from an isolated, strictly imported project."""
import argparse
import json
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

def variant_options(project, kinds, privileged_directories=(), source_files=None, source_root=None, *, read_sources=True):
    if (not isinstance(privileged_directories, (list, tuple)) or len(privileged_directories) > 16
            or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name)
                   for name in privileged_directories)
            or len(set(privileged_directories)) != len(privileged_directories)):
        raise ValueError('privileged export directories require distinct literal immediate directory names')
    if privileged_directories and (not kinds or set(kinds) != {'debug'}):
        raise ValueError('privileged directories may be included only by a separate debug export')
    for name in privileged_directories:
        directory = project / name
        if directory.is_symlink() or not directory.is_dir() or any(path.is_symlink() for path in directory.rglob('*')):
            raise ValueError('privileged export directories must be real owned source directories')
    source_files = {} if source_files is None else source_files
    if not isinstance(source_files, dict) or len(source_files) > 16:
        raise ValueError('export configuration requires bounded literal JSON file mappings')
    prepared = {}
    for destination, source in source_files.items():
        if (not isinstance(destination, str) or len(destination) > 240 or not destination.endswith('.json')
                or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', destination)
                or any(part in {'.', '..', '.git', '.godot', 'scripts'} for part in Path(destination).parts)):
            raise ValueError('export configuration destinations require literal native JSON paths')
        if source_root is None or not isinstance(source, (str, Path)):
            raise ValueError('export JSON configurations require their owning checkout root and source paths')
        target = project / destination
        if any(path.is_symlink() for path in [target, *target.parents]):
            raise ValueError('export JSON destinations must not traverse links')
        root = Path(source_root).resolve()
        source = Path(source)
        source = source.absolute() if source.is_absolute() else root / source
        if (not source.is_relative_to(root) or '..' in source.parts or source.suffix != '.json'
                or source.relative_to(root).parts[0] not in {'.artifacts', 'app', 'api', 'core', 'internal', 'deploy'}
                or any(path.is_symlink() for path in [source, *source.parents])):
            raise ValueError('export JSON inputs require regular owned configuration files')
        if not read_sources:
            continue
        if not source.is_file() or not 0 < source.stat().st_size <= 65536:
            raise ValueError('export JSON inputs require bounded regular owned configuration files')
        body = source.read_bytes()
        if len(body) > 65536:
            raise ValueError('export JSON input exceeds its bounded size')
        def nonfinite(_value):
            raise ValueError('export configuration requires finite JSON data')
        json.loads(body, parse_constant=nonfinite)
        prepared[destination] = body
    return prepared


def include_debug_directories(config, directories):
    if not directories:
        return
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 1048576:
        raise ValueError('debug directory selection requires regular bounded export presets')
    required = {name + '/*' for name in directories}
    def replace(match):
        filters = [value.strip() for value in match[1].split(',') if value.strip()]
        if not required <= set(filters):
            raise ValueError('native release presets must explicitly exclude every privileged directory')
        return 'exclude_filter="' + ', '.join(value for value in filters if value not in required) + '"'
    text, count = re.subn(r'(?m)^exclude_filter="([^"\r\n]*)"$', replace, config.read_text())
    if not count:
        raise ValueError('native release presets must explicitly exclude privileged directories')
    config.write_text(text)


def export(project, presets, binary='godot', run=None, *, import_arguments=(), timeout=600, import_timeout=None,
           privileged_directories=(), source_files=None, source_root=None):
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
    prepared = variant_options(project, [value[2] for value in normalized], privileged_directories, source_files, source_root)
    with tempfile.TemporaryDirectory(prefix='godot-export-') as directory:
        copied = Path(directory) / 'project'
        shutil.copytree(project, copied, ignore=shutil.ignore_patterns('.godot', 'build', 'gdam.link.json'))
        include_debug_directories(copied / 'export_presets.cfg', privileged_directories)
        for destination, body in prepared.items():
            target = copied / destination
            if any(path.is_symlink() for path in [target, *target.parents]):
                raise ValueError('export JSON destinations must not traverse links')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
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
