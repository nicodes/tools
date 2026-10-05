# Unified workflow contract

This directory is the generic source for the version 1 workflow catalogue.
Applications declare their capabilities in `.github/workflow-contract.json` and
fixed operator argv in `.github/workflow-operations.json`. The fleet map belongs
in the docs repository; this library does not know any applications or owners.

The initial adoption keeps cicd v0.9.0 engineering pins. Copy these three files
byte-for-byte to the caller's `.github/` directory and bind their SHA256 hashes in
its manifest. Docs' fleet policy binds the same hashes independently, and Fleet
Audit validates current main without executing downloaded code. Changes to the
library require regenerated copies and an explicit reviewed policy update.

`pins_compat.mjs` provides a read-only schema adapter for v0.9.0's historical
`bun-updates.yml` checker. It validates the unified job's main-only original cron
condition, then exposes the equivalent legacy shape to the unchanged released
checker in memory. It neither writes a legacy workflow nor edits the installed
snapshot. Existing tool, action, snapshot and issue-only update assertions run
unchanged. This bridge can be removed after a reviewed engineering release adds
native support for the consolidated caller schema.

CI checks the manifest on an existing Test runner. Operator workflows call
`workflow_operation.py` only from main; commands remain caller-owned. Stateful
Recovery validates exact main run/artifact provenance, immutable ZIP digest,
bounded receipt/ciphertext contents and encrypted transport. It reports neither
metadata authentication nor successful restore; decryption stays local.

Released reusable workflow paths remain available: `backup.yml`, `dependabot.yml`,
`deployed.yml`, `tools.yml`, `verify.yml`, and `vuln.yml`. Their legacy display names
are explicit API compatibility exceptions. Product entry points use the unified
names while calling those released paths. Do not rename a remote `uses` path at
a fixed release SHA unless that release actually contains the new path.

Templates in `templates/` remain compatible with v0.9.0 and its historical pin
checker. New adoption should apply this catalogue and the schema bridge together,
then run the validator and the repository's full Make gates. Do not merely rename
a template and lose dependency coverage or PR artifact identity.

Regenerate with `python3 workflow-library/sync.py <caller-checkout> ...`;
verify with the same command and `--check`. Update the docs policy separately in
the reviewed adoption PR. This command never discovers fleet members itself.

Tool callers that newly install the engineering artifact also copy
`engineering_bootstrap.py` to `scripts/engineering-bootstrap.py` and declare the
independent release manifest digest in `engineering-pin.json`. Maintenance calls
that verifier before any helper; cached bytes cannot bypass the reviewed inventory.
