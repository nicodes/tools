#!/usr/bin/env bash
set -euo pipefail
# Both supported runner pools are disposable. Never restart a self-hosted daemon.
[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo 'Registry setup requires CI' >&2; exit 1; }
if [[ "${RUNNER_ENVIRONMENT:-}" != github-hosted && -z "${BLACKSMITH_RUNNER_ACQUIRE_JOB_MS:-}" ]]; then
  echo 'Registry setup requires a supported disposable runner' >&2
  exit 1
fi
# Hosted images carry Docker Hub login state. Do not forward that credential
# to a public mirror. Later registry logins use this job-owned directory.
docker_config=$(mktemp -d "${RUNNER_TEMP:?}/fleet-docker-config.XXXXXXXX")
printf '{}\n' > "$docker_config/config.json"
export DOCKER_CONFIG="$docker_config"
printf 'DOCKER_CONFIG=%s\n' "$DOCKER_CONFIG" >> "${GITHUB_ENV:?}"
# Blacksmith owns its daemon configuration; the selected BuildKit builder below
# supplies the public mirror without changing that daemon.
if [[ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then exit 0; fi
test -z "$(docker ps -q)" || { echo 'Configure registry cache before starting containers' >&2; exit 1; }
config=$(mktemp "${RUNNER_TEMP:?}/docker-mirror.XXXXXXXX.json")
trap 'rm -f "$config"' EXIT
sudo python3 - "$config" <<'PY'
import json
from pathlib import Path
import sys
source = Path('/etc/docker/daemon.json')
current = json.loads(source.read_text()) if source.exists() else {}
mirrors = current.get('registry-mirrors', [])
if not isinstance(mirrors, list) or any(not isinstance(value, str) for value in mirrors):
    raise ValueError('existing Docker mirrors are invalid')
current['registry-mirrors'] = list(dict.fromkeys(['https://mirror.gcr.io', *mirrors]))
Path(sys.argv[1]).write_text(json.dumps(current) + '\n')
PY
sudo install -m 0644 "$config" /etc/docker/daemon.json
sudo systemctl restart docker
docker info --format '{{json .RegistryConfig.Mirrors}}'
