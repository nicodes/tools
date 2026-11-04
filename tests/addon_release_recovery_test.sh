#!/usr/bin/env bash
# Negative controls for immutable release retry. These never call GitHub.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
SCRIPT="$ROOT_DIR/helpers/addon-release-recovery.sh"
ASSET_NAME='example-addon.zip'
export RELEASE_ASSET_NAME=$ASSET_NAME
# Frozen test pin. The planner accepts this exact digest and refuses a
# disagreed release identity. No network or production package is involved.
V004_SHA256=85e7dc926971ee210af309e577c816300f83cfdef64ca131c4be5e785450c047

controls=0
reached=0
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

expect_failure() {
    local label="$1"
    shift
    local status=0
    local out="$work/fail-output"
    local err="$work/fail-err"
    : >"$out"
    : >"$err"
    set +e
    GITHUB_OUTPUT="$out" "$@" >"$work/fail-stdout" 2>"$err"
    status=$?
    set -e
    if [ "$status" -eq 0 ]; then
        echo "Negative control unexpectedly passed: $label" >&2
        exit 1
    fi
    if [ "$status" -eq 99 ]; then
        echo "Negative control invoked a forbidden gh mutation or unplanned gh call: $label" >&2
        exit 1
    fi
    if [ -s "$out" ]; then
        echo "Negative control wrote release outputs: $label" >&2
        cat "$out" >&2
        exit 1
    fi
    if [ -n "${EXPECT_MSG:-}" ] && ! grep -F -- "$EXPECT_MSG" "$err" >/dev/null; then
        echo "Negative control missed expected message: $label" >&2
        echo "expected: $EXPECT_MSG" >&2
        cat "$err" >&2
        exit 1
    fi
    controls=$((controls + 1))
    echo "CONTROL_FAIL_OK $label"
}

install_tripwire_gh() {
    local bin="$1"
    mkdir -p "$bin"
    cat >"$bin/gh" <<'EOF'
#!/bin/bash
echo "gh must not be called: $*" >&2
exit 99
EOF
    chmod +x "$bin/gh"
}

init_repo() {
    local repo="$1"
    mkdir -p "$repo"
    git -C "$repo" init -b main >/dev/null
    git -C "$repo" config user.email test@example.com
    git -C "$repo" config user.name test
}

commit_plugin() {
    local repo="$1"
    local version="$2"
    local message="$3"
    mkdir -p "$repo/addon"
    printf '[plugin]\nversion="%s"\n' "$version" >"$repo/addon/plugin.cfg"
    git -C "$repo" add addon/plugin.cfg
    git -C "$repo" commit --allow-empty -m "$message" >/dev/null
    git -C "$repo" rev-parse HEAD
}

output_value() {
    local file="$1"
    local key="$2"
    sed -n "s/^${key}=//p" "$file" | head -n 1
}

assert_eq() {
    local label="$1"
    local got="$2"
    local want="$3"
    if [ "$got" != "$want" ]; then
        echo "$label: expected [$want] got [$got]" >&2
        exit 1
    fi
}

run_plan() {
    local repo="$1"
    local out="$2"
    shift 2
    : >"$out"
    env \
        GITHUB_OUTPUT="$out" \
        GITHUB_REF=refs/heads/main \
        GITHUB_SHA="$(git -C "$repo" rev-parse HEAD)" \
        RELEASE_REPO_ROOT="$repo" \
        PATH="$tripwire:$PATH" \
        "$@" \
        "$SCRIPT" plan
}

tripwire="$work/tripwire-bin"
install_tripwire_gh "$tripwire"

base="$work/base"
init_repo "$base"
base_parent="$(commit_plugin "$base" 0.0.4 base)"
base_tag="$(commit_plugin "$base" 0.0.4 tagged)"
git -C "$base" tag v0.0.4 "$base_tag"
base_main="$(commit_plugin "$base" 0.0.4 main)"

expect_failure ref-not-main \
    env GITHUB_REF=refs/heads/feature GITHUB_SHA="$base_main" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    "$SCRIPT" plan
