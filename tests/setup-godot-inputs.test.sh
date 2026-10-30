#!/usr/bin/env bash
# tests/setup-godot-inputs.test.sh - pin the exact shape of the setup-godot
# composite: the input default, the cache path and key, the env threading,
# and the caller-script invocation.
#
# WHY THIS EXISTS. setup-godot/ is two steps and ships no scripts of its own,
# so there is nothing here to execute -- what can drift is the action.yml
# itself, and every drift reaches the whole fleet the moment a product bumps
# its `uses:`. The cache path and GODOT_ARCHIVE_DIR must stay byte-equal or
# the archives land outside the directory the cache restores; the key must
# keep hashing the caller's script and release manifest or a reviewed Godot
# bump silently reuses the previous archive directory; and the run line must
# keep invoking the CALLER's tools/setup_godot.sh -- the day this action
# vendors its own copy, the drift it exists to kill is back.
#
# Run: bash tests/setup-godot-inputs.test.sh
# shellcheck disable=SC2016 # the needles below are LITERAL ${{ }} expressions
# from setup-godot/action.yml -- single quotes are the point, not a mistake.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0
fail=0

# pins <label> -- the rest of the arguments are a fixed string that must
# appear verbatim in setup-godot/action.yml.
pins() {
	local label="$1" needle="$2"
	if grep -qF "$needle" setup-godot/action.yml; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      missing: %s\n' "$label" "$needle"
	fi
}

# refuses <label> <fixed-string> -- the string must NOT appear in
# setup-godot/action.yml.
refuses() {
	local label="$1" needle="$2"
	if grep -qF "$needle" setup-godot/action.yml; then
		fail=$((fail + 1))
		printf 'FAIL  %s\n      found: %s\n' "$label" "$needle"
	else
		pass=$((pass + 1))
	fi
}

echo "== the input =="

pins "install-templates defaults to 'false'" "default: 'false'"
pins "install-templates is optional" "required: false"

echo "== the cache step =="

# The internalized pin, named for its version -- the same actions/cache SHA
# the three local composites carried.
pins "the actions/cache pin is the fleet's SHA" \
	"actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0"
# Neutral path: no repository name in it. Caches are repo-scoped, so the
# shared path cannot cross-contaminate.
pins "the cache path is the neutral runner.temp directory" \
	'path: ${{ runner.temp }}/godot-downloads'
# The key hashes the caller's script and the caller's reviewed release
# manifest -- hashFiles evaluates against the caller's workspace in a
# composite action, so a product's Godot bump is its own cache generation.
# The -v2 generation suffix is because the neutral path changed the key
# space this action took the step over: an exact-key miss forces one clean
# re-download (still checksum-verified) rather than restoring an entry saved
# under a product's old local composite, which hashed the same caller files.
pins "the cache key carries the v2 generation and the caller's hashes" \
	"key: godot-verified-v2-\${{ runner.os }}-\${{ hashFiles('tools/setup_godot.sh', 'tools/godot-release.json') }}"

echo "== the install step =="

# The env block, both halves: the input threaded as data, and the archive
# directory override every product's script reads.
pins "INSTALL_TEMPLATES is threaded as an env var" \
	'INSTALL_TEMPLATES: ${{ inputs.install-templates }}'
pins "GODOT_ARCHIVE_DIR points at the cache directory" \
	'GODOT_ARCHIVE_DIR: ${{ runner.temp }}/godot-downloads'
# The invocation itself: the CALLER's script, never one shipped here.
pins "the caller's script is invoked" "run: bash tools/setup_godot.sh"

# The cache path and the archive-dir override are the same directory. Both
# are pinned above; this says they must remain one string, so a future edit
# cannot move one without the other.
path_count="$(grep -cF '${{ runner.temp }}/godot-downloads' setup-godot/action.yml)"
if [ "$path_count" -eq 2 ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  the cache path and GODOT_ARCHIVE_DIR drifted apart (%s occurrences of the neutral path, want 2)\n' "$path_count"
fi

echo "== what the action must never do =="

# No interpolation inside the run line: inputs arrive as env vars, so a
# crafted value is data to bash rather than shell source.
if grep '^ *run:' setup-godot/action.yml | grep -qF '${{'; then
	fail=$((fail + 1))
	printf 'FAIL  a run: line interpolates ${{ }} -- inputs must arrive via env:\n'
else
	pass=$((pass + 1))
fi

# Nothing vendored: the directory holds the action metadata and nothing
# else. A second copy of the install script here would be the drift the
# action exists to kill.
if [ "$(ls setup-godot)" = "action.yml" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  setup-godot/ ships more than action.yml: %s\n' "$(ls setup-godot)"
fi

# No sibling composition: setup-godot is a top-level action like publish,
# composing only third-party actions/cache -- so it stays out of
# scripts/release.py's SUBACTIONS and no release rewrites anything in it.
refuses "no komizo-actions sibling is composed" "nicodes/komizo-actions/"

# Metadata is expression-free. GitHub template-validates everything above
# `runs:` at action-load time, where contexts such as runner do not exist:
# a literal ${{ }} in a description makes the action UNLOADABLE by any
# caller ("Unrecognized named-value: 'runner'"), which is what v0.0.12
# shipped before this check existed. Step-level expressions are evaluated
# per caller run and are fine -- this covers the metadata alone.
metadata_exprs="$(awk '/^runs:/{exit} /\$\{\{/ {print FNR": "$0}' setup-godot/action.yml)"
if [ -z "$metadata_exprs" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  metadata contains ${{ }} expressions GitHub refuses to load:\n%s\n' "$metadata_exprs"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
