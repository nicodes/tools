<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 6bccb407bb774f97860b55fcfcfaba9248c8f5d65a02c72e2c56d2749e17b817 -->

# Reusable engineering tools

## Installed release acceptance

Before publishing an engineering release, CI and the release gate execute the
checksum-bound archive from an isolated installation. Required checks cover
bootstrap, publication handoffs, encrypted backup transport, recovery boundaries,
and real disposable PostgreSQL restoration. Skipped or empty required suites fail.
The published `consumer-check.json` names the exact source, archive and manifest.
Registry and object-storage checks use fixture transports; this evidence does not
establish a production backup or restore.

Tools and deployment-action adoption pushes require a caller-owned
`--rollout-policy` and zero-based `--cohort`. Earlier cohorts must have adopted the
target immutable release and passed their declared fresh workflow acceptance.
The selected repositories must equal the reviewed cohort. Missing, stale, failed,
pending or inaccessible acceptance stops expansion before repository edits.

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

See [the Make contract](#make-contract), [release integrity](https://github.com/nicodes/tools/blob/10b124c2d88d47f978b76cc4546294235d697995/.github/workflows/release.yml),
[recovery format](#postgresql-logical-recovery-contract), [credential boundaries](#caller-owned-configuration),
[caller contracts](#reusable-backup-workflow), [workflow library](https://github.com/nicodes/tools/tree/10b124c2d88d47f978b76cc4546294235d697995/workflow-library) and [templates](#pr-preview-deployments).

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


# Make and Actions contract

Products own `engineering.json`: command argument arrays, declared artifact paths,
image references, build environment keys, authentication and release namespace.
The shared repository contains orchestration and boundary checks, without an
application inventory. Fleet policy is maintained by the caller in its docs repo.

Product Make adapters use grouped targets and require GNU Make 4.3 or newer.
Use a current GNU Make on PATH when the system Make predates grouped targets.
See the [GNU Make 4.3 release notes](https://lists.gnu.org/archive/html/info-gnu/2020-01/msg00004.html).

| Target | Meaning |
| --- | --- |
| install | Pinned tools and locked dependencies; no browser download or tests |
| lint | Static, format, policy and source checks |
| unit | Isolated assertions |
| integration | Real components and disposable data; no browser required |
| test | Unit plus integration |
| build | Production artifacts; no tests or publication |
| artifact-check | Shipping image, routing, security and recovery checks |
| browser-install | Browser tooling only |
| e2e | Essential assembled browser journeys |
| vuln | Online vulnerability checks |
| check | All applicable verification stages |
| dev / stop / clean | Checkout-owned development lifecycle |

Every stage must supply commands or a nonempty `inapplicable` reason. A missing
suite fails validation. `artifact-check` and `e2e` require a current build; E2E
also installs browsers. Multi-target calls share one runner and execute each
stage once. A checkout lock protects shared outputs. Development run/stop use
their own supervisor lock so a running development stack can still be stopped.

Build reuse requires unchanged source bytes and executable bits, relevant
environment hashes, exact artifact hashes and Docker image identities. Failed
builds invalidate evidence. `.artifacts/contract/timings.jsonl` records elapsed
stage time. Source mutation outside declared outputs fails the build.

Use `nicodes/tools/make@<full commit>` with `target: lint test` or
`target: build artifact-check e2e`. The action accepts only canonical verification
targets; callers keep secrets, runners, deployment and application adapters.
`verify.yml` supplies generic Test/Build jobs when no additional product setup
is needed. The composite preserves existing required job names otherwise.

Optional `build-cache: true` restores local BuildKit caches through Actions and
sets up a cache-capable builder. Products explicitly call `docker-build.py`
for components worth caching, usually Go dependencies. Expensive gate compiler
layers may also opt in when measured savings exceed cache transfer. Small scratch
configuration images should not consume remote cache storage. A failed build
preserves its previous layer cache; cache data never substitutes for artifact
validation. See [Docker local caching](https://docs.docker.com/build/cache/backends/local/).

Optional `web-cache: true` restores Expo/Metro transforms. `export-web.py` binds
each namespace to dependency/configuration hashes, the pinned toolchain, output
variant and public/test-auth build environment. It verifies the saved transform
inventory, never restores exported outputs, and still requires a successful full
export and artifact manifest. Release and test authentication cannot share a
namespace. Failed exports leave no success receipt. Dotenv files and all `EXPO_`
flags also enter the identity. Products declare additional compiler modes through
`FLEET_WEB_CACHE_ENV="APP_MODE OTHER_MODE"`; the shared library owns no product
variable names. Declare every custom environment value read by compiler config.

Disposable fixture frontends can use a stable `/api` build address while keeping
fresh random backend ports. `browser.mjs` supplies `proxy(prefix, origin)` to
register an explicit loopback-only HTTP target. Products choose their own routes
and services. The transport forwards method, bytes, authorization and cookies;
it preserves redirects without following them. Actual API/database journeys and
production rejection of fixture authentication remain required. Small uncached
Docker builds should use `docker buildx build --load` so the selected builder
always returns the image used by artifact checks.

During coordinated unmerged PR review, `engineering-pin.json` names an exact
source commit and the bootstrap uses the reviewed release packager to materialize
that commit. It validates the entire helper inventory and hashes on every use.
Promotion replaces this temporary bootstrap with a published, checksum-pinned
mise artifact after the shared PR is merged and released. Existing released
komizo-actions wrappers can read the namespace from `engineering.json.release`;
new callers may pass `--image-base` explicitly. No mutable refs are accepted.

Private fleet policy can be copied to a caller-owned JSON file. The baseline
helper requires its reviewed SHA256 through `FLEET_BASELINE_SHA256`. This keeps
PR checks independent of private repository credentials. A public policy URL
remains supported through explicit `FLEET_BASELINE_URL`.

Optional `go-cache: true` reuses Go's native compiler entries, scoped by OS,
architecture, repository, job, toolchain/module hashes and commit. Test and Build
use separate cache keys so an earlier ordinary build cannot prevent race-compiler
entries from being saved. Go binds compiler entries to source and flags; tests
still use `-count=1`, and no test results or release outputs replace execution.
Current opt-ins contain no application-owned external C libraries. A caller with
external C libraries must bind or invalidate those inputs before opting in: Go's
build cache does not detect changed external C libraries. See the
[Go cache reference](https://pkg.go.dev/cmd/go#hdr-Build_and_test_caching).


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


# PostgreSQL logical recovery contract

Scope: PostgreSQL 18 logical dumps, application-owned immutable files and required
recovery keys. This does not provide WAL archival or point-in-time recovery.
Production resets, deployment and automated production restore schedules are not
enabled by these helpers. The existing operator-only recovery policy still applies.

## Capture is product-owned

The producer must:

1. Fence file reclamation without stopping ordinary application writes.
2. Export one PostgreSQL transaction snapshot and keep its owning transaction alive.
3. Run the pinned `pg_dump --format=custom --snapshot=...` against that snapshot.
   Retain grants/revokes: do not use `--no-acl`. Global roles/passwords are not
   dumped; declare the required application role names instead.
4. Read the referenced file inventory using the same snapshot and copy all required
   originals/checkpoints and recovery keys into private owned staging.
5. Check capture success and continuous ownership of the reclamation fence before
   releasing it. An expired/lost fence invalidates the capture; reacquisition does
   not repair the gap. Do not publish a partial capture.
6. Write `postgresql.json`, archive the staging tree as `data.tar.gz`, validate it
   with `snapshot.restore(..., backend='postgresql')`, and use the existing CMS
   envelope for encrypted off-host transport.

The supplied packaging command can perform step 6 from a completed product
`capture.json` report and separately supplied image metadata:

```sh
python3 helpers/postgres-recovery.py package --capture /private/capture \
  --destination /private/package --metadata /private/images.json
python3 helpers/snapshot.py seal /private/package --recipient /private/recipient.pem
```

After seal, a runner may PUT the ciphertext pair with `helpers/upload-backup.py`
as described in [off-host backup PUT](https://github.com/nicodes/tools/blob/10b124c2d88d47f978b76cc4546294235d697995/helpers/upload-backup.py); app VPS hosts never
receive those object-storage keys.

`capture.json` contains `snapshot_id`, `reclamation_fenced`, `completed_at`,
`database`, `major` and the complete `files` map. `images.json` contains only
`project`, `revision`, `engine`, `roles` and `images` from the manifest below.
Packaging refuses existing/overlapping destinations or changed/unlisted files;
it does not modify the completed source staging or pretend it performed a DB restore.

The central manifest parser checks the capture declaration, not the truth of a
producer's transaction/lease protocol. Product concurrency tests must establish
that separately. A directory copy plus an unrelated dump is not this protocol.

## Version 1 payload

Top-level files are `postgresql.json`, `database.dump` and explicitly inventoried
regular files below `files/` and `secrets/`. Links, traversal, duplicate manifest
keys, undeclared files, changed sizes/hashes and unknown formats are refused.

The manifest has exactly these fields:

```json
{
  "format": "nicodes-postgresql-logical-v1",
  "project": "sampleapp",
  "revision": "<40 lowercase hex characters, or host-local:<docker tag>>",
  "database": "sampleapp",
  "engine": {
    "reference": "postgres@sha256:<64 lowercase hex characters>",
    "image_id": "sha256:<64 lowercase hex characters>",
    "major": 18
  },
  "roles": ["sampleapp_runtime", "sampleapp_worker"],
  "images": {
    "api": {"reference": "ghcr.io/example-org/sampleapp-api:<revision tag>", "image_id": "sha256:<image ID>"},
    "gate": {"reference": "ghcr.io/example-org/sampleapp-gate:<revision tag>", "image_id": "sha256:<image ID>"}
  },
  "capture": {
    "snapshot_id": "<pg_export_snapshot result>",
    "reclamation_fenced": true,
    "completed_at": "<timezone-qualified ISO timestamp>"
  },
  "files": {
    "database.dump": {"size": 123, "sha256": "<content SHA256>"},
    "files/<owned relative path>": {"size": 123, "sha256": "<content SHA256>"},
    "secrets/<owned key filename>": {"size": 123, "sha256": "<content SHA256>"}
  }
}
```

The example illustrates the schema. Project identifiers are caller-owned; role
names must equal the project or use its prefix.
Include source owner roles referenced by default-privilege ACLs, as well as the
application login roles. Each image reference is bound to the project owner's
registry namespace and component, with the sha256 image ID as the pinned
identity. For a release revision every component tag is 40-hex and the API-side
tag equals the revision; the gate may roll on a different 40-hex tag. The API-side
component is recorded under the project's release-pipeline name, `api` or
`service`; both spellings bind to their own component prefix and anchor the
revision identically, and the gate is always required beside exactly one of
them. During the host-local release-tag transition the revision is recorded
truthfully as `host-local:<API-side tag>` and each component binds its own
recorded docker tag.
The engine must use a digest-pinned official PostgreSQL reference. Current live
controls use PostgreSQL 18.6.

## Distinct verification stages

`snapshot.restore` and `snapshot.unseal` retain PocketBase as their default. A PG
payload requires an explicit backend or an authenticated inner `backend` value.
The outer receipt cannot change that value. PG extraction reports:

- `archive_validation: passed`
- `database_restore: not_run`
- `application_restore: not_run`

Valid JSON, hashes and a custom-dump header are **not** proof of a restored DB.
`postgres-recovery.restored_database` performs a real `pg_restore --exit-on-error
--single-transaction --no-owner --no-tablespaces`, retaining ACL statements. It
creates fresh non-superuser application logins and returns private credential-file
paths, never passwords in its report. SQL output and container logs are suppressed.
The context owns a local Docker container with no external network or published
ports, verifies the engine identity/major, and removes only its labeled container
by inspected ID. It never targets an existing database or remote Docker context.

Full product recovery calls `restore-drill.drill(..., application_check=callback,
expected_revision=...)`. The callback is trusted product code, not a command taken
from backup metadata. It must verify API, worker and frontend behavior and clean up
its own resources, returning `passed` for `api_boot`, `worker_boot`,
`frontend_artifact`, `application_checks`, and `application_cleanup`. Without that
callback the full PG drill fails closed. Database-only success is not application
acceptance.

Restoring SQL executes code selected by source superusers. Use trusted backups and
an appropriate isolated restore computer. The container memory/CPU bounds, archive
limits, timeout and disk-headroom preflight are not a sandbox guarantee against
hostile SQL or a formal upper bound on database expansion. Do not run a restore on
a live production host by default.

## Checks

The recovery helper integration checks require local Docker and the pinned
PostgreSQL image. The fixture checks actual data and ACL restoration and cleanup.
Unit controls preserve PocketBase/WAL verification and reject PG manifest,
transport, backend-selection and image-identity tampering. Product adoption still
requires a complete reviewed helper snapshot and that product's full gate.


# Repository envelope templates

Owner: nicodes. Copy the selected archetype's `Makefile`, `.mise.toml` and
`.gitignore` into the product root and `ci.yml` into `.github/workflows/ci.yml`. Copy `dependabot.yml` into
`.github/dependabot.yml`, adding an explicit entry for every extra Go module.
These are envelope templates, not complete applications. Missing product scripts
fail the gate; never replace them with successful placeholders. Callers supply and verify the application-specific integrations.

`full-stack` uses `app/`, `api/`, `pb/`, `deploy/` and `docs/`; embedded and separate
PocketBase are both supported. `app-only` uses `app/`, `deploy/` and `docs/` and has
no API or database scaffold. Its Go tooling builds and scans Caddy, not a new
backend service. Extend exact tool pins when a product needs additional tools.

`static-web` is the static site envelope (Astro on bun in the current adopters,
hosted by Vercel, which deploys from its own build). Copy `ci.yml` into
`.github/workflows/ci.yml`, `dependabot.yml` into `.github/dependabot.yml`,
`.mise.toml`, `biome.json` and `.gitignore` into the root, and `actions/build/action.yml` and
`actions/test/action.yml` into `.github/actions/` — the workflow's jobs call
these repo-local composite actions, so they are part of the archetype, not
optional extras. This archetype omits the `Makefile` and `bun-updates.yml` on
purpose: none of the three adopting repositories has either. Their entire gate
is the two composite actions, and a second entry point would only drift from
it; the bun pin lives in `.mise.toml` and moves with the product.

## PR preview deployments

`pr-preview.yml` is the per-PR preview template. Previews are opt-in: a
trusted `/preview` comment brings a live preview stack up for the pull
request through the `nicodes/komizo-actions/preview` composite, pushes keep
it current while the PR carries the `preview` label, `/preview down` or
closing the PR tears it down, and one sticky PR comment carries the URLs. It
is a documented template, not an archetype — copy it into
`.github/workflows/pr-preview.yml` and finish the product-owned parts.

The rule, decided by the secret-free `request` job
(`nicodes/komizo-actions/preview-request`) that every privileged job gates on:

| Event | Result |
| --- | --- |
| `pull_request` `closed` | tear down, always (idempotent; a lost label must not strand a stack) |
| `pull_request` `synchronize`, `reopened` | redeploy only while the PR carries the `preview` label |
| `pull_request` `opened`, `ready_for_review` | nothing |
| `issue_comment` `created`, exact body `/preview` | deploy, then add the `preview` label |
| `issue_comment` `created`, exact body `/preview down` | tear down, then remove the label |

Comments count only from an OWNER, MEMBER or COLLABORATOR, and only on pull
requests. Every deploy also requires a same-repository head (never a fork), a
trusted author, an open non-draft PR; dependabot PRs are refused both ways.

Adopting it:

- **What to copy.** `pr-preview.yml` verbatim, then replace the two
  product-owned values in BOTH privileged jobs, identically: `APP` (the
  deployment app slug) and `COMPONENTS` (the space-separated image components
  whose refs the preview deploys, named
  `ghcr.io/<owner>/<project>-<component>:<head-sha>`). Do not repin the
  composites on copy: the template already ships the real
  v0.0.33 pin (`398fe9d90aa68daadf6569cb7679df4478218a37`, the release's
  peeled commit SHA, never the annotated tag object) for `preview-request`
  and `preview` alike. Future composite updates move through the product's
  fleet pin record, `ACTION-PINS.json` at the repository root, as usual.
- **Create the label once.** `gh label create preview` in the adopting
  repository. The workflow adds and removes the label under
  `pull-requests: write` but never creates it: that would need
  `issues: write`, which no job holds. Without the label the up job fails
  after its sticky comment with a clear error; the down path warns and
  continues.
- **Secrets and vars to set.** Exactly the deploy composite's SSH path, as
  repo-level entries: the `KOMIZO_DEPLOY_KEY` secret and the
  `KOMIZO_SERVER_URL` and `KOMIZO_KNOWN_HOSTS` variables. The workflow names
  no other product-owned secret: the sticky comment and the label use the
  workflow's own `GITHUB_TOKEN`, and the jobs' permissions floor is
  `contents: read` plus `pull-requests: write`. Image builds and their
  `packages: write` push stay in the product's own CI; this workflow only
  derives the refs CI already published for the PR's head SHA and waits (at
  most 25 minutes) for them to appear.
- **Use the resolved identity.** Both privileged jobs read `pr` and `sha`
  from the request job's outputs, never `github.event.pull_request.*`: a
  comment-triggered run carries no pull request payload. The only
  `github.event.pull_request` expression left is the concurrency key,
  `preview-${{ github.event.pull_request.number || github.event.issue.number }}`.
- **Per-preview runtime secrets are product-owned, and they need the host.**
  The template deploys a preview; it does not give that preview its Clerk
  instance, its allowed origins or anything else the product's server reads
  at boot. A product that needs those adds its own step before `Preview up`,
  writing them over ssh into `doas /usr/local/bin/write-preview-stackenv
  <app> <pr>` — the caller-provisioned wrapper installed on the host, which owns the
  path, the 0750 root state directory and the 0600 file. Two things have to
  be true before that step can work, and neither is visible from the
  repository: the deploy user needs a doas rule permitting that wrapper, and
  the repo needs the development Clerk entries (`CLERK_SECRET_KEY_DEV` as a
  secret, `CLERK_PUBLISHABLE_KEY_DEV` as a variable) on top of the three the
  deploy path already requires. Without the doas rule the step fails with
  `doas: Operation not permitted`, which says nothing about what is missing.
- **The registry wiring is required — do not delete it on copy.** Since
  v0.0.21 the up invocation must pass `registry-user: ${{ github.actor }}`
  and `registry-token: ${{ secrets.GITHUB_TOKEN }}`, and the preview-up job
  must grant `packages: read`: `up` pulls the PR's images AS ROOT on the
  host through the doas wrapper, and root's docker config carries no ghcr
  authorization — without a login the composite fails closed before touching
  the host. The run-scoped `GITHUB_TOKEN` is the credential on purpose
  (never a long-lived PAT); it travels on stdin to the host's root-owned
  preview wrapper, which drops it however the run exits. The teardown job
  needs neither the inputs nor `packages: read` — `down` pulls nothing.
- **The request gate is non-negotiable.** Every job that touches secrets or
  the preview infrastructure carries the job-level
  `if: needs.request.outputs.enabled == 'true' && needs.request.outputs.action == 'up'`
  (or `== 'down'`). The trigger is `pull_request` plus `issue_comment`, never
  `pull_request_target`: the preview runs the PR's own code, so the gate —
  not the trigger — is the security control, and the same-repo, draft,
  dependabot and trusted-author checks live in the SHA-pinned, unit-tested
  composite rather than in per-job expressions (which could not see a
  comment event's PR anyway). Never weaken, move to step level, or delete it.
  The `types:` list on `pull_request` stays complete: the fleet workflow
  contract requires every lifecycle event, and `issue_comment` is an
  additional trigger it ignores.
- **Fork PRs skip everything.** A pull request from a fork is refused by the
  request job: no checkout of untrusted code next to secrets, no
  `KOMIZO_DEPLOY_KEY` in scope, no preview stack created or torn down. Both
  privileged jobs skip, silently, on every event type including `closed`.
- **The one-sticky-comment invariant.** Exactly one preview comment per PR,
  marked `<!-- preview -->`. The first successful deploy creates it; every
  redeploy updates that same comment in place with the fresh URL, API URL,
  head SHA and health-gate status; the teardown path makes a final update
  marking it torn down, and creates nothing if no deploy ever succeeded.
- **The label is the signal, written after the fact.** `preview` is added
  only after the composite verified the stack up (and after the comment with
  the URL landed), and removed only after it verified the teardown. A label
  left behind by host-side garbage collection costs one wasted redeploy on
  the next push, which recreates the state — accepted and cheaper than
  asking the host from the decision job.
- **Concurrency.** Both privileged jobs share the per-PR group
  `preview-<number>`. A new push cancels the in-flight deploy
  (`cancel-in-progress: true`); the teardown declares
  `cancel-in-progress: false` in the same group, so a close that lands
  mid-deploy queues behind it and tears down the finished stack, and the
  teardown itself is never superseded. The request job is never queued.

## Fleet tool baseline

Every archetype pins its shared tools exactly, as `x.y.z`, in its
`.mise.toml`:

| Tool | Pin | Archetypes |
| --- | --- | --- |
| go | 1.27.0 | full-stack, app-only |
| golang.org/x/vuln/cmd/govulncheck | 1.7.0 | full-stack, app-only |
| bun | 1.4.1 | all three |
| node | 24.18.1 | full-stack, app-only |
| python | 3.13.11 | full-stack, app-only |

The baseline is per archetype, by need: `static-web` carries only bun because
its gate is the two composite actions and its CI runs no Go or Python.
Product-specific pins — Godot versions, extra node runtimes, deployment tooling —
are product choices and live in the product's own `.mise.toml`, out of scope
for this baseline.

The rule is exact `x.y.z` pins, so one version is in force everywhere a
repository runs. The single relaxation is mise's two-component core form
(`python = "3.12"`, which floats the patch release): the fleet watcher
normalizes it to a `x.y.0` comparison core rather than rejecting it, so it is
tolerated where a product already uses it, but new pins are always exact
three-component versions.

## Canonical lint and format: Biome

The web repositories share one lint+format configuration:
`static-web/biome.json` is the canonical artifact and is copied verbatim into
each adopting repository's root. The engine is `@biomejs/biome` at exactly
`2.5.14`, installed as a devDependency in the product's `package.json`
(the archetype ships no `package.json` of its own — the pin belongs to the
product's manifest and lockfile, under the usual frozen-install gate). Biome
is installed through the bun channel like every other JS dependency; the
archetype's Test gate then runs `bun x biome check`, which fails the build on
any lint or format violation against the canonical configuration. The
configuration's `$schema` URL records the same version, so the pin, the
schema and the gate move together in one commit when the fleet upgrades.

`ci.yml` and `actions/build/action.yml` are admitted copies, identical across
adopters. `actions/test/action.yml` is the per-repo override point: the
archetype ships the two common assertions (the build wrote `dist/index.html`;
no JavaScript was emitted) and the product adds its own assertions — routes
that must exist, content that must be present — as further steps in that file.
The push trigger on `ci.yml` is load-bearing here: there is no CD, and the
host's build never runs the assertions, so this workflow is the only thing
that gates a merge to main.

Install the engineering snapshot rather than vendoring it: `.mise.toml` pins one
version and its sha256, `scripts/engineering-env.sh` resolves it into
`CICD_ENGINEERING`, and nothing is copied into the product. Keep the normative
policy in the September workspace plan. Use these product-owned scripts:

| Script | Required behavior |
| --- | --- |
| `scripts/test.sh` | Frozen Bun install, pin checks, Actionlint, ShellCheck, helper tests, Expo compatibility, TypeScript, lint, unit tests and product contracts. For every applicable Go module: formatting, vet and full integration/unit tests with `-race -count=1`; production-equivalent database fixtures. |
| `scripts/build.sh` | Exact-head web export and runtime image build; scan actual binaries and record the checked image archive with `release.py`. |
| `scripts/vuln.sh` | Online source dependency scans, fail closed on scanner failure and applicable vulnerabilities. |
| `scripts/e2e.sh` | Real Playwright core journey against built artifacts. Full-stack uses a signed isolated issuer, real API and disposable database, rejects fixture auth in production images, and exercises the real-image restore helper. |
| `scripts/dev-stack.sh` or `scripts/dev-app.sh` | Select nonconflicting ports, start the product processes in the owning supervisor and preserve local mutable state. Full-stack accepts API, DB and app preferred ports; app-only accepts the app port. |

Bare `make` is help. `make check` includes test, vulnerability checks, build and
browser E2E; build is an E2E prerequisite so direct `make e2e` is also complete.
`make stop` uses the checkout's authenticated supervisor, never port-based killing.
`make clean` removes generated output and preserves local databases.

CI jobs are exactly Test and Build and run independently. Test runs `make test
vuln`; Build runs `make install e2e`, including build and browser checks. Both jobs
are required before merge. Templates reference reviewed action SHAs, use exact
mise tools and contain no production secrets or deployment privileges.

App-only CI runs on main for deploy-ready applications. When
adding reviewed CD, remove that redundant main trigger and rerun both gates on
the merged SHA in CD. Full-stack requires such product-owned CD before adoption.
Keep service/image lists, secret names, health URLs, backup exporters, production
synthetic identities, migration compatibility and deploy order in the product.
Use the checked release archive without rebuilding. Do not copy a generic
privileged deploy workflow.

Select the template matching the caller's build and deployment layout.

Container publication requires the actual test receipt for the exact source.
The shared `make` action retains `test.json` with its engineering job artifacts.
Download the current run's test-job artifacts into `.artifacts/test-evidence`
before publishing. The default test job ID is `Test`; callers with another ID
pass `--test-job` (for example `--test-job test` for the shipped templates).
Artifact selection uses `engineering-<job-id>-*`, rather than the display name.
For tools v0.14 and later, `scripts/fanout.py` adds this download and explicit job
binding during adoption, and rejects absent or ambiguous test prerequisites.
Local publishers use the receipt from `make test` or `make check`
in `.artifacts/contract`. Build and test input digests are recorded separately;
a different PR merge checkout cannot certify a head artifact.

PR-head publication retains the workflow's merge identity separately. The head
must match the runner's pull-request event, and successful tests must actually
run on that head. The adoption helper adds those tests to a preview-producing
Build job and uses its own receipt; the existing merge Test job remains intact.
This adds test execution for the second source rather than claiming a merge
test certified an independently built head image.
