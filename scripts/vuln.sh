#!/usr/bin/env bash
set -euo pipefail
for module in sessionauth pgbudget; do
  (cd "$module" && GOTOOLCHAIN=local govulncheck ./...)
done
