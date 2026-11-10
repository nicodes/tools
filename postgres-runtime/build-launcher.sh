#!/bin/sh
set -eu
inputs=$(CDPATH="" cd -- "$(dirname -- "$0")" && pwd)
cd "$inputs/su-exec/source"
sha256sum --check ../SHA256SUMS
apk add --no-cache gcc musl-dev
mkdir /out
cc -static -Os -Wall -Wextra -Werror -o /out/su-exec su-exec.c
{ echo 'su-exec 0.3'; echo 'source=89c016e6e08749d583efdeda04b9f73e1218e253'; echo 'static=true'; cc --version; apk info -v musl musl-dev gcc; sha256sum su-exec.c account-files.h; } > /out/su-exec.buildinfo
sha256sum /out/su-exec | cut -d ' ' -f 1 > /out/su-exec.sha256
chmod 0755 /out/su-exec
