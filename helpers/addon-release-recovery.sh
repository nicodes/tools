#!/usr/bin/env bash
# Plan a protected-main release, or bind a retry to an existing immutable tag.
# Retry never creates or moves a tag or GitHub release. assert-bump is the last
# gate in front of `gh release create` on the normal bump path.
set -euo pipefail

ROOT_DIR=$PWD
ASSET_NAME=${RELEASE_ASSET_NAME:?declare the exact addon release asset}
if ! [[ "$ASSET_NAME" =~ ^[A-Za-z0-9@_.-]+\.zip$ ]] || [[ "$ASSET_NAME" == .* ]]; then
    echo "Release asset must be one safe ZIP basename." >&2
    exit 1
fi

usage() {
    echo "usage: addon-release-recovery.sh plan|recover|recheck|assert-bump" >&2
    exit 2
}

repo_root() {
    printf '%s\n' "${RELEASE_REPO_ROOT:-$ROOT_DIR}"
}

plugin_version_text() {
    local text="$1"
    printf '%s\n' "$text" | sed -n -E 's/^version="([^"]+)"/\1/p' | head -n 1
}

require_sha() {
    local label="$1"
    local value="$2"
    if ! [[ "$value" =~ ^[0-9a-f]{40}$ ]]; then
        echo "$label must be a 40-character lowercase commit SHA." >&2
        exit 1
    fi
}

require_tag() {
    local tag="$1"
    if ! [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Tag must be an immutable vX.Y.Z tag: $tag" >&2
        exit 1
    fi
}

require_sha256() {
    local value="$1"
    if ! [[ "$value" =~ ^[0-9a-f]{64}$ ]]; then
        echo "SHA-256 must be 64 lowercase hex characters." >&2
        exit 1
    fi
}

require_protected_main() {
    local repo="$1"
    local head=""
    if [ "${GITHUB_REF:-}" != "refs/heads/main" ]; then
        echo "Run releases from protected main (refs/heads/main)." >&2
        exit 1
    fi
    if [ -z "${GITHUB_SHA:-}" ]; then
        echo "GITHUB_SHA is required as the permitted main target." >&2
        exit 1
    fi
    require_sha "Permitted main target" "$GITHUB_SHA"
    head="$(git -C "$repo" rev-parse HEAD)"
    if [ "$head" != "$GITHUB_SHA" ]; then
        echo "Checkout $head is not the permitted main target $GITHUB_SHA." >&2
        exit 1
    fi
}

write_output() {
    local key="$1"
    local value="$2"
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        printf '%s=%s\n' "$key" "$value" >>"$GITHUB_OUTPUT"
    fi
    printf '%s=%s\n' "$key" "$value"
}

emit_plan() {
    write_output version "$1"
    write_output tag "$2"
    write_output retry "$3"
    write_output target "$4"
    write_output sha256 "$5"
}

cmd_plan() {
    local repo=""
    local retry_tag=""
    local retry_sha=""
    local bump=""
    local target=""
    local version=""
    local tag_plugin=""
    local head_plugin=""
    local shown=""
    local latest=""
    local major=""
    local minor=""
    local patch=""
    local tag=""
    repo="$(repo_root)"
    require_protected_main "$repo"
    retry_tag="${RETRY_TAG:-}"
    retry_sha="${RETRY_SHA256:-}"
    bump="${BUMP:-}"
    if [ -z "$retry_tag" ] && [ -z "$retry_sha" ]; then
        case "$bump" in patch|minor|major) ;; *) echo "Unsupported bump: $bump" >&2; exit 1 ;; esac
    fi

    # Either retry input selects the immutable path. A partial pair must not
    # fall through into a bump and create a new release.
    if [ -n "$retry_tag" ] || [ -n "$retry_sha" ]; then
        if [ -z "$retry_tag" ] || [ -z "$retry_sha" ]; then
            echo "retry-tag and retry-sha256 must be set together." >&2
            exit 1
        fi
        require_tag "$retry_tag"
        require_sha256 "$retry_sha"
        if ! target="$(git -C "$repo" rev-list -n 1 "$retry_tag" 2>/dev/null)"; then
            echo "retry-tag does not resolve to a commit: $retry_tag" >&2
            exit 1
        fi
        require_sha "Tag target" "$target"
        if ! git -C "$repo" merge-base --is-ancestor "$target" "$GITHUB_SHA"; then
            echo "Tag $retry_tag target $target is not an ancestor of permitted main $GITHUB_SHA." >&2
            exit 1
        fi
        version="${retry_tag#v}"
        if ! shown="$(git -C "$repo" show "${target}:addon/plugin.cfg" 2>/dev/null)"; then
            echo "addon/plugin.cfg is missing at tag target $target." >&2
            exit 1
        fi
        tag_plugin="$(plugin_version_text "$shown")"
        if [ -z "$tag_plugin" ]; then
            echo "addon/plugin.cfg at $target has no version." >&2
            exit 1
        fi
        if [ ! -f "$repo/addon/plugin.cfg" ]; then
            echo "addon/plugin.cfg is missing on permitted main." >&2
            exit 1
        fi
        head_plugin="$(plugin_version_text "$(cat "$repo/addon/plugin.cfg")")"
        if [ "$tag_plugin" != "$version" ] || [ "$head_plugin" != "$version" ]; then
            echo "plugin.cfg must equal $version at the immutable tag and on permitted main (tag=$tag_plugin main=$head_plugin)." >&2
            exit 1
        fi
        emit_plan "$version" "$retry_tag" true "$target" "$retry_sha"
        return 0
    fi

    latest="$(git -C "$repo" tag --list 'v[0-9]*' | sed -E 's/^v//' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"
    if [ -z "$latest" ]; then
        version="0.0.1"
    else
        IFS=. read -r major minor patch <<<"$latest"
        case "$bump" in
            major) major=$((major + 1)); minor=0; patch=0 ;;
            minor) minor=$((minor + 1)); patch=0 ;;
            patch) patch=$((patch + 1)) ;;
            *) echo "Unsupported bump: $bump" >&2; exit 1 ;;
        esac
        version="${major}.${minor}.${patch}"
    fi
    tag="v${version}"
    if git -C "$repo" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
        echo "Tag already exists: $tag" >&2
        exit 1
    fi
    if [ ! -f "$repo/addon/plugin.cfg" ]; then
        echo "addon/plugin.cfg is missing." >&2
        exit 1
    fi
    head_plugin="$(plugin_version_text "$(cat "$repo/addon/plugin.cfg")")"
    if [ "$head_plugin" != "$version" ]; then
        echo "addon/plugin.cfg version is $head_plugin, but the next $bump release is $version." >&2
        echo "Update addon/plugin.cfg to version=\"$version\", commit it, then rerun this workflow." >&2
        exit 1
    fi
    emit_plan "$version" "$tag" false "$GITHUB_SHA" ""
}

