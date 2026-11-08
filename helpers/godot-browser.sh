#!/usr/bin/env bash
set -euo pipefail
unset CONTAINER_HOST CONTAINER_CONNECTION CONTAINER_SSHKEY DOCKER_HOST DOCKER_CONTEXT
: "${GATE_IMAGE:?Set GATE_IMAGE to the already-built release image}"
if [[ -z ${BROWSER_TEST_MODE:-} && ( ${BROWSER_ROUTE_CYCLES:-0} != 0 || ${BROWSER_SOAK_SECONDS:-0} != 0 ) ]]; then
  echo "Retention workload requires explicit BROWSER_TEST_MODE" >&2
  exit 1
fi
if [[ ${BROWSER_TEST_MODE:-canonical} == retention ]]; then
  : "${EXPECTED_SOURCE_COMMIT:?Focused retention requires EXPECTED_SOURCE_COMMIT}"
  : "${EXPECTED_IMAGE_ID:?Focused retention requires EXPECTED_IMAGE_ID}"
  actual_image_id=$(podman --remote=false image inspect "$GATE_IMAGE" --format '{{.Id}}')
  actual_image_id="sha256:${actual_image_id#sha256:}"
  [[ "$actual_image_id" == "$EXPECTED_IMAGE_ID" ]] || {
    echo "Focused retention image identity mismatch" >&2
    exit 1
  }
fi
mkdir -p "$GODOT_BROWSER_REPORTS"
tls_dir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/godot-browser-tls.XXXXXX")
proxy_pid=''
cache_gate_id=''
switch_file=''
gate_id=$(podman --remote=false run --pull=never --detach --read-only --user 1000:1000 --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --sysctl net.ipv4.ip_unprivileged_port_start=0 \
  --publish 127.0.0.1::80 "$GATE_IMAGE")
# shellcheck disable=SC2329
cleanup() {
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  rm -f "$tls_dir/key.pem" "$tls_dir/cert.pem" "$tls_dir/port"
  rmdir "$tls_dir"
  podman --remote=false logs "$gate_id" > "$GODOT_BROWSER_REPORTS/gate.log" 2>&1
  podman --remote=false rm --force "$gate_id" >/dev/null
  if [[ -n "$cache_gate_id" ]]; then podman --remote=false rm --force "$cache_gate_id" >/dev/null; fi
  if [[ -n "$switch_file" ]]; then rm -f "$switch_file"; fi
}
trap cleanup EXIT
address=$(podman --remote=false port "$gate_id" 80/tcp)
if [[ -n ${GATE_CACHE_IMAGE:-} ]]; then
  cache_gate_id=$(podman --remote=false run --pull=never --detach --read-only --user 1000:1000 --cap-drop ALL \
    --security-opt no-new-privileges:true --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --sysctl net.ipv4.ip_unprivileged_port_start=0 --publish 127.0.0.1::80 "$GATE_CACHE_IMAGE")
  cache_address=$(podman --remote=false port "$cache_gate_id" 80/tcp)
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
      if [[ -n "$cache_gate_id" ]]; then browser_command=(node "$GODOT_BROWSER_CACHE_RUNNER"); fi
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
        podman --remote=false run --pull=never --rm --init "${user_namespace[@]}" --network host --shm-size=1g --user "$(id -u):$(id -g)" \
          -e HOME=/tmp -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
          -e GATE_TLS_PORT -e GATE_TLS_SPKI -e BROWSER -e BROWSER_SOAK_SECONDS -e BROWSER_ROUTE_CYCLES \
          -e BROWSER_TEST_MODE -e EXPECTED_SOURCE_COMMIT -e EXPECTED_RELEASE_ID -e EXPECTED_IMAGE_ID \
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
