#!/usr/bin/env bash
set -euo pipefail
unset CONTAINER_HOST CONTAINER_CONNECTION CONTAINER_SSHKEY DOCKER_HOST DOCKER_CONTEXT
loopback_address() {
  if [[ "$1" =~ ^http://127\.0\.0\.1:([1-9][0-9]{0,4})/?$ ]]; then
    if (( BASH_REMATCH[1] <= 65535 )); then
      local value="${1%/}"
      printf '%s\n' "${value#http://}"
      return 0
    fi
  fi
  echo 'Browser fixture URL must be a bounded loopback HTTP listener' >&2
  return 1
}
: "${GATE_URL:?Set GATE_URL to the owned native fixture}"
if [[ -z ${BROWSER_TEST_MODE:-} && ( ${BROWSER_ROUTE_CYCLES:-0} != 0 || ${BROWSER_SOAK_SECONDS:-0} != 0 ) ]]; then
  echo "Retention workload requires explicit BROWSER_TEST_MODE" >&2
  exit 1
fi
if [[ ${BROWSER_TEST_MODE:-canonical} == retention ]]; then
  : "${EXPECTED_SOURCE_COMMIT:?Focused retention requires EXPECTED_SOURCE_COMMIT}"
  : "${GATE_URL:?Retention requires a native fixture URL}"
  : "${GATE_DESCRIPTOR_FILE:?Retention requires the exact fixture descriptor file}"
  : "${EXPECTED_DESCRIPTOR_DIGEST:?Retention requires EXPECTED_DESCRIPTOR_DIGEST}"
  python3 -I -B - "$GATE_DESCRIPTOR_FILE" "$EXPECTED_DESCRIPTOR_DIGEST" "$EXPECTED_SOURCE_COMMIT" <<'PYTHON'
import hashlib,json,os,re,stat,sys
from pathlib import Path
path=Path(sys.argv[1]).absolute()
if '..' in path.parts or any(parent.is_symlink() for parent in [path,*path.parents]):
    raise SystemExit('Fixture descriptor must not traverse links')
def fingerprint(info):return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
info=path.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=4<<20:
    raise SystemExit('Fixture descriptor must be one bounded ordinary file')
with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
    before=os.fstat(stream.fileno()); raw=stream.read((4<<20)+1); after=os.fstat(stream.fileno())
if fingerprint(info)!=fingerprint(before) or fingerprint(before)!=fingerprint(after) or fingerprint(path.lstat())!=fingerprint(after) or len(raw)!=info.st_size:
    raise SystemExit('Fixture descriptor changed during admission')
if not re.fullmatch('sha256:[a-f0-9]{64}',sys.argv[2]) or 'sha256:'+hashlib.sha256(raw).hexdigest()!=sys.argv[2]:
    raise SystemExit('Focused retention descriptor identity mismatch')
value=json.loads(raw)
if not re.fullmatch('[a-f0-9]{40}',sys.argv[3]) or value.get('source',{}).get('revision')!=sys.argv[3]:
    raise SystemExit('Focused retention descriptor source mismatch')
PYTHON
fi
mkdir -p "$GODOT_BROWSER_REPORTS"
tls_dir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/godot-browser-tls.XXXXXX")
proxy_pid=''
switch_file=''
cache_address=''
# shellcheck disable=SC2329
cleanup() {
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  rm -f "$tls_dir/key.pem" "$tls_dir/cert.pem" "$tls_dir/port" "$tls_dir/registry-auth.json"
  rmdir "$tls_dir" 2>/dev/null || true
  if [[ -n "$switch_file" ]]; then rm -f "$switch_file"; fi
}
trap cleanup EXIT
printf '{"auths":{}}\n' > "$tls_dir/registry-auth.json"
chmod 600 "$tls_dir/registry-auth.json"
address=$(loopback_address "$GATE_URL")
if [[ -n ${GATE_CACHE_URL:-} ]]; then
  cache_address=$(loopback_address "$GATE_CACHE_URL")
fi
if [[ -n "$cache_address" ]]; then
  export CACHE_NEXT_PORT="${cache_address##*:}" CACHE_FIRST_PORT="${address##*:}"
  switch_file="$PWD/${GODOT_BROWSER_REPORTS}/upstream-$$"
  export CACHE_SWITCH_FILE="$switch_file"
fi
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$tls_dir/key.pem" \
  -out "$tls_dir/cert.pem" -days 1 -subj '/CN=localhost' \
  -addext 'subjectAltName=IP:127.0.0.1' >/dev/null 2>&1
GATE_TLS_SPKI=$(openssl x509 -in "$tls_dir/cert.pem" -pubkey -noout | \
  openssl pkey -pubin -outform DER | openssl dgst -sha256 -binary | openssl base64 -A)
export GATE_TLS_SPKI
node "$GODOT_BROWSER_TLS_PROXY" "$tls_dir/key.pem" "$tls_dir/cert.pem" \
  "$tls_dir/port" "${address##*:}" "$switch_file" &
proxy_pid=$!
for _attempt in {1..30}; do
  if [[ -s "$tls_dir/port" ]] && curl --silent --fail -H "Host: $GODOT_BROWSER_HOSTNAME" "http://$address/" >/dev/null; then
    GATE_TLS_PORT=$(< "$tls_dir/port")
    export GATE_TLS_PORT
    read -r -a browser_matrix <<< "${BROWSER:-chromium firefox webkit}"
    for browser_name in "${browser_matrix[@]}"; do
      export BROWSER="$browser_name"
      echo "Verifying release in $BROWSER"
      browser_command=(node "$GODOT_BROWSER_RUNNER")
      if [[ -n "$cache_address" ]]; then browser_command=(node "$GODOT_BROWSER_CACHE_RUNNER"); fi
      if [[ ${BROWSER_STARTUP:-0} == 1 ]]; then browser_command=(node "$GODOT_BROWSER_STARTUP_RUNNER"); fi
      if [[ "$BROWSER" == firefox || ( "$BROWSER" == chromium && ${BROWSER_STARTUP:-0} != 1 && ${BROWSER_TEST_MODE:-canonical} != retention ) ]]; then
        browser_command=(xvfb-run -a "${browser_command[@]}")
      fi
      if [[ -n ${BROWSER_RUNTIME_IMAGE:-} ]]; then
        : "${BROWSER_CACHE:?Set BROWSER_CACHE to the installed Playwright browser directory}"
        : "${GDPW_BIN:?Set GDPW_BIN to the verified gd-playwright-cli executable}"
        native_environment=()
        read -r -a forwarded_parameters <<< "${GODOT_BROWSER_FORWARD_ENVIRONMENT:-}"
        for parameter in "${forwarded_parameters[@]}"; do
          native_environment+=(-e "$parameter")
        done
        user_namespace=()
        if [[ $(id -u) != 0 ]]; then user_namespace=(--userns=keep-id); fi
        podman --remote=false run --authfile "$tls_dir/registry-auth.json" --pull=never --rm --init "${user_namespace[@]}" --network host --shm-size=1g --user "$(id -u):$(id -g)" \
          -e HOME=/tmp -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
          -e GATE_TLS_PORT -e GATE_TLS_SPKI -e BROWSER -e BROWSER_SOAK_SECONDS -e BROWSER_ROUTE_CYCLES \
          -e BROWSER_TEST_MODE -e EXPECTED_SOURCE_COMMIT -e EXPECTED_RELEASE_ID -e EXPECTED_DESCRIPTOR_DIGEST -e GATE_DESCRIPTOR_FILE \
          -e CACHE_NEXT_PORT -e CACHE_FIRST_PORT -e CACHE_SWITCH_FILE "${native_environment[@]}" \
          -v "$PWD:$PWD" -w "$PWD" -v "$BROWSER_CACHE:/ms-playwright:ro" \
          -v "$GDPW_BIN:/usr/local/bin/gd-playwright-cli:ro" "$BROWSER_RUNTIME_IMAGE" "${browser_command[@]}"
      else
        "${browser_command[@]}"
      fi
    done
    exit 0
  fi
  sleep 1
done
echo 'Browser-test gate did not become healthy' >&2
exit 1
