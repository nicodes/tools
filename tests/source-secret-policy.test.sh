#!/usr/bin/env bash
# tests/check-secrets.test.sh - drive check-secrets/check.py over the shapes
# the rule allows and the ones it must refuse.
#
# WHY THIS EXISTS. The checker's whole value is that it says no, and a checker
# that says yes to everything is indistinguishable from a passing build. Each
# refusal below is a state some app in this portfolio was actually found in:
# six unreferenced credentials sitting in one host's secrets.env, a gate
# container reading the same secret file as its API, an env_file pointing at a
# path nobody could account for.
#
# The accept cases matter as much. A check that fires on a correct app gets an
# ignore comment within the week, so the two apps that already follow the rule
# exactly -- one delivering six secrets, one delivering two -- are pinned here
# as passing.
#
# Run: bash tests/check-secrets.test.sh

# SC2016 off for the whole file: the fixtures contain ${{ secrets.X }}, which
# is a GitHub expression and must reach the checker as those exact characters.
# Single quotes are the point, not an oversight.
# shellcheck disable=SC2016

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
root="$(pwd)"

parser="${SOURCE_POLICY_PYTHON:?Prepare the reviewed source-policy SDK during mise run setup}"
C=helpers/source-secret-policy.py
pass=0
fail=0
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# fixture <compose-body> <workflow-body> [host-keys-body]
#
# Written fresh for every case: a file left over from an earlier row is how a
# test suite starts passing for the wrong reason.
fixture() {
	rm -rf "${tmp:?}/app"
	mkdir -p "$tmp/app/deploy" "$tmp/app/.github/workflows"
	printf '%s' "$1" > "$tmp/app/deploy/compose.yml"
	printf '%s' "$2" > "$tmp/app/.github/workflows/cd.yml"
	if [ $# -ge 3 ]; then
		printf '%s' "$3" > "$tmp/app/host-keys"
	fi
}

# check <expected-rc> <label> <expected-substring-or-empty> [extra args...]
check() {
	local want="$1" label="$2" needle="$3"
	shift 3
	local out rc
	out="$(cd "$tmp/app" && "$parser" -I "$root/$C" \
		--compose deploy/compose.yml \
		--workflow .github/workflows/cd.yml "$@" 2>&1)"
	rc=$?
	if [ "$rc" != "$want" ]; then
		echo "FAIL $label: expected rc $want, got $rc"
		printf '%s\n' "$out" | sed 's/^/      /'
		fail=$((fail + 1))
		return
	fi
	if [ -n "$needle" ] && ! printf '%s' "$out" | grep -qF "$needle"; then
		echo "FAIL $label: rc $rc as expected, but the output never said '$needle'"
		printf '%s\n' "$out" | sed 's/^/      /'
		fail=$((fail + 1))
		return
	fi
	echo "ok   $label"
	pass=$((pass + 1))
}

# The shape every app should have: one service reads the delivered file, the
# database reads a host-local one that is declared, and each name is an
# expression.
GOOD_COMPOSE='services:
  api:
    image: example
    env_file: [secrets.env]
  postgres:
    image: postgres
    env_file: [postgres-owner.env]
  gate:
    image: caddy
'
GOOD_WORKFLOW='jobs:
  deploy:
    steps:
      - uses: nicodes/komizo-actions/deploy@v0.0.22
        env:
          KOMIZO_SECRET_CLERK_SECRET_KEY: ${{ secrets.CLERK_SECRET_KEY_PROD }}
          KOMIZO_SECRET_RUNTIME_DATABASE_URL: ${{ secrets.RUNTIME_DATABASE_URL }}
'

fixture "$GOOD_COMPOSE" "$GOOD_WORKFLOW"
check 0 "the shape the rule describes" "secret rule: ok" \
	--host-only 'postgres-owner.env: postgres'

# --- rule 1/2: how a name is delivered --------------------------------------

fixture "$GOOD_COMPOSE" 'jobs:
  deploy:
    steps:
      - uses: nicodes/komizo-actions/deploy@v0.0.22
        env:
          KOMIZO_SECRET_CLERK_SECRET_KEY: sk_live_0123456789abcdef
'
check 1 "a literal value where an expression belongs" "must be a" \
	--host-only 'postgres-owner.env: postgres'

fixture "$GOOD_COMPOSE" 'jobs:
  deploy:
    steps:
      - uses: nicodes/komizo-actions/deploy@v0.0.22
        env:
          KOMIZO_SECRET_PROXY_CIDR: ${{ vars.PROXY_CIDR }}
'
check 0 "a nonsecret from a repository variable is noted, not refused" \
	"its value is public" --host-only 'postgres-owner.env: postgres'

# --- rule 3: no other way to write the store --------------------------------

fixture "$GOOD_COMPOSE" 'jobs:
  deploy:
    steps:
      - run: ssh deploy-target "cat >> /srv/app/secrets.env"
'
check 1 "writing secrets.env from a run step" "no provenance"

fixture "$GOOD_COMPOSE" 'jobs:
  deploy:
    steps:
      - run: printf %s "$VALUE" | ssh deploy-target doas /usr/local/bin/set-secret-blog KEY
'
check 1 "calling the host set-secret command by hand" "no provenance"

fixture "$GOOD_COMPOSE" 'jobs:
  deploy:
    steps:
      - run: scp deploy/prod.env deploy-target:/srv/app/
'
check 1 "copying an env file to the host" "no provenance"

# --- rule 4: who reads what -------------------------------------------------

fixture 'services:
  api:
    env_file: [secrets.env]
  gate:
    env_file: [secrets.env]
' "$GOOD_WORKFLOW"
check 1 "two services sharing the whole secret set" "receives all of them"

fixture 'services:
  api:
    env_file: [secrets.env]
  gate:
    env_file: [secrets.env]
' "$GOOD_WORKFLOW"
check 0 "…unless the workflow says that is intended" "secret rule: ok" \
	--secrets-env-services api,gate

fixture 'services:
  api:
    env_file: [secrets.env]
  worker:
    env_file: [secrets/current/worker.env]
' "$GOOD_WORKFLOW"
check 0 "a per-service file under the scoped-env directory" "secret rule: ok"

fixture 'services:
  api:
    env_file: [secrets.env]
  worker:
    env_file: [shared/prod.env]
' "$GOOD_WORKFLOW"
check 1 "an env_file with no accountable origin" "neither"

# "../secrets/current/api.env" used to be normalised into a path that matched
# the scoped-env prefix and passed. Where it actually resolves depends on the
# host's working directory, which is the opposite of an accountable origin.
fixture 'services:
  api:
    env_file: [secrets.env]
  worker:
    env_file: [../secrets/current/worker.env]
' "$GOOD_WORKFLOW"
check 1 "a parent-directory escape is not normalised into a pass" \
	"climbs out of the app directory"

fixture "$GOOD_COMPOSE" "$GOOD_WORKFLOW"
check 1 "a host-local file read by a service it was not declared for" \
	"deliberate change" --host-only 'postgres-owner.env: migrate'

# env_file also accepts a bare string and the {path:, required:} mapping. A
# checker that understands only the list form reads both as "no env_file",
# which is a pass and the wrong answer.
fixture 'services:
  api:
    env_file: secrets.env
  gate:
    env_file:
      - path: secrets.env
        required: false
' "$GOOD_WORKFLOW"
check 1 "the string and mapping forms of env_file are still read" \
	"receives all of them"

# --- rule 5: nothing committed ----------------------------------------------

fixture 'services:
  api:
    environment:
      DATABASE_URL: postgres://app:hunter2hunter2@db:5432/app
' "$GOOD_WORKFLOW"
check 1 "a password inside a committed connection string" "with a password in it"

fixture 'services:
  api:
    environment:
      DATABASE_URL: postgres://app:${DB_PASSWORD}@db:5432/app
      CLERK_PUBLISHABLE_KEY: pk_live_Y2xlcmsuZXhhbXBsZS5jb20k
' "$GOOD_WORKFLOW"
check 0 "an interpolated password and a publishable key are not secrets" \
	"secret rule: ok"

# --- the host half ----------------------------------------------------------

fixture "$GOOD_COMPOSE" "$GOOD_WORKFLOW" 'CLERK_SECRET_KEY
RUNTIME_DATABASE_URL
'
check 0 "a host holding exactly what CI delivers" "secret rule: ok" \
	--host-only 'postgres-owner.env: postgres' --host-keys host-keys

fixture "$GOOD_COMPOSE" "$GOOD_WORKFLOW" 'CLERK_SECRET_KEY
RUNTIME_DATABASE_URL
PB_ADMIN_PASSWORD
'
check 1 "a credential on the host that no workflow delivers" \
	"nothing rotates it" --host-only 'postgres-owner.env: postgres' \
	--host-keys host-keys

fixture "$GOOD_COMPOSE" "$GOOD_WORKFLOW" 'CLERK_SECRET_KEY
'
check 0 "a name added since the last deploy is a note, not a failure" \
	"is not on the host yet" --host-only 'postgres-owner.env: postgres' \
	--host-keys host-keys

# --- every failure at once --------------------------------------------------
#
# A checker that stops at the first error turns one fix into several round
# trips through CI, so the count is part of the contract.
fixture 'services:
  api:
    env_file: [secrets.env]
  gate:
    env_file: [secrets.env]
  worker:
    env_file: [../shared/prod.env]
' 'jobs:
  deploy:
    steps:
      - env:
          KOMIZO_SECRET_A: sk_live_0123456789abcdef
'
check 1 "all violations are reported in one run" "4 violation(s)"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
