#!/usr/bin/env python3
"""Build and test the exact Caddy source in disposable local storage."""
from pathlib import Path
import importlib.util
import shutil
import subprocess
import tempfile

source = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix='caddy-engineering-') as directory:
    root = Path(directory)
    for original, target in [('caddy.go.mod', 'go.mod'), ('caddy.go.sum', 'go.sum'), ('caddy-main.go', 'main.go')]:
        shutil.copyfile(source/original, root/target)
    subprocess.run(['sh', str(source/'caddy-build.sh'), '--test'], cwd=root, check=True, timeout=900)
    spec = importlib.util.spec_from_file_location('image_scan', source/'scan-image.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    report = subprocess.check_output(['govulncheck', '-mode=binary', '-json', str(root/'caddy')], text=True, timeout=600)
    verdict = scanner.judge_report(report)
    if verdict['reached']:
        raise ValueError(f'Caddy has vulnerable linked symbols: {verdict["reached"]}')
    print('Caddy: upstream race tests and live binary vulnerability scan passed.')