release_target() {
    local tag="$1"
    gh release view "$tag" --json targetCommitish --jq .targetCommitish
}

release_digest() {
    local tag="$1"
    gh release view "$tag" --json assets --jq ".assets[] | select(.name==\"${ASSET_NAME}\") | .digest"
}

require_release_identity() {
    local tag="$1"
    local expected="$2"
    local target="$3"
    local actual=""
    require_tag "$tag"
    require_sha256 "$expected"
    require_sha "Tag target" "$target"
    actual="$(release_target "$tag")"
    if [ "$actual" != "$target" ]; then
        echo "GitHub release target does not match the immutable tag target." >&2
        exit 1
    fi
}

cmd_recover() {
    local repo=""
    local dist=""
    local tag=""
    local expected=""
    local target=""
    local actual_digest=""
    local stage=""
    local -a files=()
    repo="$(repo_root)"
    dist="${RELEASE_DIST_DIR:-$repo/dist}"
    tag="${TAG:-}"
    expected="${EXPECTED_SHA256:-}"
    target="${TARGET:-}"
    require_release_identity "$tag" "$expected" "$target"
    stage="$(mktemp -d)"
    # RETURN runs before this function's locals disappear, including on set -e.
    trap 'rm -rf "$stage"' RETURN
    gh release download "$tag" --pattern "$ASSET_NAME" --dir "$stage"
    mapfile -t files < <(find "$stage" -type f | sort)
    if [ "${#files[@]}" -ne 1 ] || [ "$(basename "${files[0]}")" != "$ASSET_NAME" ]; then
        echo "Existing release download did not yield exactly $ASSET_NAME." >&2
        exit 1
    fi
    printf '%s  %s\n' "$expected" "$ASSET_NAME" >"$stage/$ASSET_NAME.sha256"
    (cd "$stage" && sha256sum --check --strict "$ASSET_NAME.sha256")
    actual_digest="$(release_digest "$tag")"
    if [ "$actual_digest" != "sha256:$expected" ]; then
        echo "Release asset digest does not match the pinned SHA-256." >&2
        exit 1
    fi
    mkdir -p "$dist"
    cp "$stage/$ASSET_NAME" "$dist/$ASSET_NAME"
    cp "$stage/$ASSET_NAME.sha256" "$dist/$ASSET_NAME.sha256"
    echo "REACHED addon release_recovery assertions=4"
}

cmd_recheck() {
    local tag=""
    local expected=""
    local target=""
    local actual_digest=""
    tag="${TAG:-}"
    expected="${EXPECTED_SHA256:-}"
    target="${TARGET:-}"
    require_release_identity "$tag" "$expected" "$target"
    actual_digest="$(release_digest "$tag")"
    if [ "$actual_digest" != "sha256:$expected" ]; then
        echo "Release asset digest does not match the pinned SHA-256." >&2
        exit 1
    fi
    echo "REACHED addon existing_release assertions=2"
}

cmd_assert_bump() {
    if [ "${RETRY:-}" != "false" ]; then
        echo "Refusing to create a release unless retry=false." >&2
        exit 1
    fi
    if [ -n "${RETRY_TAG:-}" ] || [ -n "${RETRY_SHA256:-}" ]; then
        echo "Refusing to create a release while retry inputs are set." >&2
        exit 1
    fi
    echo "REACHED addon assert_bump"
}

case "${1:-}" in
    plan) cmd_plan ;;
    recover) cmd_recover ;;
    recheck) cmd_recheck ;;
    assert-bump) cmd_assert_bump ;;
    *) usage ;;
esac
