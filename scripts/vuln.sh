#!/usr/bin/env bash
set -euo pipefail
for module in sessionauth pgbudget operator; do
  (cd "$module" && GOTOOLCHAIN=local govulncheck ./...)
done
