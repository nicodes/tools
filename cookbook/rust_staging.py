"""Compile and immediately preserve each application's declared native variant."""
from pathlib import Path
import shutil

from configuration import relative
import rust_component


def artifact_paths(declared):
    result = []
    for component in declared:
        if component.get('build', {}).get('enabled', True) is False:
            continue
        options = component | component.get('build', {})
        directory = Path(component.get('output_directory', '.artifacts'))
        result += [(directory / name).as_posix() for name in options.get('binaries', [])]
    if len(set(result)) != len(result):
        raise ValueError('native variants must preserve distinct declared output files')
    return result


def compile_and_stage(root, declared, records, environment, run):
    # Building all variants before copying can silently copy the final feature
    # variant into every fixture when Cargo shares its dependency cache.
    artifact_paths(declared)
    for original, record in zip(declared, records, strict=True):
        if record.get('build', {}).get('enabled', True) is False:
            continue
        rust_component.build(root, [record], environment, run)
        options = record | record.get('build', {})
        target = Path(environment.get('CARGO_TARGET_DIR', str(root / Path(record['manifest']).parent / 'target')))
        if record.get('target_directory'):
            target = relative(root, record['target_directory'])
        if record.get('target'):
            target /= record['target']
        target /= 'release' if options.get('profile', 'release') == 'release' else 'debug'
        destination = relative(root, original.get('output_directory', '.artifacts'))
        destination.mkdir(parents=True, exist_ok=True)
        for name in options.get('binaries', []):
            source = target / name
            if source.is_symlink() or not source.is_file():
                raise ValueError('native compiler output must be a regular declared executable')
            shutil.copy2(source, destination / name)
