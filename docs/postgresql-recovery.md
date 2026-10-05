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
as described in [off-host backup PUT](offhost-backup-put.md); app VPS hosts never
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

`make check` includes `make test-postgres`, requiring local Docker and the pinned
PostgreSQL image. The fixture checks actual data and ACL restoration and cleanup.
Unit controls preserve PocketBase/WAL verification and reject PG manifest,
transport, backend-selection and image-identity tampering. Product adoption still
requires a complete reviewed helper snapshot and that product's full gate.
