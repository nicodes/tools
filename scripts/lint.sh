#!/usr/bin/env bash
set -euo pipefail
actionlint
shellcheck scripts/*.sh
test -z "$(gofmt -l sessionauth pgbudget)"
