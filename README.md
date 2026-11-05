<!-- Generated from private documentation source. Do not edit directly. Source SHA256: bed44ece55f420a51c184cc0f9fdadbed0cefc8a6873631e8b0909b9bbfdee27 -->

# tools

A collection of reusable tools, GitHub Actions and recipes. Application source, domain algorithms, product policy and product assertions stay in their owning repositories.

## Rust libraries

The Cargo workspace contains:

- `httpcontract` under `crates/httpcontract/`: Axum middleware for bounded JSON failures, server-generated request IDs and structured request diagnostics. Add `axum::middleware::from_fn(httpcontract::observe)` to application HTTP routes, inside response compression and before payload signing. Success/streaming bodies and domain metadata remain intact; application authentication, authorization and business error codes remain product-owned. Pre-signed RPC protocols retain their own wire contract. `httpcontract::init_logging()` enables JSON request diagnostics on stderr for applications without a tracing subscriber; applications with an existing subscriber enable the `http_contract` and `httpcontract` targets. It records route patterns and correlation IDs without raw paths, queries, headers or bodies. `httpcontract::failure` constructs an application failure and `httpcontract::health` constructs public status/revision metadata for product-owned liveness and dependency-readiness handlers.
- `sessionauth` under `crates/sessionauth/`: local Clerk session verification using a caller-supplied JWK, authorized-party policy, clock and bounded leeway. Callers own issuer pinning, origin policy, bounded key retrieval and error mapping. Verified raw claims and normalized organization claims remain available.
- `pgbudget` under `crates/pgbudget/`: serving-pool settings, PostgreSQL timeout validation and an operation deadline that retains a tighter parent deadline. Apply all configured fields when constructing your driver pool and wrap the entire operation, including pool acquisition. Query/result semantics and migration credentials remain product-owned.
- `tools-operator` under `crates/operator/`: the protected operator CLI and bounded dependency-metadata parser.

Reference the required package from this repository at a reviewed full commit in your application's Cargo dependencies. Historical Go module tags remain available. Product users install their application's published distribution directly.

