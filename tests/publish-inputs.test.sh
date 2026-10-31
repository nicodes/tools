#!/usr/bin/env bash
# tests/publish-inputs.test.sh - drive publish/validate.sh over the input matrix
# the docs promise, and pin the exact helper invocation publish/run.sh builds.
#
# WHY THIS EXISTS. Same reason as tests/deploy-inputs.test.sh: the rest of this
# repository is a thin wrapper around docker and a registry, and running it in
# a test means having both. These two scripts are the pure functions of the
# inputs -- validate.sh decides whether any of the rest runs, and run.sh is the
# one line every product used to hand-roll its own copy of, which is the drift
# the action exists to kill, so its exact shape is now a contract.
#
# Run: bash tests/publish-inputs.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# Resolve before the isolated HOME removes Mise's configuration trust state.
# Exercise the helper with the installed interpreter rather than a HOME-sensitive shim.
python_bin_dir="$(dirname "$(python3 -c 'import sys; print(sys.executable)')")"

pass=0
fail=0

# A stand-in for the caller's vendored release.py: prints its argv so the exact
# invocation can be compared as a string. validate.sh only needs it to exist;
# run.sh executes it.
stub_dir="$(mktemp -d)"
stub="$stub_dir/release.py"
cat > "$stub" <<'PY'
#!/usr/bin/env python3
import sys
print(" ".join(sys.argv[1:]))
PY

# run <script> <expected-rc> <label> [VAR=VALUE ...]
#
# Fresh environment per case, as in tests/deploy-inputs.test.sh, so a value
# left over from an earlier row cannot make a later one pass. HELPER defaults
# to the stub and the registry credentials to valid values, since nearly every
# row needs them; individual rows override any of it, including with empty.
run() {
	local script="$1" want="$2" label="$3"
	shift 3
	local tmp out rc
	tmp="$(mktemp -d)"
	out="$(
		env -i \
			PATH="$python_bin_dir:$PATH" HOME="$tmp" \
			PROJECT= REVISION= COMPONENTS= \
			REGISTRY_USER=nicodes REGISTRY_TOKEN=stub-token ARTIFACT_NAME= \
			HELPER="$stub" \
			"$@" \
			bash "$script" 2>&1
	)"
	rc=$?
	if [ "$rc" -eq "$want" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      expected rc=%s, got rc=%s\n' "$label" "$want" "$rc"
		[ -n "$out" ] && printf '      %s\n' "$out"
	fi
	LAST_OUTPUT="$out"
}

