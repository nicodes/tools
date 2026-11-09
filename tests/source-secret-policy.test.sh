#!/usr/bin/env bash
# shellcheck disable=SC2016
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
root="$(pwd)"
parser="${SOURCE_POLICY_PYTHON:?Prepare source policy Python during setup}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
pass=0 fail=0
fixture() {
  rm -rf "${tmp:?}/app"
  mkdir -p "$tmp/app/deploy" "$tmp/app/.github/workflows"
  printf '%s' "$1" > "$tmp/app/deploy/application.toml"
  printf '%s' "$2" > "$tmp/app/.github/workflows/cd.yml"
  if [[ $# -ge 3 ]]; then printf '%s' "$3" > "$tmp/app/host-keys"; fi
}
check() {
  local want="$1" label="$2" needle="$3" out rc
  shift 3
  out=$(cd "$tmp/app" && "$parser" -I "$root/helpers/source-secret-policy.py" --application deploy/application.toml --workflow .github/workflows/cd.yml "$@" 2>&1)
  rc=$?
  if [[ "$rc" != "$want" ]] || { [[ -n "$needle" ]] && ! printf '%s' "$out" | grep -qF "$needle"; }; then
    printf 'FAIL %s: expected rc %s containing %s, got %s\n%s\n' "$label" "$want" "$needle" "$rc" "$out"
    fail=$((fail+1))
  else printf 'ok %s\n' "$label"; pass=$((pass+1)); fi
}
GOOD='[services.api]
environment_files = ["runtime"]
[services.postgres]
environment_files = ["postgres-owner"]
[jobs.migrate]
environment_files = ["migration"]
'
WORKFLOW='jobs:
  deploy:
    steps:
      - uses: example/actions/deploy@v1
        env:
          KOMIZO_SECRET_API_KEY: ${{ secrets.API_KEY }}
'
readers=(--host-only 'postgres-owner: postgres' --host-only 'migration: migrate' --secrets-env-services api)
fixture "$GOOD" "$WORKFLOW"
check 0 'logical readers exactly match declaration' 'secret rule: ok' "${readers[@]}"
fixture "$GOOD" 'jobs:
  deploy:
    steps:
      - env:
          KOMIZO_SECRET_API_KEY: sk_live_0123456789abcdef
'
check 1 'literal credential has no delivery provenance' 'must be a' "${readers[@]}"
fixture "$GOOD" 'jobs:
  deploy:
    steps:
      - env:
          KOMIZO_SECRET_PROXY: ${{ vars.PROXY_CIDR }}
'
check 0 'public repository variable is noted' 'its value is public' "${readers[@]}"
for bypass in 'ssh target "cat >> secrets.env"' 'ssh target set-secret-app KEY' 'scp prod.env target:/srv/app/'; do
  fixture "$GOOD" "jobs:
  deploy:
    steps:
      - run: $bypass
"
  check 1 'direct host credential write has no provenance' 'no provenance' "${readers[@]}"
done
fixture '[services.api]
environment_files=["runtime"]
[services.gate]
environment_files=["runtime"]
' "$WORKFLOW"
check 1 'gate cannot inherit API credential' 'undeclared credential' --secrets-env-services api
check 0 'explicit shared runtime reader is reviewed' 'secret rule: ok' --secrets-env-services api,gate
fixture '[services.api]
environment_files=["runtime"]
' "$WORKFLOW"
check 1 'undeclared logical ID is refused' 'undeclared credential'
check 1 'missing declared reader is refused' 'receive no credential' --secrets-env-services api,gate
fixture '[services.api]
environment_files=["../secrets/runtime"]
' "$WORKFLOW"
check 1 'host paths and traversal are not logical credentials' 'logical credential IDs' --secrets-env-services api
fixture '[services.api]
environment_files="runtime"
' "$WORKFLOW"
check 1 'non-list credential forms fail closed' 'logical credential IDs'
fixture '[services.api]
environment_files=["runtime","runtime"]
' "$WORKFLOW"
check 1 'duplicate logical credentials fail closed' 'logical credential IDs'
fixture '[services.api.environment]
DATABASE_URL="postgres://app:hunter2hunter2@db:5432/app"
' "$WORKFLOW"
check 1 'committed DSN passwords are rejected' 'with a password in it'
fixture '[services.api.environment]
DATABASE_URL="postgres://app:${DB_PASSWORD}@db:5432/app"
PUBLIC_KEY="pk_live_abcdefghijk"
' "$WORKFLOW"
check 0 'interpolation and public client keys are allowed' 'secret rule: ok'
fixture "$GOOD" "$WORKFLOW" 'API_KEY
'
check 0 'host keys exactly match delivery' 'secret rule: ok' "${readers[@]}" --host-keys host-keys
fixture "$GOOD" "$WORKFLOW" 'API_KEY
STALE_KEY
'
check 1 'stale host key is reported' 'nothing rotates it' "${readers[@]}" --host-keys host-keys
fixture "$GOOD" "$WORKFLOW" ''
check 0 'new delivery key is a note before deployment' 'is not on the host yet' "${readers[@]}" --host-keys host-keys
printf '%s passed, %s failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
