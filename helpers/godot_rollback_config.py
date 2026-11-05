"""Inspect (never start) the exact historical config before granting deployment."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

from godot_release_record import DIGEST, REPO


def validate_stack(stack, gate_ref):
    services = stack.get('services', {})
    if set(services) != {REPO.rsplit('/',1)[-1] + '-gate'}:
        raise ValueError('Rollback config must contain only the declared gate')
    gate = services[REPO.rsplit('/',1)[-1] + '-gate']
    if (gate.get('image') != gate_ref or gate.get('user') != '1000:1000'
            or gate.get('read_only') is not True or gate.get('cap_drop') != ['ALL']
            or 'no-new-privileges:true' not in gate.get('security_opt', [])
            or any(gate.get(key) for key in ('privileged', 'ports', 'volumes', 'devices', 'cap_add', 'build', 'network_mode', 'pid', 'ipc'))):
        raise ValueError('Rollback config does not pin the verified, constrained gate')


def main():
    refs = {}
    for kind in ('gate', 'config'):
        ref = os.environ[kind.upper() + '_REF']
        prefix = f'ghcr.io/{REPO}-{kind}@'
        if not ref.startswith(prefix) or not DIGEST.fullmatch(ref[len(prefix):]):
            raise ValueError('Unexpected immutable rollback image')
        refs[kind] = ref
    subprocess.run(['docker', 'pull', refs['config']], check=True)
    container = subprocess.check_output(['docker', 'create', refs['config']], text=True).strip()
    try:
        with tempfile.TemporaryDirectory(prefix='godot-rollback-') as directory:
            compose = Path(directory) / 'compose.yml'
            subprocess.run(['docker', 'cp', container + ':/config/compose.yml', str(compose)], check=True)
            stack = json.loads(subprocess.check_output(['docker', 'compose', '-f', str(compose),
                'config', '--format', 'json', '--no-interpolate'], text=True))
            validate_stack(stack, refs['gate'])
    finally:
        subprocess.run(['docker', 'rm', container], check=True)
    print('PASS: historical config pins the verified gate with production restrictions')


if __name__ == '__main__':
    main()
