"""Build an unrelated CLI and validate release archives without a deployment runner."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

spec = importlib.util.spec_from_file_location('cli_release', Path(__file__).resolve().parents[1] / 'scripts/cli_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def main():
    with tempfile.TemporaryDirectory(prefix='standalone-cli-') as temporary:
        root = Path(temporary)
        source = root / 'source'
        source.mkdir()
        (source / 'go.mod').write_text('module example.com/mailroom\n\ngo 1.26\n')
        (source / 'main.go').write_text('package main\nimport "fmt"\nfunc main() { fmt.Println("mailroom ready") }\n')
        for args in (['init', '-q'], ['add', '.'],
                     ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'Initial CLI']):
            subprocess.run(['git', '-C', str(source), *args], check=True)
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        # Any accidental deployment-runner invocation fails even on a host
        # whose PATH happens to contain a previously installed CLI.
        blocked = root / 'blocked'
        blocked.mkdir()
        guard = blocked / 'komizo'
        guard.write_text('#!/bin/sh\necho unexpected deployment runner >&2\nexit 99\n')
        guard.chmod(0o755)
        os.environ['PATH'] = str(blocked) + os.pathsep + os.environ['PATH']
        verification=Path(__file__).resolve().parents[1]/'helpers/verification.py'
        archives = source / 'dist'
        subprocess.run([sys.executable,str(verification),'build','--artifact','dist','--',
                        sys.executable,str(Path(release.__file__)),'build','--binary','mailroom',
                        '--directory','dist','--tag','v0.0.1','--source',revision],cwd=source,check=True)
        release.validate(archives, 'mailroom')
        native = root / 'native'
        native.mkdir()
        with tarfile.open(archives / 'mailroom_Linux_x86_64.tar.gz') as archive:
            archive.extract('mailroom', native, filter='data')
        output = subprocess.check_output([str(native / 'mailroom')], text=True)
        assert output == 'mailroom ready\n', output
        subprocess.run([sys.executable,str(verification),'session','--test','native','--',
                        sys.executable,str(verification),'test','--name','native','--artifact','dist','--',
                        str(native/'mailroom')],cwd=source,check=True)
        build=json.loads((source/'.artifacts/verification/build.json').read_text())
        tested=json.loads((source/'.artifacts/verification/test.json').read_text())
        assert build['provenance']['source_commit']==tested['tested_commit']==revision
        assert build['identity']==tested['input_identity']
        assert len(build['artifacts'])==5
        assert not (source/'deploy/release.json').exists()
        assert not (source/'komizo.json').exists()
        print('Standalone CLI builds, runs and validates four archives with source/test evidence and no deployment configuration.')


if __name__ == '__main__':
    main()
