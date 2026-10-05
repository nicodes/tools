#!/bin/sh
# Run in a disposable directory containing caddy.go.mod as go.mod,
# caddy.go.sum as go.sum and caddy-main.go as main.go.
set -eu
export GOTOOLCHAIN=local
go mod download
go mod vendor
source="$(pwd)/vendor/github.com/caddyserver/caddy/v2/modules/caddyhttp/celmatcher.go"
expected=e0fb48fde80ea23f7810e0e7f3fb2998032a6bea052deedaaeb02881d6c8cc6b
actual="$(sha256sum "$source" | cut -d ' ' -f 1)"
[ "$actual" = "$expected" ] || { echo 'Caddy matcher source differs from reviewed v2.11.4' >&2; exit 1; }
[ "$(grep -c '\[\]interpreter.Interpretable{' "$source")" = 2 ] || exit 1
sed 's/\[\]interpreter.Interpretable{/[]interpreter.InterpretableV2{/g' "$source" > celmatcher.go.overlay
target="$(pwd)/celmatcher.go.overlay"
# These paths only enter a generated Go overlay document. Refuse path syntax
# needing JSON escapes; callers create their disposable workdir under /tmp.
case "$source$target" in *[!a-zA-Z0-9_./@-]*) echo 'Unsupported build path' >&2; exit 1;; esac
printf '{"Replace":{"%s":"%s"}}\n' "$source" "$target" > overlay.json
# An overlay preserves upstream module/version build metadata. A local module
# replacement would hide Caddy's real version from shipped-binary scanners.
go build -mod=vendor -overlay=overlay.json -trimpath -o caddy .
if [ "${1:-}" = --test ]; then
    # Go vendor intentionally omits upstream tests. Run those from a separate
    # full source copy with the same patch and root dependency lock. This local
    # replacement is test-only; the shipped binary above keeps version metadata.
    mkdir test-source
    cp go.mod go.sum test-source/
    cp -R "$(go env GOMODCACHE)/github.com/caddyserver/caddy/v2@v2.11.4" test-source/upstream
    chmod -R u+w test-source/upstream
    cp celmatcher.go.overlay test-source/upstream/modules/caddyhttp/celmatcher.go
    (
        cd test-source
        go mod edit -replace github.com/caddyserver/caddy/v2=./upstream
        go test -mod=readonly -race -count=1 github.com/caddyserver/caddy/v2/modules/caddyhttp
    )
fi