expect_failure missing-permitted-sha \
    env GITHUB_REF=refs/heads/main RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    "$SCRIPT" plan
EXPECT_MSG='is not the permitted main target' expect_failure checkout-not-permitted-target \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$base_parent" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    "$SCRIPT" plan
expect_failure bad-tag \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$base_main" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    RETRY_TAG=v0.0.4-rc1 RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" plan
expect_failure bad-digest \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$base_main" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    RETRY_TAG=v0.0.4 RETRY_SHA256=85E7DC926971EE210AF309E577C816300F83CFDEF64CA131C4BE5E785450C047 \
    "$SCRIPT" plan
partial="$work/partial"
init_repo "$partial"
partial_old="$(commit_plugin "$partial" 0.0.4 old)"
git -C "$partial" tag v0.0.4 "$partial_old"
partial_main="$(commit_plugin "$partial" 0.0.5 next)"
expect_failure tag-without-digest \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$partial_main" RELEASE_REPO_ROOT="$partial" PATH="$tripwire:$PATH" \
    RETRY_TAG=v0.0.4 \
    "$SCRIPT" plan
expect_failure digest-without-tag \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$partial_main" RELEASE_REPO_ROOT="$partial" PATH="$tripwire:$PATH" \
    BUMP=patch RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" plan
expect_failure missing-tag \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$base_main" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    RETRY_TAG=v9.9.9 RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" plan

side="$work/side"
init_repo "$side"
side_main="$(commit_plugin "$side" 0.0.4 main)"
git -C "$side" checkout -b side "$side_main" >/dev/null
side_tip="$(commit_plugin "$side" 0.0.8 side)"
git -C "$side" tag v0.0.8 "$side_tip"
git -C "$side" checkout main >/dev/null
expect_failure tag-not-ancestor \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$side_main" RELEASE_REPO_ROOT="$side" PATH="$tripwire:$PATH" \
    RETRY_TAG=v0.0.8 RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" plan

mismatch="$work/mismatch"
init_repo "$mismatch"
mismatch_tag="$(commit_plugin "$mismatch" 0.0.3 old)"
git -C "$mismatch" tag v0.0.4 "$mismatch_tag"
mismatch_main="$(commit_plugin "$mismatch" 0.0.4 main)"
expect_failure tag-plugin-mismatch \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$mismatch_main" RELEASE_REPO_ROOT="$mismatch" PATH="$tripwire:$PATH" \
    RETRY_TAG=v0.0.4 RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" plan

moved="$work/moved"
init_repo "$moved"
moved_tag="$(commit_plugin "$moved" 0.0.4 tagged)"
git -C "$moved" tag v0.0.4 "$moved_tag"
moved_main="$(commit_plugin "$moved" 0.0.5 bumped)"
expect_failure main-plugin-mismatch \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$moved_main" RELEASE_REPO_ROOT="$moved" PATH="$tripwire:$PATH" \
    RETRY_TAG=v0.0.4 RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" plan

duplicate="$work/duplicate"
init_repo "$duplicate"
duplicate_old="$(commit_plugin "$duplicate" 0.0.4 old)"
git -C "$duplicate" tag v0.0.4 "$duplicate_old"
commit_plugin "$duplicate" 0.0.5 next >/dev/null
dup_bin="$work/dup-bin"
mkdir -p "$dup_bin"
cat >"$dup_bin/git" <<'EOF'
#!/bin/bash
set -euo pipefail
if [ "${1:-}" = "-C" ] && [ "${3:-}" = "tag" ] && [ "${4:-}" = "--list" ]; then
    printf '%s\n' v0.0.4
    exit 0
fi
if [ "${1:-}" = "-C" ] && [ "${3:-}" = "rev-parse" ] && [[ "$*" == *refs/tags/v0.0.5* ]]; then
    printf '%s\n' 0123456789abcdef0123456789abcdef01234567
    exit 0
