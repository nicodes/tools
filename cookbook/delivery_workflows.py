"""Compose thin Komizo delivery callers after the same-run Make acceptance."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from configuration import relative


def declaration(root, config):
    value = config.get('delivery')
    if not isinstance(value, dict) or set(value) != {'definition'}:
        raise ValueError('delivery requires its explicit native declaration')
    path = relative(Path(root), value['definition'], exists=True)
    if path.stat().st_size > 32768:
        raise ValueError('delivery declaration exceeds its bound')
    data = json.loads(path.read_text())
    keys = {'version', 'project', 'komizo_ref', 'publish_runner', 'publish_timeout',
            'deploy_runner', 'deploy_timeout', 'health_urls', 'secrets', 'variables',
            'backup', 'upload_backup', 'readiness', 'diagnostics', 'notification_url'}
    if not isinstance(data, dict) or set(data) - keys or data.get('version') != 1:
        raise ValueError('unsupported delivery declaration')
    if not re.fullmatch('[a-z][a-z0-9-]{0,62}', data.get('project', '')):
        raise ValueError('invalid delivery project')
    if not re.fullmatch('[a-f0-9]{40}', data.get('komizo_ref', '')):
        raise ValueError('Komizo delivery must use an exact accepted source pin')
    for key in ['publish_runner', 'deploy_runner']:
        if not re.fullmatch('[A-Za-z0-9_.-]{1,80}', data.get(key, '')):
            raise ValueError('delivery requires its existing runner labels')
    for key in ['publish_timeout', 'deploy_timeout']:
        if type(data.get(key)) is not int or not 1 <= data[key] <= 180:
            raise ValueError('delivery requires bounded existing job budgets')
    urls = data.get('health_urls')
    if not isinstance(urls, list) or not 1 <= len(urls) <= 20:
        raise ValueError('delivery requires explicit health URLs')
    for url in [*urls, data.get('notification_url', '')]:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 33 for c in url):
            raise ValueError('delivery URLs must be explicit public HTTPS URLs')
    for key in ['secrets', 'variables']:
        mappings = data.get(key, {})
        if not isinstance(mappings, dict) or any(not re.fullmatch('KOMIZO_SECRET_[A-Z][A-Z0-9_]*', name)
                or not isinstance(source, str) or not re.fullmatch('[A-Z][A-Z0-9_]*', source)
                for name, source in mappings.items()):
            raise ValueError('delivery maps explicit host secrets to named caller inputs')
    if set(data.get('secrets', {})) & set(data.get('variables', {})):
        raise ValueError('host inputs cannot have two secret authorities')
    for key in ['backup', 'readiness']:
        if data.get(key):
            relative(Path(root), data[key], exists=True)
    for key in ['upload_backup', 'diagnostics']:
        if type(data.get(key, False)) is not bool:
            raise ValueError('delivery options must be literal booleans')
    return data


def jobs(root, config, tools):
    data = declaration(root, config)
    ref = data['komizo_ref']
    # Token-created merges explicitly dispatch exact final-source CI. Both main
    # entrypoints may deliver; a stale dispatched source never reaches host jobs.
    condition = "github.ref == 'refs/heads/main' && github.event_name != 'pull_request' && (inputs.source == '' || inputs.source == github.sha)"
    quote = lambda value: json.dumps(value)
    lines = ['  publish:', '    name: Publish accepted images', '    needs: validate',
             '    if: ' + condition,
             '    uses: nicodes/komizo-actions/.github/workflows/cookbook-publish.yml@' + ref,
             '    permissions:', '      contents: read', '      actions: read', '      packages: write',
             '      attestations: read', '    with:', '      tools-ref: ' + tools, '      komizo-ref: ' + ref,
             '      source: ${{ github.sha }}', '      project: ' + data['project'],
             '      runner: ' + data['publish_runner'], '      timeout-minutes: ' + str(data['publish_timeout']),
             '  deploy:', '    name: Deploy accepted images', '    needs: publish', '    if: ' + condition,
             '    runs-on: ' + data['deploy_runner'], '    timeout-minutes: ' + str(data['deploy_timeout']),
             '    environment: production', '    concurrency:', '      group: deploy-production',
             '      cancel-in-progress: false', '    permissions:', '      contents: read',
             '      actions: read', '      packages: read', '      attestations: read', '      id-token: write',
             '    steps:', '      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
             '        with:', '          persist-credentials: false', '          fetch-depth: 0',
             '      - uses: nicodes/komizo-actions/application-delivery@' + ref,
             '        with:', '          tools-ref: ' + tools,
             '          project: ' + data['project'], '          declaration: ' + config['delivery']['definition'],
             '        env:', '          GH_TOKEN: ${{ github.token }}',
             '          KOMIZO_APP_NAME: ${{ vars.KOMIZO_APP_NAME }}',
             '          KOMIZO_SERVER_URL: ${{ vars.KOMIZO_SERVER_URL }}',
             '          KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}',
             '          KOMIZO_KNOWN_HOSTS: ${{ vars.KOMIZO_KNOWN_HOSTS }}',
             '          KOMIZO_REGISTRY_TOKEN: ${{ github.token }}']
    for key, context in [('secrets', 'secrets'), ('variables', 'vars')]:
        for name, source in sorted(data.get(key, {}).items()):
            lines.append('          ' + name + ': ${{ ' + context + '.' + source + ' }}')
    if data.get('upload_backup'):
        for key in ['ACCESS_KEY', 'SECRET_KEY']:
            lines.append('          BACKUP_S3_' + key + ': ${{ secrets.BACKUP_S3_' + key + ' }}')
        for key in ['HOSTNAME', 'BUCKET']:
            lines.append('          BACKUP_S3_' + key + ': ${{ vars.BACKUP_S3_' + key + ' }}')
    lines += ['  report:', '    needs: [validate, publish, deploy]', '    if: always() && !cancelled() && (' + condition + ')',
              '    uses: nicodes/komizo-actions/.github/workflows/cookbook-report.yml@' + ref,
              '    permissions:', '      contents: read', '      issues: write', '    with:',
              '      tools-ref: ' + tools, '      komizo-ref: ' + ref, '      validation: ${{ needs.validate.result }}',
              '      publication: ${{ needs.publish.result }}', '      deployment: ${{ needs.deploy.result }}',
              '      deployment-url: ' + quote(data['notification_url']), '    secrets:',
              '      DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}']
    return '\n'.join(lines) + '\n'
