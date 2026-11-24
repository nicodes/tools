#!/usr/bin/env python3
"""Watch exact repository tool pins and keep one owned, actionable update issue."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


def updates(root, installed, outdated):
    config = (root / 'mise.toml').resolve()
    result = []
    for name, item in sorted(outdated.items()):
        # Ignore tools inherited from a different mise configuration.
        if name not in installed or Path(item['source']['path']).resolve() != config:
            continue
        entry = installed[name]
        current = entry.get('version') if isinstance(entry, dict) else entry
        latest = item.get('bump') or item.get('latest')
        current_core = decorated_version(name, current, 'repository')
        try:
            latest_core = decorated_version(name, latest, 'upstream')
        except ValueError as error:
            raise ValueError(f'{name}: unrecognized upstream version {latest!r} for exact pin {current}') from error
        if tuple(map(int, latest_core.split('.'))) > tuple(map(int, current_core.split('.'))):
            result.append((name, current, latest))
    return result


def decorated_version(name, version, origin):
    """Numeric core of a decorated table-form pin; unknown shapes fail closed."""
    if not isinstance(version, str):
        raise ValueError(f'{name}: unrecognized {origin} version')
    match = re.fullmatch(r'(?:[a-z]+-)?v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.+-]+)?', version)
    if not match:
        raise ValueError(f'{name}: unrecognized {origin} version')
    return '.'.join(match.groups())


def main():
    root = Path.cwd()
    config = root/'mise.toml'
    if config.is_symlink() or not config.is_file():
        raise ValueError('a regular caller-owned mise.toml is required')
    installed = tomllib.loads(config.read_text()).get('tools', {})
    outdated = json.loads(subprocess.check_output(
        ['mise', 'outdated', '--bump', '--local', '--json'], text=True, timeout=600))
    pending = updates(root, installed, outdated)
    repo = os.environ['GITHUB_REPOSITORY']
    # Caller identity is checked before any API write.
    if os.environ.get('GITHUB_REPOSITORY', repo) != repo:
        raise ValueError('repository differs from the workflow caller')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*/[a-z0-9-]+', repo):
        raise ValueError('invalid repository')
    title = 'Tool maintenance: pinned versions have updates'
    issues = json.loads(subprocess.check_output(['gh', 'issue', 'list', '--repo', repo,
        '--state', 'open', '--search', f'in:title "{title}"', '--limit', '100',
        '--json', 'number,title'], text=True, timeout=60))
    issues = [item for item in issues if item['title'] == title]
    if not pending:
        print('All repository tool pins are current.')
        # Do not automatically close a human's maintenance investigation.
        return
    inventory = root/'.artifacts/tool-updates.json'
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(json.dumps(pending, indent=2)+'\n')
    rows = ''
    shown = 0
    for name, old, new in pending:
        row = f'| `{name}` | `{old}` | `{new}` |\n'
        if len(rows) + len(row) > 10000:
            break
        rows += row
        shown += 1
    inventory_note = f'\nShowing {shown} of {len(pending)} updates. Full inventory: the `tool-updates` workflow artifact.\n'
    run = os.environ.get('GITHUB_RUN_ID', '')
    if run.isdigit():
        inventory_note += f'[Workflow run](https://github.com/{repo}/actions/runs/{run})\n'
    body = ('The scheduled tool watch found these upstream releases.\n\n'
            '| Tool | Pinned | Available |\n|---|---|---|\n' +
            rows + inventory_note +
            '\nUpdate `mise.toml` and every corresponding Go directive, container builder, '
            'download URL, and binary checksum together. Obtain checksums from the upstream '
            'release and verify the actual downloaded bytes. Keep the Bun packageManager '
            'field and lockfile in sync.\n\n'
            'Run the repository’s own verification command on the resulting PR and follow the caller repository’s review policy. '
            'This watcher does not authorize or auto-merge updates.\n')
    if len(body) > 15000:
        raise ValueError('too many tool updates for a bounded report')
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / 'body.md'
        file.write_text(body)
        command = (['gh', 'issue', 'edit', str(min(i['number'] for i in issues))] if issues
                   else ['gh', 'issue', 'create', '--title', title])
        # gh issue edit rejects --assignee: assignment there is the additive
        # --add-assignee (already-assigned stays assigned). gh issue create has
        # only --assignee, so the two paths cannot share one flag.
        assignee = '--add-assignee' if issues else '--assignee'
        assignment = [assignee, os.environ['MAINTENANCE_ASSIGNEE']] if os.environ.get('MAINTENANCE_ASSIGNEE') else []
        subprocess.run([*command, '--repo', repo, *assignment, '--body-file', str(file)],
                       check=True, timeout=60)


if __name__ == '__main__':
    main()
