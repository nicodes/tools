#!/usr/bin/env python3
"""Keep platform self-check path selection inside the complete CI workflow."""
import json
import os
from pathlib import Path
import re
import subprocess

GROUPS = {
    'composite': ('podman-ci/**', 'helpers/composite-actions.py', 'tests/test_podman_ci_cache.py'),
    'ubuntu': ('setup/**', 'helpers/select-tools.py'),
    'windows': ('.gitattributes', 'setup/**', 'helpers/verification.py', 'helpers/source_config.py',
        'helpers/sdk_identity.py', 'helpers/sdk-bootstrap.py', 'helpers/pinned-tools.py',
        'helpers/rust_install_context.py', 'helpers/private_directory.py', 'helpers/bootstrap-python.ps1',
        'helpers/source_policy.py', 'tests/test_sdk_identity.py', 'tests/test_sdk_bootstrap.py',
        'tests/test_pinned_tools.py', 'tests/test_rust_runtime_context.py', 'tests/test_rust_install_context.py',
        'tests/test_private_directory.py', 'tests/native_shell.py', 'tests/test_native_shell.py',
        'tests/bootstrap_shell_diagnostic.py', 'tests/bootstrap_transport_diagnostic.py',
        'tests/test_sdk_bootstrap_transport.py', 'tests/test_sdk_bootstrap_child.py', 'tests/test_source_config.py'),
}
SHARED = ('.github/workflows/build-and-test.yml', '.github/scripts/plan-ci.py', 'tests/test_ci_paths.py')


def identity(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{40}', value) is None or value == '0' * 40:
        raise ValueError('CI selection requires an exact nonzero source identity')
    return value


def matches(path, patterns):
    return any(path.startswith(pattern[:-2]) if pattern.endswith('/**') else path == pattern for pattern in patterns)


def selection(event_name, event, ref, root):
    if event_name in ('workflow_dispatch', 'merge_group'):
        return dict(composite=True, ubuntu=True, windows=True)
    if event_name not in ('pull_request', 'push') or event_name == 'push' and ref != 'refs/heads/main':
        raise ValueError('unsupported CI event')
    if event_name == 'pull_request':
        before = identity(event['pull_request']['base']['sha'])
        after = identity(event['pull_request']['head']['sha'])
        arguments = ['diff', '--no-renames', '--name-only', '-z', before + '...' + after]
    else:
        after = identity(event['after'])
        before = event['before']
        if before == '0' * 40:
            arguments = ['ls-tree', '-r', '--name-only', '-z', after]
        else:
            arguments = ['diff', '--no-renames', '--name-only', '-z', identity(before), after]
    raw = subprocess.check_output(['git', *arguments], cwd=root)
    paths = [path.decode('utf-8', 'surrogateescape') for path in raw.split(b'\0') if path]
    return {name: any(matches(path, patterns + SHARED) for path in paths)
            for name, patterns in GROUPS.items()}


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    result = selection(os.environ['GITHUB_EVENT_NAME'], event, os.environ['GITHUB_REF'], Path.cwd())
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        for name, value in result.items():
            output.write(name + '=' + str(value).lower() + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
