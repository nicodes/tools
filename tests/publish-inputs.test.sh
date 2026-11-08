#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I -B helpers/release.py --help >/dev/null
if rg -n 'docker/login|docker.sock|password-stdin|components|image-base' publish; then
  echo 'obsolete publication transport remains' >&2
  exit 1
fi
python3 - <<'PY'
from pathlib import Path
import yaml
action = yaml.safe_load(Path('publish/action.yml').read_text())
step = action['runs']['steps'][0]
assert step['env']['KOMIZO_REGISTRY_TOKEN'] == '${{ inputs.registry-token }}'
assert 'helpers/release.py' in step['run'] and '--test-evidence-directory' in step['run']
assert set(action['inputs']) == {'project','revision','directory','test-evidence-directory','registry-user','registry-token'}
PY
