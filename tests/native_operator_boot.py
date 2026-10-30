"""Check tmpfiles creates the operator's private write path in an empty root."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this isolated tmpfiles acceptance with sudo.')
    source=Path(__file__).resolve().parents[1]/'operator/internal/operator/assets/fleet-operator.conf'
    with tempfile.TemporaryDirectory(prefix='operator-boot-') as temporary:
        root=Path(temporary)
        configuration=root/'etc/tmpfiles.d'
        configuration.mkdir(parents=True)
        shutil.copyfile(source,configuration/source.name)
        (root/'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
        (root/'etc/group').write_text('root:x:0:\n')
        shared=root/'run/lock'
        shared.mkdir(parents=True)
        shared.chmod(0o1777)
        subprocess.run(['systemd-tmpfiles','--root',str(root),'--create',source.name],check=True)
        locks=root/'run/fleet-operator-locks'
        assert locks.is_dir() and not locks.is_symlink()
        assert locks.stat().st_uid==0 and locks.stat().st_mode & 0o777==0o700
        assert shared.stat().st_mode & 0o7777==0o1777
        print('Operator boot creates its protected lock directory in an empty root.')


if __name__=='__main__':
    main()