fi
exec /usr/bin/git "$@"
EOF
chmod +x "$dup_bin/git"
EXPECT_MSG='Tag already exists: v0.0.5' expect_failure duplicate-bump-tag \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$(git -C "$duplicate" rev-parse HEAD)" RELEASE_REPO_ROOT="$duplicate" PATH="$dup_bin:$tripwire:$PATH" \
    BUMP=patch \
    "$SCRIPT" plan
expect_failure bad-bump \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$base_main" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    BUMP=weekly \
    "$SCRIPT" plan
expect_failure bump-plugin-mismatch \
    env GITHUB_REF=refs/heads/main GITHUB_SHA="$base_main" RELEASE_REPO_ROOT="$base" PATH="$tripwire:$PATH" \
    BUMP=patch \
    "$SCRIPT" plan

plan_out="$work/plan-ok"
run_plan "$base" "$plan_out" RETRY_TAG=v0.0.4 RETRY_SHA256="$V004_SHA256" BUMP=nope
assert_eq retry-ancestor-version "$(output_value "$plan_out" version)" 0.0.4
assert_eq retry-ancestor-tag "$(output_value "$plan_out" tag)" v0.0.4
assert_eq retry-ancestor-retry "$(output_value "$plan_out" retry)" true
assert_eq retry-ancestor-target "$(output_value "$plan_out" target)" "$base_tag"
if [ "$base_tag" = "$base_main" ]; then
    echo "retry ancestor fixture did not create a descendant main commit" >&2
    exit 1
fi
assert_eq retry-ancestor-sha "$(output_value "$plan_out" sha256)" "$V004_SHA256"
reached=$((reached + 1))

equal="$work/equal"
init_repo "$equal"
equal_tag="$(commit_plugin "$equal" 0.0.4 only)"
git -C "$equal" tag v0.0.4 "$equal_tag"
run_plan "$equal" "$plan_out" RETRY_TAG=v0.0.4 RETRY_SHA256="$V004_SHA256"
assert_eq retry-equal-target "$(output_value "$plan_out" target)" "$equal_tag"
assert_eq retry-equal-retry "$(output_value "$plan_out" retry)" true
reached=$((reached + 1))

next="$work/next"
init_repo "$next"
next_old="$(commit_plugin "$next" 0.0.4 old)"
git -C "$next" tag v0.0.4 "$next_old"
commit_plugin "$next" 0.0.5 next >/dev/null
run_plan "$next" "$plan_out" BUMP=patch
assert_eq bump-patch-version "$(output_value "$plan_out" version)" 0.0.5
assert_eq bump-patch-tag "$(output_value "$plan_out" tag)" v0.0.5
assert_eq bump-patch-retry "$(output_value "$plan_out" retry)" false
assert_eq bump-patch-target "$(output_value "$plan_out" target)" "$(git -C "$next" rev-parse HEAD)"
assert_eq bump-patch-sha "$(output_value "$plan_out" sha256)" ""
reached=$((reached + 1))
commit_plugin "$next" 0.1.0 minor >/dev/null
run_plan "$next" "$plan_out" BUMP=minor
assert_eq bump-minor-tag "$(output_value "$plan_out" tag)" v0.1.0
reached=$((reached + 1))
commit_plugin "$next" 1.0.0 major >/dev/null
run_plan "$next" "$plan_out" BUMP=major
assert_eq bump-major-tag "$(output_value "$plan_out" tag)" v1.0.0
reached=$((reached + 1))

first="$work/first"
init_repo "$first"
commit_plugin "$first" 0.0.1 first >/dev/null
run_plan "$first" "$plan_out" BUMP=patch
assert_eq bump-first-tag "$(output_value "$plan_out" tag)" v0.0.1
assert_eq bump-first-retry "$(output_value "$plan_out" retry)" false
reached=$((reached + 1))

install_fake_gh() {
    local bin="$1"
    mkdir -p "$bin"
    cat >"$bin/gh" <<'EOF'
#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >>"${GH_LOG:?}"
joined="$*"
case "$joined" in
    *'release create'*|*'release delete'*|*'release edit'*|*'release upload'*)
        echo "mutation refused: $joined" >&2
        exit 99
        ;;
