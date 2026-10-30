#!/usr/bin/env bash
set -euo pipefail
actionlint
shellcheck scripts/*.sh
shellcheck -s sh helpers/*.sh
test -z "$(gofmt -l sessionauth pgbudget operator)"
