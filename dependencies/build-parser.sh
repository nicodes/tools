#!/usr/bin/env bash
# Compile only pinned action source, with no caller compiler hooks or Cargo config.
set -euo pipefail
stage="$1"
source_revision="$2"
[[ "$stage" = /* && -d "$stage" && ! -L "$stage" ]]
[[ "$source_revision" =~ ^[a-f0-9]{40}$ ]]
source_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$source_root"
pinned_cargo="$(rustup which --toolchain 1.94.0 cargo)"
pinned_rustc="$(rustup which --toolchain 1.94.0 rustc)"
env -i HOME="$HOME" PATH="$(dirname "$pinned_rustc"):/usr/bin:/bin" \
  CARGO_HOME="$stage/cargo" CARGO_TARGET_DIR="$stage/target" RUSTC="$pinned_rustc" \
  VERSION=dev COMMIT="$source_revision" \
  "$pinned_cargo" build --locked --manifest-path "$source_root/Cargo.toml" -p tools-operator
cp "$stage/target/debug/tools-operator" "$stage/tools-operator"
chmod 700 "$stage/tools-operator"
