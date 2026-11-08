#!/usr/bin/env bash
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true ]] || { echo 'Container setup requires CI' >&2; exit 1; }
if [[ "${RUNNER_ENVIRONMENT:-}" != github-hosted && -z "${BLACKSMITH_RUNNER_ACQUIRE_JOB_MS:-}" ]]; then
  echo 'Container setup requires a supported disposable runner' >&2
  exit 1
fi
if ! command -v podman >/dev/null 2>&1; then
  [[ "${RUNNER_OS:-Linux}" == Linux ]] || { echo 'Prepare a native Podman machine before container setup' >&2; exit 1; }
  sudo apt-get update -qq
  sudo apt-get install --yes --no-install-recommends podman crun uidmap slirp4netns fuse-overlayfs
fi
runtime_root=$(mktemp -d "${RUNNER_TEMP:?}/tools-podman.XXXXXXXX")
chmod 700 "$runtime_root"
printf '{"auths":{}}\n' > "$runtime_root/auth.json"
chmod 600 "$runtime_root/auth.json"
cat > "$runtime_root/registries.conf" <<'EOF'
unqualified-search-registries = []
credential-helpers = ["containers-auth.json"]

[[registry]]
prefix = "docker.io"
location = "docker.io"
mirror-by-digest-only = true
[[registry.mirror]]
location = "mirror.gcr.io"
EOF
export REGISTRY_AUTH_FILE="$runtime_root/auth.json"
export CONTAINERS_REGISTRIES_CONF="$runtime_root/registries.conf"
unset CONTAINER_HOST CONTAINER_CONNECTION CONTAINER_SSHKEY DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH BUILDKIT_HOST BUILDX_BUILDER
printf 'REGISTRY_AUTH_FILE=%s\n' "$REGISTRY_AUTH_FILE" >> "${GITHUB_ENV:?}"
printf 'CONTAINERS_REGISTRIES_CONF=%s\n' "$CONTAINERS_REGISTRIES_CONF" >> "$GITHUB_ENV"
podman --remote=false info --format json > "$runtime_root/runtime.json"
python3 - "$runtime_root/runtime.json" <<'PY'
import json,sys,subprocess
from pathlib import Path
data=json.loads(Path(sys.argv[1]).read_text())
version=data.get('version',{}).get('Version','')
if not version or tuple(int(part) for part in version.split('.')[:2])<(4,9):
    raise SystemExit('Native Podman 4.9 or newer is required')
if data.get('host',{}).get('os')!='linux':
    raise SystemExit('Container execution requires a Linux runtime')
if data.get('host',{}).get('cgroupVersion') != 'v2':
    raise SystemExit('Native container verification requires cgroup v2')
for command, flags in {'build':['--format','--layers','--pull'], 'save':['--format','oci-archive'],
                       'load':['--input'], 'image inspect':['--format']}.items():
    help_text=subprocess.check_output(['podman','--remote=false',*command.split(),'--help'],text=True,timeout=15)
    if any(flag not in help_text for flag in flags):
        raise SystemExit('Native Podman lacks the required OCI build/archive API')
print('Native Podman '+version+'; local OCI build/archive APIs; cgroup v2')
PY
