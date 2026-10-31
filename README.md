<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 6c89dc0648bfe52cad3ccb21045e8c478ee3ac4407cc9e7bde9a81c9ad20d856 -->

# tools

Shared Rust libraries, development helpers and reusable CLI release automation.

## Rust libraries

The Cargo workspace contains:

- `sessionauth` under `sessionauth/`: local Clerk session verification using a caller-supplied JWK, authorized-party policy, clock and bounded leeway. Callers own issuer pinning, origin policy, bounded key retrieval and error mapping. Verified raw claims and normalized organization claims remain available.
- `pgbudget` under `pgbudget/`: serving-pool settings, PostgreSQL timeout validation and an operation deadline that retains a tighter parent deadline. Apply all configured fields when constructing your driver pool and wrap the entire operation, including pool acquisition. Query/result semantics and migration credentials remain product-owned.
- `tools-operator` under `operator/`: the protected operator CLI and bounded dependency-metadata parser.

Reference the required package from this repository at a reviewed full commit in your application's Cargo dependencies. Historical Go module tags remain available. Product users install their application's published distribution directly.

For development, install [mise](https://mise.jdx.dev/), then run `mise install` and `mise exec -- make check`. The pinned Rust toolchain builds and tests the libraries and operator. Go remains a contributor tool for the locked upstream Caddy dependency; Tools contains no owned Go entry point for it. CI builds that upstream command directly, retains its dependency lock, runs the upstream race tests, and scans linked symbols. SDK verification includes signed-session security cases, database-budget contracts, metadata ambiguity refusal and private bundle export checks.

## CI helpers

Use `nicodes/tools/setup@FULL_40_CHARACTER_COMMIT` after checking out your product. It installs the tool versions in the product's `mise.toml` and exposes the reviewed helper directory as `TOOLS_ROOT`. Product Makefiles or scripts own the commands to run. The action supports Linux and macOS on x86_64 and arm64; it verifies the pinned mise executable before installation.

Check out this repository at a reviewed full commit to use `helpers/integration-identity.py`. Run it from the product checkout with `EXPECTED_HEAD` set to the exact pull request head. It records the actual tested integration commit and raw parents in `.artifacts/fleet-integration.json`, refusing a checkout that does not contain the requested PR head as its second parent. Upload that file from the same job that performs verification. The helper also supports exact `main` delivery identity for push and manual dispatch events.

`nicodes/tools/dependencies@FULL_40_CHARACTER_COMMIT` inspects signed bot updates against the caller's protected `.github/dependency-policy.json`. Supply a new `output` report path and use `apply: 'true'` only from the trusted policy workflow. It verifies the bot commit, current required checks, integration tree and unchanged base/head before merging. It builds its metadata parser from the pinned action source and never loads the product's tool hooks or PR code.

The recovery helpers accept caller-owned declarations and commands. The snapshot helper packages a product capture; the PostgreSQL helper retains ownership/ACL metadata; restore drills verify restoration in isolated disposable containers. Products supply database/image choices, role and data assertions, resource limits and retention policy. Transport helpers preserve conditioned creation, bounded responses and private credentials. The backup-store capability probe creates a uniquely named synthetic object only when explicitly invoked; it is never part of ordinary setup. Deployed-image scanning requires an explicit repository, image components and deployment environment in its scan declaration. Production host backup/restore authority and admission remain in the deployment system.

Use the optional helpers action at a reviewed full commit to expose TOOLS_ROOT without installing SDKs. The runner supplies Python or other interpreters needed by the chosen helper. The setup action instead installs the caller's native SDK pins.

Reusable dependency maintenance and security scanning live in tools. Security scanning requires a caller-owned source-command; deployed-image inspection is optional and uses the caller's explicit scan declaration. These workflows grant no host or OIDC authority. The optional setup-godot action caches reviewed archives and runs the caller-owned installer and checksum declaration.

## Source and release evidence

The optional `helpers/verification.py` wraps a command chosen by the product. It records the source and input digest, exact build outputs, public workflow identifiers and completed tests in `.artifacts/verification`. It installs no SDKs and defines no tasks. For example:

```sh
python3 "$TOOLS_ROOT/helpers/verification.py" build --artifact dist -- make build
python3 "$TOOLS_ROOT/helpers/verification.py" session --test unit -- \
  python3 "$TOOLS_ROOT/helpers/verification.py" test --name unit --artifact dist -- make unit
```

Every required test must complete within the same session. Stale receipts, changed source inputs, linked artifacts and conflicting production build variants or revision inputs are refused. Declare relevant environment variable names with `--environment` and revision variable names with `--revision-environment`; their values contribute to the input digest and are never written to receipts. Local edits can be checked, and receipts identify whether tracked source was clean.

Container publishers use `helpers/release.py` to record and publish the exact images checked by their Build job. They own a tracked `deploy/release.json` artifact definition:

```json
{
  "version": 1,
  "project": "example",
  "image_base": "ghcr.io/example/example",
  "tests": ["unit", "integration"],
  "inputs": {
    "artifacts": [".artifacts/release"],
    "images": ["ghcr.io/example/example-api:{revision}"],
    "environment": [],
    "revision_environment": []
  }
}
```

Pass `--definition deploy/release.json` to verification commands instead of individual input flags. The definition contains output selectors and required tests; product task commands and SDK pins stay in native configuration. An optional `stateful_contract` names the product's tracked data compatibility declaration. Publication retains immutable image IDs, archive hashes, clean source and same-run test checks. Stateful declarations alone cannot authorize automatic image rollback. Ordinary library, CLI and static builds need no release definition or deployment configuration.

## Development sessions and optional auth checks

`helpers/dev.py` owns one Git checkout's foreground process tree and authenticated local stop socket. Its Git lease remains held through descendant shutdown and explicit cleanup, even when cleanup removes runtime output. It never discovers shutdown targets by port or command name. For an offline static-site session with parent credentials removed:

```sh
python3 "$TOOLS_ROOT/helpers/dev.py" run --state .artifacts/dev -- \
  ghenv local -- python3 "$TOOLS_ROOT/helpers/static-site.py"
```

Install the optional [ghenv](https://github.com/nicodes/ghenv) CLI for credential isolation. `ghenv local` needs no account or remote credentials; `ghenv dev --config ghenv.json` applies the product's explicit local or Preview credential policy. Products own setup commands and credential selection. The static-site helper keeps supported Astro development scripts in the foreground and preserves caller arguments and exit status.

Use `dev.py stop --state .artifacts/dev` to stop that checkout. `dev.py clean --state .artifacts/dev -- COMMAND` refuses cleanup while its session is active. A run can supply `--cleanup '["ghenv","local","--","bash","scripts/stop.sh"]'` before `--` to execute caller-owned cleanup after its descendants exit, while retaining the lease. Cleanup commands own their environment and resource selection.

For dependency preparation before credential selection, supply an isolated explicit command with `--prepare '["ghenv","local","--","mise","run","install"]'` and repeat `--policy-input` for the product's native task and credential policy files. The supervisor acquires the checkout lease first and refuses application launch if those files or the source revision change during preparation.

`helpers/auth-boundary.mjs` is an optional source check for products that choose its Clerk environment rules. Import `checkAuthBoundary(root, policy)` from product tests, or run `bun "$TOOLS_ROOT/helpers/auth-boundary.mjs" --policy AUTH_POLICY.json` with an explicit object such as `{"provider":"clerk","api":true}`. It reads no deployment configuration. Provider choice, runtime authorization and authentication acceptance tests remain product-owned.

## Protected operator

The optional Rust `tools-operator` CLI manages the protected engineering operator. It is separate from ordinary build/test setup and requires a root-owned authority policy before installation. Its releases use the `operator-v` tag prefix and the usual Linux/macOS archive names.

Before running the CLI, verify the archive checksum and GitHub attestation against this repository's `cli-release.yml` builder, binding both the signer and source digest to the exact release revision on `main`. Also require the verified certificate's `buildConfigURI` to name `operator-release.yml@refs/heads/main` and its `buildConfigDigest` to match that revision. Then run:

```sh
sudo ./tools-operator plan
sudo ./tools-operator apply --expected-plan REVIEWED_PLAN_SHA256
sudo ./tools-operator status
```

`recover` restores a prepared installation after interruption. `export --directory NEW_ABSOLUTE_PATH` stages the embedded operator files without installing them. Installation preserves the protected policy and consumed grants. Application hosts independently enforce deployment admission; installing this optional operator does not grant access to a host.

## CLI releases

Use the reusable workflow to check and publish a complete Linux/macOS archive set for x86_64/arm64. Rust products build their archives on native runners and supply the verified set through `prepared-assets`; the source-build Go interface remains available for dependent tooling. Pin both references to the same reviewed full commit:

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
      prepared-assets: dist
      check-command: make check
      bump: ${{ inputs.bump }}
```

The caller supplies its own `mise.toml` and required `check-command`. Verification and builds run with those tools; deployment software and deployment configuration are not required. Optional inputs select supported platforms (`linux,darwin` by default), a Go module directory, Go version/revision string symbols, a tag prefix, or a directory of archives already built and checked by that gate. Repositories with additional acceptance requirements can retain those jobs and call `cli-publish.yml` with their accepted artifact ID, binary name, tag and pinned tools reference. Explicit `extra-files` select additional release assets. For coordinated native jobs, `requested-tag` must match the freshly computed release tag. `prepared-artifact-pattern` downloads artifacts from the same run into a bounded `prepared-artifact-directory` under `.artifacts/` before the caller verification command. That command must verify the complete native set and stage `prepared-assets`. The operator release uses this path while retaining the existing `cli-release.yml` signing identity.

Publication requires an explicit dispatch from `main`. It refuses existing tags/releases, checks the complete archive matrix and SHA-256 checksums, stages a draft, verifies every uploaded asset digest, and publishes the draft only after the payload matches. Products own their versions, release assets, installer and additional acceptance checks. Installation does not require a tools checkout, a Go compiler or a server connection.
