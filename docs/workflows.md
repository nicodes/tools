# Reusable workflow callers

Caller workflows own triggers, branch trust, environments and credentials.
Pin every reusable call to one full tools commit matching the helper snapshot.

## Reusable backup workflow

```yaml
name: Backup
on:
  workflow_dispatch:
permissions:
  contents: read
  issues: write
concurrency:
  group: deploy-production
  queue: max
  cancel-in-progress: false
jobs:
  backup:
    uses: nicodes/tools/.github/workflows/backup.yml@<full-40-hex-commit>
    with:
      product: sampleapp
      allowed-products: '["sampleapp"]'
      server: host.example.test
      user: deploy-user
    secrets:
      KOMIZO_DEPLOY_KEY: ${{ secrets.KOMIZO_DEPLOY_KEY }}
      BACKUP_S3_ACCESS_KEY: ${{ secrets.BACKUP_S3_ACCESS_KEY }}
      BACKUP_S3_SECRET_KEY: ${{ secrets.BACKUP_S3_SECRET_KEY }}
```

The caller configures `KOMIZO_KNOWN_HOSTS`, `BACKUP_S3_HOSTNAME` and
`BACKUP_S3_BUCKET` in the scopes required by the called job. Repo-level entries
and environment entries follow GitHub's separate forwarding rules; explicit
secret mappings make the contract reviewable. `secrets: inherit` forwards only
available names and is not a substitute for provisioning the declared names.
A reusable cannot elevate a missing grant. Under-granted permissions or wrong
secret input names cause a `startup_failure` with zero jobs.

Concurrency is caller-owned. Use `deploy-production`, `queue: max` and
`cancel-in-progress: false` when production changes must serialize. A called job
must not reuse its caller's active concurrency group: that concurrency-group
collision can queue it behind the caller waiting for it to finish.

The caller supplies `scripts/export-backup.sh`; the helper handles encrypted
transport, exact receipt checks and a reviewed slug allowlist. Never pass
unencrypted application data to the upload job.
<!-- ws1: reusable-backup end -->

## Dependency maintenance

```yaml
on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]
concurrency:
  group: dependabot-${{ github.event.pull_request.number }}
  cancel-in-progress: true
permissions:
  contents: write
  pull-requests: write
  actions: write
jobs:
  policy:
    uses: nicodes/tools/.github/workflows/dependabot.yml@<full-40-hex-commit>
    with:
      dispatch-target: ci.yml
```

The trusted-base `dependency-policy.py` admits only bounded updates and
`merge-checked.py` requires successful evidence for the exact PR head before
an atomic SHA merge. Helpers formerly vendored at `scripts/engineering/helpers/`
can be installed from verified archives. Never execute PR-controlled code in a
privileged policy job. Callers select required gates; skipped required gates fail.

<!-- ws13/deployed-reusable: begin -->
## Deployed revision comparison

```yaml
on:
  schedule:
    - cron: '37 * * * *'
  workflow_dispatch:
permissions:
  actions: read
  contents: read
jobs:
  deployed:
    uses: nicodes/tools/.github/workflows/deployed.yml@<full-40-hex-commit>
    with:
      deploy-workflow: cd.yml
```

The caller keeps its cron schedule and the workflow file name. The check compares
main with the latest successful deployment and reports missing commits, refusing
unreadable evidence. Both read permissions are required; an under-granted caller
can fail with zero jobs before a check starts. This workflow accepts no
application credentials and reads no application endpoints.
<!-- ws13/deployed-reusable: end -->

### Caller contract: vulnerability scan

The caller owns `workflow_dispatch` and its scan schedule and configuration.
Call `.github/workflows/vuln.yml` at a full tools commit with `scan-config` naming
its deployed-image manifest. The permission floor is:

```yaml
permissions:
  contents: read
  packages: read
  deployments: read
  issues: write
```

An under-granted caller can fail with zero jobs. The scan reads immutable image
identities and does not execute application binaries.

### Caller contract: tool watch

Call `.github/workflows/tools.yml` at the same reviewed full tools commit.
Concurrency is caller-owned: put the `tool-watch` group on the caller, not on
both the caller and its called job. Under-granted calls can fail with zero jobs.

```yaml
permissions:
  contents: read
  issues: write
```

### What this repository changed

The library owns reusable checks. Triggers, environments, application identities
and operational inventories belong to callers.
