<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 8112969729af0994759655aca0eccfef92aa8186916835365f5939d1df418948 -->

# tools

Shared Go libraries and reusable CLI release automation.

## Go libraries

The Go modules are:

- `github.com/nicodes/tools/sessionauth`: Clerk session verification.
- `github.com/nicodes/tools/pgbudget`: bounded PostgreSQL pools and query deadlines.

Users of products built with these libraries do not need to clone or configure this repository. Install and use the product directly.

Add the required module to your Go application with `go get`. For library development, install the [komizo release](https://github.com/nicodes/komizo#install) matching `komizo.lock`, then run `komizo check`. Both modules retain vet, race and vulnerability checks.

## CLI releases

Use the reusable workflow to check a Go CLI, build Linux/macOS archives for x86_64/arm64, attest the payload, and publish a new version in the calling repository. Pin both references to the same reviewed full commit:

```yaml
name: Release
on:
  workflow_dispatch:
    inputs:
      bump:
        type: choice
        default: patch
        options: [patch, minor, major]
permissions: {}
concurrency: release-${{ github.repository }}
jobs:
  release:
    uses: nicodes/tools/.github/workflows/cli-release.yml@FULL_40_CHARACTER_COMMIT
    permissions:
      contents: write
      attestations: write
      id-token: write
    with:
      tools-ref: FULL_40_CHARACTER_COMMIT
      binary: example
      main-package: ./cmd/example
      bump: ${{ inputs.bump }}
```

The caller supplies a valid `komizo.json`/`komizo.lock` for its existing verification gate. Optional inputs select a Go module directory, Go version/revision string symbols, a tag prefix, or a directory of archives already built and checked by that gate. Repositories with additional acceptance requirements can retain those jobs and call `cli-publish.yml` with their accepted artifact ID, binary name, tag and pinned tools reference. Explicit `extra-files` select additional release assets.

Publication requires an explicit dispatch from `main`. It refuses existing tags/releases, checks the complete archive matrix and SHA-256 checksums, stages a draft, verifies every uploaded asset digest, and publishes the draft only after the payload matches. Products own their versions, release assets, installer and additional acceptance checks. Installation does not require a tools checkout, a Go compiler or a server connection.