For development, install [mise](https://mise.jdx.dev/) and Docker, then run `mise install` and `mise run check`. The pinned Rust toolchain builds and tests the libraries and operator; Docker supplies owned disposable PostgreSQL recovery fixtures. `mise run test` runs unit and integration checks, `mise run build` builds the workspace, and `mise run clean` removes declared generated outputs. Make targets invoke the same implementations. Go remains a contributor tool for the locked upstream Caddy dependency; Tools contains no owned Go entry point for it. CI builds that upstream command directly, retains its dependency lock, runs the upstream race tests, and scans linked symbols. SDK verification includes signed-session security cases, database-budget contracts, metadata ambiguity refusal and private bundle export checks.

## CI helpers

`python3 "$TOOLS_ROOT/helpers/clean.py" --path dist --path .artifacts/build` removes only explicitly named generated outputs from the checkout root. It validates all paths before removal and refuses tracked source, linked paths, traversal and the whole `.artifacts` namespace. Products with a running development session invoke cleanup through their session supervisor.

`python3 "$TOOLS_ROOT/helpers/site-contract.py" dist` checks actual exported HTML for language, title, description, viewport, a main landmark, image alternatives and basic control labels. It complements the product's browser, responsive-layout and performance checks.

Use `nicodes/tools/setup@FULL_40_CHARACTER_COMMIT` after checking out your product. It installs the tool versions in the product's `mise.toml` and exposes the reviewed helper directory as `TOOLS_ROOT`. Recipe Makefiles select the shared implementation; native projects can call helpers independently. The action supports Linux, macOS and Windows on x86_64 and arm64 and verifies the pinned Mise executable. Windows setup also installs a checksum-verified GNU Make executable without executing package installation hooks.

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

Website exports can declare product byte limits with `helpers/site-contract.py OUTPUT --budget scripts/site-budget.json`. The JSON requires `max_total_bytes` and `max_file_bytes` for the complete export, `max_html_gzip_bytes` per page and `max_javascript_gzip_bytes` for all JavaScript. These reproducible byte checks complement browser acceptance; they do not claim network timing.


## Cookbook building blocks

The cookbook owns reusable automation implementations. `cookbook/common.mk` supplies the shared Make command interface to native recipe implementations. Its default target displays help; `build` runs compilation/packaging followed by output validation, while `test` runs the implemented lint, unit, integration, E2E and security groups without invoking setup or a product build. Optional development and database commands exist only when a recipe implements them. Recipe adoption is incremental; existing independently pinned interfaces remain available.

The setup action accepts an optional `tools` input containing space-separated tool identifiers, such as `python rust`, to install only those tools at the versions declared by the caller. Leaving it empty preserves installation of all declared tools.

For a closed-manifest addon package:

```sh
python3 "$TOOLS_ROOT/helpers/addon-package.py" build \
  --source addon --manifest package.files --archive dist/addon.zip
python3 "$TOOLS_ROOT/helpers/addon-package.py" verify \
  --manifest package.files --archive dist/addon.zip
```

The manifest contains sorted, unique package-relative filenames. Packaging preserves their bytes and rejects unlisted files, traversal, source/archive symlinks and duplicate archive entries. Build validates the candidate archive before replacing the destination.

`helpers/merge-candidate.py snapshot` records the pull request head, current main base, synthetic merge commit and exact tree. Its `verify` operation requires the same current identities and clean-source build/test proof. It refuses stale, conflicting, closed or draft candidates. The helper validates candidate identity; credentialed merge orchestration separately owns required job completion and merge authority.


`cookbook/generate.py --root PROJECT --revision FULL_COMMIT` generates a small pinned `Makefile` from `cookbook.toml`. Before setup, bare `make` works offline and lists the available interface. `make setup` fetches that exact cookbook source; other commands never fetch it implicitly. CI can check generated content with `--check`. Mise remains the caller's SDK pin authority.

The `godot-addon` adapter accepts a closed package source, manifest and archive; native test entrypoints under the caller's tests; required source paths; and explicit generated cleanup paths. It implements native formatting with the caller-pinned `gdformat`, archive packaging and checksum validation, fresh test-session evidence and tracked-source security checks. Integration and E2E groups require the existing build to match current source inputs and artifact hashes. Test failures invalidate the aggregate success receipt. Product assertions and fixtures stay in the caller's tests.

A `godot-suite` test entry names a native test directory, an exact assertion prefix in `sentinel` and a per-test `timeout`. The shared runner discovers `*_test.gd` files, runs each in an isolated home and requires one exact `<sentinel> <test filename without extension>` line, a clean exit and no engine failure messages. Missing suites, missing/duplicate assertions and exceeded deadlines fail. Direct `godot` entries instead name one test file and its complete sentinel.

Addon ZIP output is deterministic across checkout timestamps. `helpers/addon-package.py checksum --archive ARCHIVE` validates its exact one-line `.sha256` sidecar; `verify --checksums SIDECAR` also checks that sidecar without rewriting it. `helpers/source-secrets.py` scans tracked text for high-confidence credentials and reports fingerprints without printing values. Deliberate credential fixtures require reviewed exact-file hashes and reasons; this source check complements dependency and product security tests.


The reusable `cookbook-validation.yml` workflow runs the selected recipe with `mode: fast` or `mode: full`. Fast validation checks the pinned generated Make entrypoint, source/configuration, documentation history policy and tracked-source credentials, without downloading Godot. Full validation calls `make setup`, `make build` and `make test`, then verifies complete current-run source and artifact evidence. Both the caller source and cookbook use exact commits. Godot addons and React Native applications use the complete host gate. Rust CLIs additionally require their declared native platform matrix. Godot games use separate build, unit shard and auxiliary capability jobs, then join complete current-run evidence before issuing full acceptance.

`cookbook-addon-release.yml` admits a source-bound publication plan, runs full read-only validation, and publishes accepted bytes in a separate job. Recipe data preserves each addon's version convention, job budgets, protected environment, archive roles and any explicitly declared evidence assets. The publisher rechecks source, tree, cookbook, workflow execution, complete build scope and evidence hashes before claiming the immutable tag, verifying uploaded draft assets and publishing. Explicit retries recover checksum-bound existing bytes without rebuilding; same-source resumes require byte equality. API-key and OIDC registry jobs remain separate. `cookbook-addon-recovery.yml` handles an existing tagged release with read-only repository access and OIDC; it cannot rebuild or create a GitHub release. Source checks receive no publication credentials.


Generate recipe entrypoints together with `cookbook/generate.py --root PROJECT --revision FULL_COMMIT --workflows`; CI checks their exact generated contents. Callers contain event bindings, permissions and pinned reusable workflow references. The recipe workflow retains full PR validation until the repository variable `COOKBOOK_REQUEST_MERGE` is set to `enabled` after the main-branch request workflow is verified. It then runs source checks on PR updates, cancels superseded runs, and reserves complete builds/tests for requested merges and final main validation.

Run **Request merge** from `main` with a PR number. The shared controller snapshots the exact head, base, candidate tree and already-accepted cookbook pin. Read-only validation runs the real Make gate. Finalization checks actual job completion and same-run test proof, rechecks the current PR head/base, and asks GitHub to merge with the expected head. It verifies the resulting tree/base before explicitly dispatching CI for the exact final source. Candidate artifacts are not relabeled as the final main commit. The `Full validation` status is specific to the tested PR head. Repository protection and plan availability govern other manual merge paths; the workflow enforces the merges it performs.

Native test entries can use `cwd` and bounded filename `pattern` data. `python-unittest` runs native discovery and refuses zero completed cases; `node-test` expands declared regular test files without shell interpretation. Recipe-specific native runners plug into the same owned-source dispatch. Reviewed Godot engine/template checksum pins may use SHA-256 or SHA-512, preserving their existing exact identities. Native SDK activation disables implicit installation during build/test; setup remains explicit.

The addon adapter prepares locked Node/browser dependencies and hash-locked Python environments only during `make setup`. Package verification compares the closed archive with current source, optional installed files and declared script UID policy. Native Godot tests use bounded execution, isolated user configuration and exact assertion markers or positive assertion counters. Fixture exports run during `make build`; integration and browser tests consume verified outputs without exporting again. Post-test screenshots, installed-tree records and candidate provenance have a separate evidence scope, so they do not change the accepted build identity.

The Rust, React Native and Godot game adapters share SDK, UI, image and packaging helpers. Native platform and Godot shard receipts are partial evidence; only the exact complete declared matrix together with successful host checks can authorize a merge. Platform payloads bind source, cookbook, run, attempt, runner and archive hashes. Product assertions and fixtures stay with their owning components. Application delivery composition calls independently pinned Komizo publication and deployment automation after same-run full validation; source validation receives no delivery secrets. Windows bootstrap selects the reviewed native Mise executable for the actual architecture; it does not substitute a different platform or bypass protected output checks.

`cookbook-cli-identity.yml` selects a typed native CLI release identity. Complete native and host validation precede publication. Generated release callers retain their own attestation step so existing installer signer workflow identities remain valid. Shared publication verifies the closed platform payload and immutable version/tag/source before uploading a draft; no release is published during ordinary CI.

`cookbook/source_policy.py` explicitly prepares a copied Python environment from the hash-locked parser requirements during setup. Source lint uses that prepared environment without installing dependencies and retains Compose, scoped environment and host-only secret assertions. The parser runs in isolation from caller Python paths.

Application delivery keeps the source repository’s authenticated `cd.yml` caller. That caller runs final-source validation once before invoking independently pinned Komizo publication and delivery. Recipe CI omits a duplicate main push build; requested merges dispatch the production caller for the exact final revision. Existing combined delivery jobs retain their original total deadline and release metadata body checks.

Shared compiler caching selects only declared Cargo targets and registry/git dependencies, with keys bound to SDKs, native target, source locks and build options. Successful saves enforce size/file-count bounds and never restore verification or release receipts. Image recipe jobs use a disposable mirror-aware Docker builder with explicit web, layer and Go compiler caches.

## Recipe capabilities and source ownership

The maintained adapters are `rust-cli`, `react-native`, `godot-game` and `godot-addon`. A CLI can include a declared browser UI; an application can include a declared API or database. These remain capabilities of the selected recipe. `cookbook.toml` selects native components, owned assertion programs, fixtures, outputs and CI topology; it is bounded data rather than an additional shell task graph. `mise.toml` supplies SDK pins.

Build immediately stages declared native executable variants before another feature variant can overwrite a shared compiler output. Nonshipping image variants use a separate source- and image-ID-bound fixture packet outside the closed shipping release directory. Browser journeys consume accepted image variants; fixtures are prepared during setup or build. Product programs own loader mutations, data semantics, renderer behavior and assertions.

Godot exports can select explicit debug versus release configurations and caller-owned runtime configuration. Debug-only export features are isolated from release exports. The shared configuration encoder validates bounded JSON data; application hostnames, routes and policy remain caller-owned. Engine/template checksums, compiler/source identities and complete browser/shard coverage stay explicit.

An optional `[operations]` table selects a tracked native declaration and generates pinned Komizo workflow callers. The declaration preserves actual runners, total and step deadlines, protected environments, schedules, native assertions and explicit opt-ins. It does not define application behavior in Tools. Source/deployed audits and artifact validation are separate scopes; an artifact audit declares its setup and build phases explicitly. Deployment, paid checks, backups and restoration retain their independent authorization.

Dependency policy version 2 uses cheap source checks to request the trusted full-validation workflow for an exact bot head and base. It does not treat a cheap check as complete acceptance. The requested merge controller preserves repository protection, verifies the full candidate and separately validates final `main` source. An application’s generated production caller retains its existing authenticated workflow identity.
