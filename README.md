# Reusable engineering tools

Source-only helpers, GitHub Actions and templates for caller-owned projects.
Applications keep their business logic, identities, domains, credentials,
deployment configuration and operational inventories in their own repositories.

## Capabilities

- Make targets and workflow contracts for testing, builds and dependency checks.
- Exact revision, action, dependency and container-image pin verification.
- Reproducible helper archives with an independent source manifest.
- Vulnerability scanning of built images without executing their contents.
- Encrypted backup transport and isolated recovery validation.
- Local authentication fixtures and caller-configured synthetic browser journeys.
- Optional adapters for public deployment and game-development tooling.

See [the Make contract](docs/make-contract.md), [release integrity](docs/releases.md),
[recovery format](docs/postgresql-recovery.md), [credential boundaries](docs/secrets.md),
[caller contracts](docs/workflows.md), [workflow library](workflow-library/README.md) and [templates](templates/README.md).

## Use

Pin an immutable release and its SHA256, verify its independent `SOURCE.json`
digest, and pin reusable workflows to the same full commit. Existing artifact
filenames and `CICD_ENGINEERING` remain compatibility interfaces.

```yaml
jobs:
  verify:
    uses: nicodes/tools/.github/workflows/verify.yml@<full-40-hex-commit>
    permissions:
      contents: read
```

Caller workflows own triggers, environments and permission grants. Grant only
what the selected workflow requires; no shared workflow can elevate caller grants.
Keep privileged jobs on trusted code and provide secrets by explicit names.

## Caller-owned configuration

No application inventory or production endpoints belong here. Fleet comparison
accepts a caller-selected policy file with its reviewed SHA256, or an explicit
policy URL. Action-record verification and updates accept `ACTION_REPOSITORY`
and optional `ACTION_PIN_POLICY` (JSON containing reviewed owners/repositories).
Backup upload requires `BACKUP_ALLOWED_SLUGS`, a JSON array supplied by the caller.
Production browser journeys require explicit origins and a credential adapter;
the generic helper does not select applications or infer their authentication flow.

Public adapter identifiers such as `nicodes/komizo-actions`, `gdam.json`, and
legacy environment/artifact names identify reusable tooling interfaces, not
application inventory. Optional integrations are explicit; applications supply
all values needed to use them.

## Development

```sh
make install
make check
make fleet-audit FLEET=/path/to/caller-owned-policy.json
```

`make check` runs syntax, workflow, unit and process-lifecycle checks. Image and
recovery checks preserve their fail-closed integrity and isolation boundaries.
Operational migration journals and fleet rollout evidence belong in private docs.
