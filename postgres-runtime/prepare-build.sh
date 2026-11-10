#!/bin/sh
set -eu
inputs=$(CDPATH="" cd -- "$(dirname -- "$0")" && pwd)
mkdir -p /build
test ! -e /build/runtime
test ! -e /build/su-exec
cp -R "$inputs/runtime" /build/runtime
cp -R "$inputs/su-exec" /build/su-exec
cd /build
 export DEBIAN_FRONTEND=noninteractive;
 rm -f /etc/apt/sources.list.d/*;
 printf '%s\n' 'APT::Update::Error-Mode "any";' 'Acquire::AllowInsecureRepositories "false";' 'APT::Get::AllowUnauthenticated "false";' > /etc/apt/apt.conf.d/99-komizo-authenticated;
 for kind in deb deb-src; do
   printf '%s [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian trixie main\n' "$kind";
   printf '%s [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian trixie-updates main\n' "$kind";
   printf '%s [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian-security trixie-security main\n' "$kind";
 done > /etc/apt/sources.list;
 apt-get update;
 apt-get install -y --no-install-recommends ca-certificates;
 sed -i 's|http://|https://|g' /etc/apt/sources.list;
 apt-get update;
 apt-get upgrade -y --with-new-pkgs;
 apt-get install -y --no-install-recommends build-essential bison flex pkg-config python3 curl xz-utils bzip2 locales libssl-dev zlib1g-dev liblz4-dev libzstd-dev gettext texinfo help2man autoconf automake libtool m4 gperf patch
exec python3 -I -B /build/runtime/build.py