esac
if [ "${1:-}" != "release" ]; then
    echo "unexpected gh: $joined" >&2
    exit 97
fi
cmd="$2"
shift 2
tag="${1:-}"
shift || true
if [ "$tag" != "${FAKE_TAG:?}" ]; then
    echo "unexpected tag: $tag" >&2
    exit 97
fi
case "$cmd" in
    view)
        if [[ "$*" == *targetCommitish* ]]; then
            printf '%s\n' "${FAKE_TARGET:?}"
            exit 0
        fi
        if [[ "$*" == *assets* ]]; then
            printf '%s\n' "${FAKE_DIGEST:-}"
            exit 0
        fi
        echo "unexpected view: $*" >&2
        exit 97
        ;;
    download)
        dir=""
        pattern=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --dir) dir="$2"; shift 2 ;;
                --pattern) pattern="$2"; shift 2 ;;
                *) echo "unexpected download arg: $1" >&2; exit 97 ;;
            esac
        done
        if [ "$pattern" != "${FAKE_ASSET:?}" ]; then
            echo "unexpected asset pattern: $pattern" >&2
            exit 97
        fi
        mkdir -p "$dir"
        case "${FAKE_DOWNLOAD_MODE:-file}" in
            missing) exit 0 ;;
            extra) printf extra >"$dir/extra.txt" ;;
        esac
        cp "${FAKE_ZIP:?}" "$dir/$pattern"
        exit 0
        ;;
    *)
        echo "unexpected release cmd: $cmd" >&2
        exit 97
        ;;
esac
EOF
    chmod +x "$bin/gh"
}

fake_bin="$work/fake-bin"
install_fake_gh "$fake_bin"
good_zip="$work/good.zip"
bad_zip="$work/bad.zip"
printf 'exact-existing-zip' >"$good_zip"
printf 'tampered-zip' >"$bad_zip"
good_sha="$(sha256sum "$good_zip" | awk '{print $1}')"
other_target=0123456789abcdef0123456789abcdef01234567
gh_log="$work/gh.log"

run_recover() {
    local mode="$1"
    local zip="$2"
    local expected="$3"
    local fake_target="$4"
    local fake_digest="$5"
    : >"$gh_log"
    PATH="$fake_bin:$PATH" \
        GH_LOG="$gh_log" \
        FAKE_TAG=v0.0.4 \
        FAKE_TARGET="$fake_target" \
        FAKE_DIGEST="$fake_digest" \
        FAKE_ASSET="$ASSET_NAME" \
        FAKE_ZIP="$zip" \
        FAKE_DOWNLOAD_MODE="$mode" \
        TAG=v0.0.4 \
        TARGET="$base_tag" \
        EXPECTED_SHA256="$expected" \
        RELEASE_DIST_DIR="$work/recovered" \
        "$SCRIPT" recover
}

: >"$gh_log"
expect_failure release-target-mismatch \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$other_target" \
        FAKE_DIGEST="sha256:$good_sha" FAKE_ASSET="$ASSET_NAME" FAKE_ZIP="$good_zip" \
        FAKE_DOWNLOAD_MODE=file TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
        RELEASE_DIST_DIR="$work/recovered" \
    "$SCRIPT" recover
if grep -q 'release download' "$gh_log"; then
    echo "target mismatch downloaded a release" >&2
    exit 1
fi
reached=$((reached + 1))

expect_failure recover-bad-digest \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$base_tag" \
        FAKE_DIGEST="sha256:$good_sha" FAKE_ASSET="$ASSET_NAME" FAKE_ZIP="$good_zip" \
        TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256=deadbeef \
        RELEASE_DIST_DIR="$work/recovered" \
    "$SCRIPT" recover
expect_failure file-digest-mismatch \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$base_tag" \
        FAKE_DIGEST="sha256:$good_sha" FAKE_ASSET="$ASSET_NAME" FAKE_ZIP="$bad_zip" \
        FAKE_DOWNLOAD_MODE=file TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
        RELEASE_DIST_DIR="$work/recovered" \
    "$SCRIPT" recover
