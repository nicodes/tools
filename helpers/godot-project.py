#!/usr/bin/env python3
"""Strictly import a native Godot project while preserving its authored settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import importlib.util

_spec = importlib.util.spec_from_file_location('_godot_project_import', Path(__file__).with_name('godot_import.py'))
godot_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(godot_import)


def files(directory, ignored=('build', 'node_modules'), ignored_patterns=()):
    result = {}
    directory = Path(directory)
    for current, folders, names in os.walk(directory, followlinks=False):
        folders[:] = sorted(name for name in folders if name not in ignored and not name.startswith('.'))
        for name in sorted(names):
            if name.startswith('.') and name != '.gdignore':
                continue
            path = Path(current) / name
            relative = path.relative_to(directory)
            if any(relative.match(pattern) for pattern in ignored_patterns):
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('import source must contain only regular files')
            with path.open('rb') as incoming:
                result[relative.as_posix()] = hashlib.file_digest(incoming, 'sha256').hexdigest()
        for name in folders:
            if (Path(current) / name).is_symlink():
                raise ValueError('import source directories may not be symbolic links')
    return result


def outputs(project):
    root = project / '.godot'
    result = {'imported/' + name: digest for name, digest in files(root / 'imported').items()}
    for name in ['global_script_class_cache.cfg', 'uid_cache.bin']:
        path = root / name
        if path.is_symlink():
            raise ValueError('Godot cache metadata may not be a symbolic link')
        if not path.is_file():
            return None
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def import_project(project, binary, run, timeout=180, arguments=(), ignored_patterns=(), preserve_defaults=False):
    if Path(project).is_symlink():
        raise ValueError('Godot project may not be a symbolic link')
    project = Path(project).resolve()
    config = project / 'project.godot'
    if config.is_symlink() or not config.is_file():
        raise ValueError('expected a regular native Godot project')
    if type(preserve_defaults) is not bool:
        raise ValueError('import preservation must be explicitly enabled or disabled')
    if preserve_defaults:
        from godot_authoring import preserve_imports
        native_run = run
        run = lambda argv, timeout: preserve_imports(project, lambda: native_run(argv, timeout=timeout))
    authored = godot_import._Settings(config)
    original = authored.original
    engine = subprocess.check_output([str(binary), '--version'], text=True, timeout=30).strip()
    cache = project / '.godot/imported'
    state = project / '.godot/cookbook-import.json'
    if (project / '.godot').is_symlink() or cache.is_symlink() or state.is_symlink():
        raise ValueError('Godot import cache may not contain symbolic links')
    identity = {'engine': engine, 'files': files(project, ignored_patterns=ignored_patterns)}
    warm = False
    if state.is_file() and cache.is_dir():
        receipt = json.loads(state.read_text())
        warm = receipt.get('inputs') == identity and receipt.get('outputs') is not None and receipt['outputs'] == outputs(project)
    state.unlink(missing_ok=True)
    if not warm and cache.exists():
        shutil.rmtree(cache)
    bootstrap = re.sub(rb'(?m)^theme/custom="[^"\r\n]*"$', b'theme/custom=""', original)
    command = [str(binary), '--headless', '--editor', '--quit', '--path', str(project), '--import', *arguments]
    if not warm and bootstrap != original:
        try:
            authored.write(bootstrap)
            with godot_import.serial_import(project):
                if run(command, timeout=timeout):
                    raise ValueError('Godot bootstrap import failed')
        finally:
            authored.restore()
    with godot_import.serial_import(project):
        if run(command, timeout=timeout):
            raise ValueError('Godot strict import failed')
    if cache.is_dir() and outputs(project) is not None:
        state.write_text(json.dumps({'inputs': {'engine': engine, 'files': files(project, ignored_patterns=ignored_patterns)},
                                     'outputs': outputs(project)}, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--binary', default='godot')
    args = parser.parse_args()
    # Import the recipe-owned strict process runner without depending on product code.
    import importlib.util
    spec = importlib.util.spec_from_file_location('game_import_runner', Path(__file__).with_name('godot-game-runner.py'))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    import_project(args.project, args.binary, runner.main)


if __name__ == '__main__':
    main()
