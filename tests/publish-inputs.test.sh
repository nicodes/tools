#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I -B helpers/release.py --help >/dev/null
python3 - <<'CHECK'
from pathlib import Path
import re
text = Path('publish/action.yml').read_text()
assert not re.search(r'docker/login|docker.sock|password-stdin|components|image-base', text), 'obsolete publication transport remains'
# This action is deliberately a fixed one-step composite. actionlint separately
# parses the complete YAML; this control checks its public and secret boundary.
inputs = text.split('inputs:\n', 1)[1].split('runs:\n', 1)[0]
assert set(re.findall(r'^  ([a-z][a-z-]*):$', inputs, re.M)) == {'project','revision','directory','test-evidence-directory','registry-user','registry-token'}
assert len(re.findall(r'^    - name:', text, re.M)) == 1
assert '        KOMIZO_REGISTRY_TOKEN: ${{ inputs.registry-token }}\n' in text
run = re.findall(r'^      run: (.+)$', text, re.M)
assert len(run) == 1 and 'helpers/release.py' in run[0] and '--test-evidence-directory' in run[0]
CHECK