# invocation_is <exact argv string> -- assert the last run.sh run asked the
# helper for exactly this. The stub prints one line of argv; run.sh's own
# status line precedes it, so compare the last line.
invocation_is() {
	local last
	last="$(printf '%s\n' "$LAST_OUTPUT" | tail -n 1)"
	if [ "$last" = "$1" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  expected invocation %s\n      got: %s\n' "$1" "$last"
	fi
}

V=publish/validate.sh
R=publish/run.sh
SHA=0123456789abcdef0123456789abcdef01234567

echo "== validate.sh: the shapes the products publish with =="

# THE FLEET'S TWO CALL SITES. sample-be and komizo-be, verbatim, must both be
# accepted before anything is downloaded or logged into.
run $V 0 "sample's full shape is accepted" \
	PROJECT=sample REVISION=$SHA COMPONENTS='api db gate config' \
	ARTIFACT_NAME="release-$SHA"

run $V 0 "komizo's shape, artifact already on disk, is accepted" \
	PROJECT=komizo REVISION=$SHA COMPONENTS='service gate config'

echo "== validate.sh: project =="

run $V 1 "an empty project is refused" REVISION=$SHA COMPONENTS=api
run $V 1 "an uppercase project is refused" PROJECT=Sample REVISION=$SHA COMPONENTS=api
run $V 1 "a project with a slash is refused" PROJECT=my/app REVISION=$SHA COMPONENTS=api
run $V 1 "a project starting with a digit is refused" PROJECT=1sample REVISION=$SHA COMPONENTS=api
run $V 0 "digits and hyphens after the first letter are allowed" \
	PROJECT=sample-2 REVISION=$SHA COMPONENTS=api

echo "== validate.sh: revision =="

run $V 1 "an empty revision is refused" PROJECT=sample COMPONENTS=api
run $V 1 "a short revision is refused" \
	PROJECT=sample REVISION=abc1234 COMPONENTS=api
run $V 1 "an uppercase revision is refused" \
	PROJECT=sample REVISION=0123456789ABCDEF0123456789ABCDEF01234567 COMPONENTS=api
run $V 1 "a non-hex revision is refused" \
	PROJECT=sample REVISION=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz COMPONENTS=api

echo "== validate.sh: components =="

run $V 1 "empty components are refused" PROJECT=sample REVISION=$SHA
# Whitespace-only is not a list -- the same call deploy/resolve.sh makes for a
# names list.
run $V 1 "whitespace-only components are refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=$'   \n  '
run $V 1 "an invalid component is refused" PROJECT=sample REVISION=$SHA COMPONENTS='api ../web'
# The helper requires distinct components; a repeat is usually a paste error.
run $V 1 "a repeated component is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS='api api'
run $V 0 "a single component is allowed" PROJECT=sample REVISION=$SHA COMPONENTS=api
run $V 0 "every allowed component at once is accepted" \
	PROJECT=sample REVISION=$SHA COMPONENTS='api db pb service gate config maintenance'
# The helper takes --components as nargs='+', so any whitespace separation
# reaches it as one argv word per component.
run $V 0 "newline separation is accepted (whitespace splits)" \
	PROJECT=sample REVISION=$SHA COMPONENTS=$'api\ndb'

echo "== validate.sh: registry credentials =="

# The empty overrides are what these rows are about: the harness defaults the
# credentials to valid values, so a row must say REGISTRY_USER= or
# REGISTRY_TOKEN= to test the missing half.
run $V 1 "an empty registry-user is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER=
run $V 1 "a malformed registry-user is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER='nicodes;oops'
# Bot logins are valid GitHub usernames: a post-merge dispatch runs as
# github-actions[bot], so a caller passing registry-user: ${{ github.actor }}
# hands the guard brackets. They must be admitted.
run $V 0 "a github-actions[bot] registry-user is accepted" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER='github-actions[bot]'
run $V 0 "a dependabot[bot] registry-user is accepted" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER='dependabot[bot]'
# Brackets are the only widening: injection shapes stay refused.
run $V 1 "a registry-user with a space is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER='nico des'
# shellcheck disable=SC2016 # the backticks are a LITERAL test input
run $V 1 "a registry-user with a backtick is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER='nico`des`'
# shellcheck disable=SC2016 # the $( ) is a LITERAL test input
run $V 1 "a registry-user with a command substitution is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_USER='$(id)'
run $V 1 "an empty registry-token is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api REGISTRY_TOKEN=

# The token is read but never echoed -- not even on the way to an unrelated
# refusal. A secret that appears in a failure message is one that ends up in
# the job log.
run $V 1 "the token never appears in output" \
	PROJECT=sample REVISION=$SHA COMPONENTS='api ../web' REGISTRY_TOKEN=token-must-not-appear
if printf '%s' "$LAST_OUTPUT" | grep -qF 'token-must-not-appear'; then
	fail=$((fail + 1))
	printf 'FAIL  the registry token leaked into validate.sh output\n'
else
	pass=$((pass + 1))
fi

echo "== validate.sh: artifact name and helper =="

run $V 0 "a plain artifact name is accepted" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api ARTIFACT_NAME="release-$SHA"
run $V 1 "an artifact name with a space is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api ARTIFACT_NAME='release sha'
run $V 1 "an artifact name with a slash is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api ARTIFACT_NAME='sample/release'

run $V 1 "a missing helper is refused" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api HELPER=scripts/engineering/nope.py

echo "== run.sh: the exact invocation =="

# The argv every product built by hand. If this changes, every product's
# publish changes with it -- so the tests say what it must be.
run $R 0 "sample's invocation is built exactly" \
	PROJECT=sample REVISION=$SHA COMPONENTS='api db gate config' HELPER="$stub"
invocation_is "publish --project sample --revision $SHA --components api db gate config"

run $R 0 "komizo's invocation is built exactly" \
	PROJECT=komizo REVISION=$SHA COMPONENTS='service gate config' HELPER="$stub"
invocation_is "publish --project komizo --revision $SHA --components service gate config"

run $R 0 "one component becomes one argv word" \
	PROJECT=sample REVISION=$SHA COMPONENTS=api HELPER="$stub"
invocation_is "publish --project sample --revision $SHA --components api"

# Repeated separators collapse: argparse nargs='+' sees one word per component.
run $R 0 "extra spaces between components collapse" \
	PROJECT=sample REVISION=$SHA COMPONENTS='api  db' HELPER="$stub"
invocation_is "publish --project sample --revision $SHA --components api db"

echo "== the internalized pins =="

# The point of the action: the login pin lives here, named for its version,
# once for the fleet -- and the artifact fetch carries its own pin the same
# way. If either drifts from the fleet's current SHA, this fails loudly rather
# than silently.
if grep -q 'docker/login-action@dbcb813823bdd20940b903addbd779551569679f # v4.6.0' publish/action.yml \
	&& grep -q 'actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2' publish/action.yml; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  publish/action.yml third-party pins drifted from the documented SHAs\n'
fi

# Nothing but validate.sh may see the token: the invocation script must not
# read it at all.
if grep -q 'REGISTRY_TOKEN' publish/run.sh; then
	fail=$((fail + 1))
	printf 'FAIL  publish/run.sh reads REGISTRY_TOKEN; only the login step needs it\n'
else
	pass=$((pass + 1))
fi


run $V 0 "caller-defined components and namespace are accepted" PROJECT=sample REVISION=$SHA COMPONENTS='renderer worker' IMAGE_BASE=ghcr.io/example/sample
run $V 1 "namespace injection is refused before login" PROJECT=sample REVISION=$SHA COMPONENTS=renderer IMAGE_BASE='ghcr.io/example/sample;exit'
run $R 0 "generic helper receives explicit namespace" PROJECT=sample REVISION=$SHA COMPONENTS=renderer IMAGE_BASE=ghcr.io/example/sample
invocation_is "publish --project sample --image-base ghcr.io/example/sample --revision $SHA --components renderer"
[ "$fail" -eq 0 ]

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
