#!/usr/bin/env python3
"""Watch exact repository tool pins and keep one owned, actionable update issue."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib


def updates(root, installed, outdated):
    config = (root / '.mise.toml').resolve()
    result = []
    for name, item in sorted(outdated.items()):
        # mise can return inherited tools even with --local. Never update them.
        if Path(item['source']['path']).resolve() != config:
            continue
        entry = installed[name]
        latest = item.get('bump') or item.get('latest')
        if isinstance(entry, dict):
            # Table form (e.g. the mise github: backend pins in example-games/sample-game's
            # .mise.toml): the pin is the entry's version field, decorated the way
            # upstream tags it — `v0.0.8`, `cli-v0.0.5`, `4.4.1-stable`.
            current = entry.get('version')
            if not isinstance(current, str):
                raise ValueError(f'{name}: expected an exact repository version')
            current_core = decorated_version(name, current, 'repository')
            latest_core = decorated_version(name, latest, 'upstream')
        else:
            current = entry
            if not isinstance(current, str):
                raise ValueError(f'{name}: expected an exact repository version')
            # A two-component core (python = "3.12") is a valid mise pin that
            # floats the patch; normalize it so it compares as 3.12.0.
            match = re.fullmatch(r'(\d+\.\d+)(?:\.(\d+))?', current)
            if not match:
                raise ValueError(f'{name}: expected an exact repository version')
            current_core = match.group(1) + '.' + (match.group(2) or '0')
            if not isinstance(latest, str) or not re.fullmatch(r'\d+\.\d+\.\d+', latest):
                if match.group(2) is None:
                    # A floating pin's upstream answer depends on the mise
                    # environment: a fresh mise-action install can answer a
                    # non-x.y.z latest/bump for python = "3.12" where a warm
                    # workstation answers 3.12.14 (fleet: sample-game tools
                    # dispatch 35552447413). Undetermined is neither drift nor
                    # an error — skip the tool, visibly, in the step log.
                    print(f'{name}: upstream latest {latest!r} is not an x.y.z '
                          f'version; floating pin {current} cannot be compared '
                          '(skipping as undetermined)')
                    continue
                # Exact pins compare strictly; an unreadable upstream shape
                # fails loudly, naming the offending value and pin.
                raise ValueError(f'{name}: unrecognized upstream version '
                                 f'{latest!r} for exact pin {current}')
            latest_core = latest
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


def caddy_updates(root):
    # Caddy's flat helper lock is maintained centrally. It is not a fake Go
    # backend in app-only products and Dependabot cannot find this filename.
    source = root/'helpers'
    if not (source/'caddy.go.mod').is_file():
        return []
    with tempfile.TemporaryDirectory(prefix='caddy-update-watch-') as directory:
        for extension in ['mod', 'sum']:
            shutil.copyfile(source/f'caddy.go.{extension}', Path(directory)/f'go.{extension}')
        output = subprocess.check_output(['go', 'list', '-mod=readonly', '-m', '-u', '-json', 'all'],
            cwd=directory, text=True, timeout=600, env={**os.environ, 'GOTOOLCHAIN': 'local'})
    decoder = json.JSONDecoder()
    pending = []
    count = 0
    while output.strip():
        item, end = decoder.raw_decode(output.lstrip())
        output = output.lstrip()[end:]
        count += 1
        if not isinstance(item, dict) or 'Error' in item or not isinstance(item.get('Path'), str):
            raise ValueError('Caddy module update lookup failed or changed schema')
        if 'Update' not in item:
            continue
        update = item['Update']
        if not isinstance(update, dict) or update.get('Path') != item['Path'] or 'Error' in update:
            raise ValueError('Caddy module update is ambiguous')
        for version in [item.get('Version'), update.get('Version')]:
            if not isinstance(version, str) or not re.fullmatch(r'v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]+)?', version):
                raise ValueError('Caddy module update has an unknown version')
        pending.append(('Caddy: '+item['Path'], item['Version'], update['Version']))
    if count < 2:
        raise ValueError('Caddy module update lookup returned no dependency inventory')
    return pending


def main():
    root = Path.cwd()
    installed = tomllib.loads((root / '.mise.toml').read_text())['tools']
    outdated = json.loads(subprocess.check_output(
        ['mise', 'outdated', '--bump', '--local', '--json'], text=True, timeout=600))
    pending = updates(root, installed, outdated) + caddy_updates(root)
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
            '\nUpdate `.mise.toml` and every corresponding Go directive, Docker builder, '
            'download URL, and binary checksum together. Obtain checksums from the upstream '
            'release and verify the actual downloaded bytes. Keep the Bun packageManager '
            'field and lockfile in sync.\n\n'
            'Caddy module updates belong in the central `helpers/caddy.go.mod` and '
            '`caddy.go.sum` lock. Review the matcher compatibility patch and run the '
            'upstream race tests and actual binary scan, then vendor the committed '
            'helper revision into consuming repositories.\n\n'
            'Run **make check** on the resulting PR and follow the caller repository’s review policy. '
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
