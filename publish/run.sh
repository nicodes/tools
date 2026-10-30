#!/usr/bin/env bash
# publish/run.sh - invoke the caller's vendored release helper, exactly as the
# products' hand-rolled publish jobs did.
#
# A file rather than a `run:` block so a test can pin the exact argv this
# builds -- that argv is the contract every product kept by copy-paste until
# now, and the one thing this action must not silently change. Everything
# arriving here was validated a step earlier by validate.sh; the helper itself
# re-checks its own artifacts and remains authoritative.
#
# Inputs (environment):
#   PROJECT     project slug (validated)
#   REVISION    full 40-hex commit being released (validated)
#   COMPONENTS  space-separated image components (validated, allowlisted)
#   HELPER      path to the vendored release helper (validated to exist)
set -euo pipefail

echo "Publishing $PROJECT $REVISION:$COMPONENTS through $HELPER"

# The one line each product repository used to carry its own copy of. The
# operation and flags are the helper's own spelling: `publish` records nothing,
# and --components is nargs='+' so the space-separated list becomes one word
# per component. The expansion is unquoted on purpose -- quoting it would pass
# "api db" as a single component -- and is safe because every word passed the
# allowlist in validate.sh before this step could run.
# shellcheck disable=SC2086 # words were allowlisted by validate.sh
namespace=()
if [ -n "${IMAGE_BASE:-}" ]; then namespace=(--image-base "$IMAGE_BASE"); fi
# shellcheck disable=SC2086 # each component passed the slug guard
python3 "$HELPER" publish --project "$PROJECT" "${namespace[@]}" --revision "$REVISION" --components $COMPONENTS
