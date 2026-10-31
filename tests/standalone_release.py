"""Build an unrelated CLI and validate release archives without a deployment runner."""
import importlib.util
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

spec = importlib.util.spec_from_file_location('cli_release', Path(__file__).resolve().parents[1] / 'scripts/cli_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def main():
    cargo = subprocess.check_output(['rustup', 'which', '--toolchain', '1.94.0', 'cargo'], text=True).strip()
    rustc = subprocess.check_output(['rustup', 'which', '--toolchain', '1.94.0', 'rustc'], text=True).strip()
    os.environ['TOOLS_FIXTURE_CARGO'] = cargo
    os.environ['RUSTC'] = rustc
    with tempfile.TemporaryDirectory(prefix='standalone-cli-') as temporary:
        root = Path(temporary)
        source = root / 'source'
        source.mkdir()
        (source / '.gitignore').write_text('/.artifacts/cargo-target/\n')
        (source / 'src').mkdir()
        (source / 'Cargo.toml').write_text('[package]\nname="mailroom"\nversion="0.0.1"\nedition="2024"\n')
        (source / 'Cargo.lock').write_text('version = 4\n\n[[package]]\nname = "mailroom"\nversion = "0.0.1"\n')
        (source / 'src/main.rs').write_text('fn main() { println!("mailroom ready"); }\n')
        (source / 'build_fixture.py').write_text('''import hashlib,os,platform,subprocess,tarfile
from pathlib import Path
root=Path.cwd()
target=root/'.artifacts/cargo-target'
subprocess.run([os.environ['TOOLS_FIXTURE_CARGO'],'build','--locked','--target-dir',str(target)],check=True)
output=root/'dist'
output.mkdir()
arch='arm64' if platform.machine() in ('aarch64','arm64') else 'x86_64'
archive=output/f'mailroom_{platform.system()}_{arch}.tar.gz'
with tarfile.open(archive,'w:gz') as payload:
    payload.add(target/'debug/mailroom',arcname='mailroom')
(output/'checksums.txt').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\\n')
''')
        subprocess.run([cargo, 'generate-lockfile'], cwd=source, check=True)
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
                        sys.executable,'build_fixture.py'],cwd=source,check=True)
        arch = 'arm64' if platform.machine() in ('aarch64', 'arm64') else 'x86_64'
        archive_path = archives / f'mailroom_{platform.system()}_{arch}.tar.gz'
        release.archive_binary(archive_path, 'mailroom')
        assert release.checksums(archives)[archive_path.name]
        # One native artifact is insufficient to publish a four-platform release.
        try:
            release.validate(archives, 'mailroom')
        except ValueError:
            pass
        else:
            raise AssertionError('incomplete native set was admitted for publication')
        native = root / 'native'
        native.mkdir()
        with tarfile.open(archive_path) as archive:
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
        assert len(build['artifacts'])==2
        assert not (source/'deploy/release.json').exists()
        assert not (source/'komizo.json').exists()
        print('Standalone Rust CLI builds and runs natively with source/test evidence; incomplete release sets are refused.')


if __name__ == '__main__':
    main()
