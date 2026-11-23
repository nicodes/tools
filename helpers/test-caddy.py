#!/usr/bin/env python3
"""Build and test the exact Caddy source in disposable local storage.

A reachable advisory in the built binary is a warning by default, because a
newly published advisory must not block unrelated work. The scheduled security
workflow passes --fail-on-advisories so the same verdict fails that run.
Build failures, a wrong compiler and failing upstream tests always fail.
"""
from pathlib import Path
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

source = Path(__file__).resolve().parent


def report_advisories(reached, fail):
    message = f'Caddy has vulnerable linked symbols: {reached}'
    if fail:
        raise ValueError(message)
    print(f'::warning title=Caddy advisories::{message}. Not failing this run; the scheduled security scan fails on it.', flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as handle:
            handle.write(f'### Warning: Caddy advisories\n\n{message}\n\nOrdinary tests do not fail on this; the scheduled security scan does.\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--fail-on-advisories', action='store_true',
                        help='fail, instead of warning, when the binary links a vulnerable symbol')
    arguments = parser.parse_args(argv)
    # Resolve the selected compiler before entering scratch: a mise shim there
    # otherwise selects the user's global compiler instead of this repository's pin.
    go_root = subprocess.check_output(['go', 'env', 'GOROOT'], text=True).strip()
    compiler_environment = {**os.environ, 'PATH': str(Path(go_root)/'bin')+os.pathsep+os.environ['PATH']}
    # Scratch has no VCS checkout. Do not inherit metadata from an unrelated
    # parent directory; module checksums and selected compiler bind this fixture.
    compiler_environment['GOFLAGS'] = os.environ.get('GOFLAGS', '') + ' -buildvcs=false'
    selected_version = subprocess.check_output([str(Path(go_root)/'bin/go'), 'version'], text=True).split()[2]
    with tempfile.TemporaryDirectory(prefix='caddy-engineering-') as directory:
        root = Path(directory)
        for original, target in [('caddy.go.mod', 'go.mod'), ('caddy.go.sum', 'go.sum')]:
            shutil.copyfile(source/original, root/target)
        subprocess.run(['sh', str(source/'caddy-build.sh'), '--test'], cwd=root, env=compiler_environment, check=True, timeout=900)
        built_version = subprocess.check_output([str(Path(go_root)/'bin/go'), 'version', '-m', str(root/'caddy')],
                                                text=True).splitlines()[0].split()[-1]
        if built_version != selected_version:
            raise ValueError('Caddy was built with a different compiler from the selected toolchain')
        spec = importlib.util.spec_from_file_location('image_scan', source/'scan-image.py')
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        report = subprocess.check_output(['govulncheck', '-mode=binary', '-json', str(root/'caddy')], text=True, timeout=600)
        verdict = scanner.judge_report(report)
        if verdict['reached']:
            report_advisories(verdict['reached'], arguments.fail_on_advisories)
            print('Caddy: upstream race tests passed; binary vulnerability scan reported advisories.')
        else:
            print('Caddy: upstream race tests and live binary vulnerability scan passed.')


if __name__ == '__main__':
    sys.exit(main())
