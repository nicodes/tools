"""Content-address the Godot shell's payload; keep only this image's release."""

import hashlib
import json
from pathlib import Path
import re
import shutil

RECOVERY = '''<script id="godot-release-recovery">
window.godotLoadFailure = function () {
  if (document.getElementById('release-reload')) return;
  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;inset:30% 10% auto;padding:2rem;background:#201d2b;color:white;z-index:99;text-align:center';
  const message = document.createElement('p');
  message.textContent = 'The game could not finish loading. A release may have changed or the connection was interrupted. Reload to try the current release.';
  const button = document.createElement('button');
  button.id = 'release-reload';
  button.textContent = 'Reload game';
  button.onclick = function () { window.location.reload(); };
  panel.append(message, button);
  document.body.appendChild(panel);
};
</script>'''


def stage_version(root, files, relative_urls=False):
    root = Path(root)
    payload = {name: info['sha256'] for name, info in files.items() if name != 'index.html'}
    release_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    prefix = f'{"" if relative_urls else "/"}releases/{release_id}/'
    html = (root / 'index.html').read_text()
    html = re.sub(r'/?releases/[0-9a-f]{64}/', '', html)
    match = re.search(r'const GODOT_CONFIG = (\{[^\n]+\});', html)
    if not match:
        raise ValueError('Expected the reviewed Godot shell configuration')
    config = json.loads(match[1])
    config['executable'] = prefix + 'index'
    config['fileSizes'] = {prefix + Path(name).name: size for name, size in config['fileSizes'].items()}
    html = html[:match.start(1)] + json.dumps(config, separators=(',', ':')) + html[match.end(1):]
    for name in payload:
        html = html.replace(f'src="{name}"', f'src="{prefix}{name}"')
        html = html.replace(f'href="{name}"', f'href="{prefix}{name}"')
    if 'id="godot-release-recovery"' not in html:
        html = html.replace('<script src=', RECOVERY + '\n<script src=', 1)
        html = html.replace(f'src="{prefix}index.js"', f'src="{prefix}index.js" onerror="godotLoadFailure()"')
        html = html.replace('const engine = new Engine(GODOT_CONFIG);',
                            "const engine = typeof Engine === 'function' ? new Engine(GODOT_CONFIG) : null;")
        html = html.replace('(function () {', '(function () {\n\tif (!engine) { godotLoadFailure(); return; }', 1)
        html = html.replace('function displayFailureNotice(err) {',
                            'function displayFailureNotice(err) {\n\t\tgodotLoadFailure();', 1)
    (root / 'index.html').write_text(html)
    releases = root / 'releases'
    if releases.is_symlink():
        raise ValueError('Release output cannot be a symlink')
    releases.mkdir(exist_ok=True)
    destination = releases / release_id
    if destination.is_symlink():
        raise ValueError('Version output cannot be a symlink')
    destination.mkdir(exist_ok=True)
    for name in payload:
        shutil.copy2(root / name, destination / name)
        for suffix in ('.gz', '.br'):
            sidecar = root / (name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, destination / sidecar.name)
    # This is generated output, not a historical artifact store. Old shells
    # fail explicitly and offer reload instead of fetching a different pack.
    for entry in releases.iterdir():
        if entry.name != release_id and re.fullmatch(r'[0-9a-f]{64}', entry.name):
            if entry.is_symlink():
                raise ValueError('Historical release output cannot be a symlink')
            shutil.rmtree(entry)
    return release_id
