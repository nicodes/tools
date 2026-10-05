# Credential boundaries

Credentials and operational inventories belong to the caller. This library must
not contain application secret layouts, tenant identities, production domains,
account inventories or live credential values.

Privileged workflow jobs use explicit environments and narrowly scoped grants.
Environment policies and secret provisioning remain caller responsibilities.
Never print credentials or record authenticated browser traces. Synthetic
verification identities must be distinct from human accounts; verify their
metadata and credentials before use, and retire owned sessions after testing.

Backup transport uses the explicit `BACKUP_S3_*` contract. Storage credentials
stay on the calling runner; application hosts receive only encrypted output.
Slugs and storage locations are caller-owned configuration. Do not broaden a
reviewed destination or slug allowlist to make a failed upload pass.

A library action cannot increase the caller's `GITHUB_TOKEN` permissions. Declare
the required permission floor in the caller and keep application secrets scoped
to the intended environment. Caller jobs own their trust and branch policies.