expect_failure api-digest-mismatch \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$base_tag" \
        FAKE_DIGEST="sha256:$V004_SHA256" FAKE_ASSET="$ASSET_NAME" FAKE_ZIP="$good_zip" \
        FAKE_DOWNLOAD_MODE=file TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
        RELEASE_DIST_DIR="$work/recovered" \
    "$SCRIPT" recover
expect_failure missing-download \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$base_tag" \
        FAKE_DIGEST="sha256:$good_sha" FAKE_ASSET="$ASSET_NAME" FAKE_ZIP="$good_zip" \
        FAKE_DOWNLOAD_MODE=missing TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
        RELEASE_DIST_DIR="$work/recovered" \
    "$SCRIPT" recover
expect_failure extra-download-file \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$base_tag" \
        FAKE_DIGEST="sha256:$good_sha" FAKE_ASSET="$ASSET_NAME" FAKE_ZIP="$good_zip" \
        FAKE_DOWNLOAD_MODE=extra TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
        RELEASE_DIST_DIR="$work/recovered" \
    "$SCRIPT" recover

rm -rf "$work/recovered"
: >"$gh_log"
run_recover file "$good_zip" "$good_sha" "$base_tag" "sha256:$good_sha" >/dev/null
test -f "$work/recovered/$ASSET_NAME"
cmp -s "$good_zip" "$work/recovered/$ASSET_NAME"
if grep -Eq 'release (create|delete|edit|upload)' "$gh_log"; then
    echo "recover mutated a GitHub release" >&2
    exit 1
fi
grep -q 'release download' "$gh_log"
reached=$((reached + 1))

expect_failure recheck-target-mismatch \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$other_target" \
        FAKE_DIGEST="sha256:$good_sha" FAKE_ASSET="$ASSET_NAME" \
        TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
    "$SCRIPT" recheck
expect_failure recheck-digest-mismatch \
    env PATH="$fake_bin:$PATH" GH_LOG="$gh_log" FAKE_TAG=v0.0.4 FAKE_TARGET="$base_tag" \
        FAKE_DIGEST="sha256:$V004_SHA256" FAKE_ASSET="$ASSET_NAME" \
        TAG=v0.0.4 TARGET="$base_tag" EXPECTED_SHA256="$good_sha" \
    "$SCRIPT" recheck
: >"$gh_log"
PATH="$fake_bin:$PATH" \
    GH_LOG="$gh_log" \
    FAKE_TAG=v0.0.4 \
    FAKE_TARGET="$base_tag" \
    FAKE_DIGEST="sha256:$V004_SHA256" \
    FAKE_ASSET="$ASSET_NAME" \
    TAG=v0.0.4 \
    TARGET="$base_tag" \
    EXPECTED_SHA256="$V004_SHA256" \
    "$SCRIPT" recheck >/dev/null
if grep -Eq 'release (create|delete|edit|upload|download)' "$gh_log"; then
    echo "recheck downloaded or mutated a release" >&2
    exit 1
fi
reached=$((reached + 1))

expect_failure assert-bump-retry \
    env PATH="$tripwire:$PATH" RETRY=true \
    "$SCRIPT" assert-bump
expect_failure assert-bump-empty \
    env PATH="$tripwire:$PATH" \
    "$SCRIPT" assert-bump
expect_failure assert-bump-retry-inputs \
    env PATH="$tripwire:$PATH" RETRY=false RETRY_TAG=v0.0.4 RETRY_SHA256="$V004_SHA256" \
    "$SCRIPT" assert-bump
expect_failure unknown-command \
    env PATH="$tripwire:$PATH" \
    "$SCRIPT" create
PATH="$tripwire:$PATH" RETRY=false "$SCRIPT" assert-bump >/dev/null
reached=$((reached + 1))

if [ "$controls" -ne 26 ] || [ "$reached" -ne 10 ]; then
    echo "Expected 26 negative controls and 10 reached paths, observed controls=$controls reached=$reached" >&2
    exit 1
fi
echo "PASS release_recovery_test controls=$controls reached=$reached"
