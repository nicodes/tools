"""Read-only secret provenance policy with an explicitly prepared, hash-locked YAML SDK."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from configuration import relative

TOOLS = Path(__file__).resolve().parents[1]
REQUIREMENTS = TOOLS / 'helpers/source-secret-policy-requirements.txt'
CHECKER = TOOLS / 'helpers/source-secret-policy.py'
SDK = '.artifacts/sdk/source-policy'
SERVICE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')


def declaration(root, config):
    value = config.get('lint', {}).get('secret_policy')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {'compose', 'workflows', 'scoped_env_dir', 'host_only', 'secrets_env_services'}
            or not {'compose', 'workflows'} <= value.keys()):
        raise ValueError('source secret policy requires native Compose/workflow inputs and bounded reader data')
    compose = relative(root, value['compose'], exists=True)
    workflows = value['workflows']
    if not isinstance(workflows, list) or not workflows or len(workflows) > 8 or len(set(workflows)) != len(workflows):
        raise ValueError('source secret policy requires a closed list of native delivery workflows')
    files = [relative(root, name, exists=True) for name in workflows]
    if any(not path.is_file() or path.stat().st_size > 1024 * 1024 for path in [compose, *files]):
        raise ValueError('source policy inputs must be bounded regular native files')
    scoped = value.get('scoped_env_dir', 'secrets/current')
    if not isinstance(scoped, str):
        raise ValueError('scoped environment prefix must be literal source data')
    if scoped:
        relative(root, scoped)
    host = value.get('host_only', {})
    if not isinstance(host, dict) or len(host) > 32:
        raise ValueError('host-only files require explicitly declared service readers')
    for path, readers in host.items():
        relative(root, path)
        services(readers)
    readers = value.get('secrets_env_services', [])
    services(readers, empty=True)
    return {'compose': value['compose'], 'workflows': workflows, 'scoped_env_dir': scoped,
            'host_only': host, 'secrets_env_services': readers}


def services(values, *, empty=False):
    if (not isinstance(values, list) or (not values and not empty) or len(values) > 32
            or any(not isinstance(name, str) or not SERVICE.fullmatch(name) for name in values)
            or len(set(values)) != len(values)):
        raise ValueError('source secret policy readers must be distinct literal native service names')


def inputs(root):
    directory = relative(root, SDK)
    python = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    identity = {'requirements': hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
                'checker': hashlib.sha256(CHECKER.read_bytes()).hexdigest(),
                'python': list(sys.version_info[:3])}
    return directory, python, identity


def setup(root, config, environment, run=subprocess.run, *, required=False):
    if declaration(root, config) is None and not required:
        return
    directory, python, identity = inputs(root)
    receipt = directory / '.cookbook-source-policy.json'
    receipt.unlink(missing_ok=True)
    directory.parent.mkdir(parents=True, exist_ok=True)
    run([sys.executable, '-I', '-m', 'venv', '--copies', str(directory)],
        cwd=root, env=environment, check=True, timeout=180)
    relative(root, python.relative_to(root).as_posix(), exists=True)
    run([str(python), '-I', '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
         '--no-cache-dir', '--no-deps', '--require-hashes', '--only-binary', ':all:',
         '--index-url', 'https://pypi.org/simple', '-r', str(REQUIREMENTS)],
        cwd=root, env=environment, check=True, timeout=180)
    receipt.write_text(json.dumps(identity, sort_keys=True) + '\n')


def interpreter(root):
    directory, python, identity = inputs(root)
    receipt = relative(root, (directory / '.cookbook-source-policy.json').relative_to(root).as_posix(), exists=True)
    if json.loads(receipt.read_text()) != identity:
        raise ValueError('source secret policy SDK is stale; run make setup')
    return str(relative(root, python.relative_to(root).as_posix(), exists=True))


def check(root, config, environment, run):
    value = declaration(root, config)
    if value is None:
        return
    command = [interpreter(root), '-I', str(CHECKER), '--compose', value['compose'],
               '--scoped-env-dir', value['scoped_env_dir']]
    for path in value['workflows']:
        command += ['--workflow', path]
    for path, readers in value['host_only'].items():
        command += ['--host-only', path + ': ' + ','.join(readers)]
    if value['secrets_env_services']:
        command += ['--secrets-env-services', ','.join(value['secrets_env_services'])]
    run(command, root, environment)


if __name__ == '__main__':
    root = Path.cwd().resolve()
    if sys.argv[1:] == ['setup']:
        setup(root, {}, dict(os.environ), required=True)
    elif sys.argv[1:] == ['test']:
        subprocess.run(['bash', str(TOOLS / 'tests/source-secret-policy.test.sh')], check=True,
                       env=dict(os.environ, SOURCE_POLICY_PYTHON=interpreter(root)), timeout=180)
    else:
        raise SystemExit('usage: source_policy.py setup|test')
