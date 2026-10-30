<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 950c9046bb602862584bec73043799dd4dde799342873fad22e1ab1468fe63cb -->

# tools

Shared Go libraries, development helpers and reusable CLI release automation.

## Go libraries

The Go modules are:

- `github.com/nicodes/tools/sessionauth`: Clerk session verification.
- `github.com/nicodes/tools/pgbudget`: bounded PostgreSQL pools and query deadlines.

Users of products built with these libraries do not need to clone or configure this repository. Install and use the product directly.

Add the required module to your Go application with `go get`. For library development, install [mise](https://mise.jdx.dev/), then run `mise install` and `mise exec -- make check`. Tool versions live in `mise.toml`; the Makefile owns verification. Both modules retain vet, race and vulnerability checks.

## CI helpers

Use `nicodes/tools/setup@FULL_40_CHARACTER_COMMIT` after checking out your product. It installs the tool versions in the product's `mise.toml` and exposes the reviewed helper directory as `TOOLS_ROOT`. Product Makefiles or scripts own the commands to run. The action supports Linux and macOS on x86_64 and arm64; it verifies the pinned mise executable before installation.

Check out this repository at a reviewed full commit to use `helpers/integration-identity.py`. Run it from the product checkout with `EXPECTED_HEAD` set to the exact pull request head. It records the actual tested integration commit and raw parents in `.artifacts/fleet-integration.json`, refusing a checkout that does not contain the requested PR head as its second parent. Upload that file from the same job that performs verification. The helper also supports exact `main` delivery identity for push and manual dispatch events.

`nicodes/tools/dependencies@FULL_40_CHARACTER_COMMIT` inspects signed bot updates against the caller's protected `.github/dependency-policy.json`. Supply a new `output` report path and use `apply: 'true'` only from the trusted policy workflow. It verifies the bot commit, current required checks, integration tree and unchanged base/head before merging. It builds its metadata parser from the pinned action source and never loads the product's tool hooks or PR code.

## Protected operator

The optional `tools-operator` CLI manages the protected engineering operator. It is separate from ordinary build/test setup and requires a root-owned authority policy before installation. Its releases use the `operator-v` tag prefix and the usual Linux/macOS archive names.

After verifying a release archive's checksums and GitHub build attestation against this repository's `operator-release.yml` workflow and exact source revision, run:

```sh
sudo ./tools-operator plan
sudo ./tools-operator apply --expected-plan REVIEWED_PLAN_SHA256
sudo ./tools-operator status
```

`recover` restores a prepared installation after interruption. `export --directory NEW_ABSOLUTE_PATH` stages the embedded operator files without installing them. Installation preserves the protected policy and consumed grants. Application hosts independently enforce deployment admission; installing this optional operator does not grant access to a host.

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
      check-command: make check
      bump: ${{ inputs.bump }}
```

The caller supplies its own `mise.toml` and required `check-command`. Verification and builds run with those tools; deployment software and deployment configuration are not required. Optional inputs select supported platforms (`linux,darwin` by default), a Go module directory, Go version/revision string symbols, a tag prefix, or a directory of archives already built and checked by that gate. Repositories with additional acceptance requirements can retain those jobs and call `cli-publish.yml` with their accepted artifact ID, binary name, tag and pinned tools reference. Explicit `extra-files` select additional release assets.

Publication requires an explicit dispatch from `main`. It refuses existing tags/releases, checks the complete archive matrix and SHA-256 checksums, stages a draft, verifies every uploaded asset digest, and publishes the draft only after the payload matches. Products own their versions, release assets, installer and additional acceptance checks. Installation does not require a tools checkout, a Go compiler or a server connection.
