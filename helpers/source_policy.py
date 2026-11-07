"""Read-only secret provenance policy with an explicitly prepared, hash-locked YAML SDK."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
_spec = importlib.util.spec_from_file_location('_source_policy_paths', Path(__file__).with_name('image_import_cache.py'))
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
relative = _paths.relative

TOOLS = Path(__file__).resolve().parents[1]
REQUIREMENTS = TOOLS / 'helpers/source-secret-policy-requirements.txt'
CHECKER = TOOLS / 'helpers/source-secret-policy.py'
SDK = '.artifacts/sdk/source-policy'
SERVICE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')




def inputs(root):
    directory = relative(root, SDK)
    python = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    identity = {'requirements': hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
                'checker': hashlib.sha256(CHECKER.read_bytes()).hexdigest(),
                'python': list(sys.version_info[:3])}
    return directory, python, identity


def setup(root, environment, run=subprocess.run):
    pins = tomllib.loads(relative(root, 'mise.toml', exists=True).read_text()).get('tools', {})
    if pins.get('python') != '.'.join(map(str, sys.version_info[:3])):
        raise ValueError('source-policy setup requires the exact caller-pinned Python SDK')
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
        raise ValueError('source secret policy SDK is stale; run explicit setup')
    return str(relative(root, python.relative_to(root).as_posix(), exists=True))



if __name__ == '__main__':
    root = Path.cwd().resolve()
    if sys.argv[1:] == ['setup']:
        setup(root, dict(os.environ))
    elif sys.argv[1:] == ['actions']:
        subprocess.run([interpreter(root), '-I', str(TOOLS / 'helpers/composite-actions.py'), str(root)], check=True,
                       env=dict(os.environ), timeout=60)
    elif sys.argv[1:] == ['test']:
        subprocess.run(['bash', str(TOOLS / 'tests/source-secret-policy.test.sh')], check=True,
                       env=dict(os.environ, SOURCE_POLICY_PYTHON=interpreter(root)), timeout=180)
    elif sys.argv[1:2] == ['check']:
        arguments = sys.argv[2:]
        sdk_root = root
        if arguments[:1] == ['--sdk-root']:
            if len(arguments) < 3:
                raise SystemExit('check --sdk-root requires a prepared source checkout and checker arguments')
            sdk_root = Path(arguments[1]).resolve(strict=True)
            arguments = arguments[2:]
        raise SystemExit(subprocess.run([interpreter(sdk_root), '-I', str(CHECKER), *arguments],
                                       cwd=root, env=dict(os.environ), timeout=180).returncode)
    else:
        raise SystemExit('usage: source_policy.py setup|actions|test|check [checker arguments]')
