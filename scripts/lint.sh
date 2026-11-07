#!/usr/bin/env bash
set -euo pipefail
actionlint
python3 helpers/source_policy.py actions
shellcheck scripts/*.sh publish/*.sh tests/*.sh dependencies/*.sh
shellcheck helpers/*.sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
