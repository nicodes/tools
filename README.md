# tools

A collection of reusable tools and GitHub Actions. Recipes and their shared execution contract live in [Cookbook](https://github.com/nicodes/cookbook). Application source, domain algorithms, product policy and product assertions stay in their owning repositories.

## Rust libraries

The Cargo workspace contains:

- `httpcontract` under `crates/httpcontract/`: Axum middleware for bounded JSON failures, server-generated request IDs and structured request diagnostics. Add `axum::middleware::from_fn(httpcontract::observe)` to application HTTP routes, inside response compression and before payload signing. Success/streaming bodies and domain metadata remain intact; application authentication, authorization and business error codes remain product-owned. Pre-signed RPC protocols retain their own wire contract. `httpcontract::init_logging()` enables JSON request diagnostics on stderr for applications without a tracing subscriber; applications with an existing subscriber enable the `http_contract` and `httpcontract` targets. It records route patterns and correlation IDs without raw paths, queries, headers or bodies. `httpcontract::failure` constructs an application failure and `httpcontract::health` constructs public status/revision metadata for product-owned liveness and dependency-readiness handlers.
- `sessionauth` under `crates/sessionauth/`: local Clerk session verification using a caller-supplied JWK, authorized-party policy, clock and bounded leeway. Callers own issuer pinning, origin policy, bounded key retrieval and error mapping. Verified raw claims and normalized organization claims remain available.
- `pgbudget` under `crates/pgbudget/`: serving-pool settings, PostgreSQL timeout validation and an operation deadline that retains a tighter parent deadline. Apply all configured fields when constructing your driver pool and wrap the entire operation, including pool acquisition. Query/result semantics and migration credentials remain product-owned.
- `tools-operator` under `crates/operator/`: the protected operator CLI and bounded dependency-metadata parser.

Reference the required package from this repository at a reviewed full commit in your application's Cargo dependencies. Historical Go module tags remain available. Product users install their application's published distribution directly.

For development, install [mise](https://mise.jdx.dev/) and Podman, then run `mise run setup` and `mise run check`. The pinned Rust toolchain builds and tests the libraries and operator; native local Podman supplies owned disposable PostgreSQL recovery fixtures. `mise run test` runs unit and integration checks, `mise run build` builds the workspace, and `mise run clean` removes declared generated outputs. Go remains a contributor tool for the locked upstream Caddy dependency; Tools contains no owned Go entry point for it. CI builds that upstream command directly, retains its dependency lock, runs the upstream race tests, and scans linked symbols. SDK verification includes signed-session security cases, database-budget contracts, metadata ambiguity refusal and private bundle export checks.

## CI helpers

`python3 "$TOOLS_ROOT/helpers/clean.py" --path dist --path .artifacts/build` removes only explicitly named generated outputs from the checkout root. It validates all paths before removal and refuses tracked source, linked paths, traversal and the whole `.artifacts` namespace. Products with a running development session invoke cleanup through their session supervisor.

`python3 "$TOOLS_ROOT/helpers/site-contract.py" dist` checks actual exported HTML for language, title, description, viewport, a main landmark, image alternatives and basic control labels. It complements the product's browser, responsive-layout and performance checks.

Use `nicodes/tools/setup@FULL_40_CHARACTER_COMMIT` after checking out your product. It installs the tool versions in the product's `mise.toml` and exposes the reviewed helper directory as `TOOLS_ROOT`. Canonical recipe Mise tasks select the shared implementation; native projects can call helpers independently. The action supports Linux, macOS and Windows on x86_64 and arm64 and verifies the pinned Mise executable. First-party task execution uses Mise on every supported platform; native external build protocols retain their owning tool requirements.

Check out this repository at a reviewed full commit to use `helpers/integration-identity.py`. Run it from the product checkout with `EXPECTED_HEAD` set to the exact pull request head. It records the actual tested integration commit and raw parents in `.artifacts/fleet-integration.json`, refusing a checkout that does not contain the requested PR head as its second parent. Upload that file from the same job that performs verification. The helper also supports exact `main` delivery identity for push and manual dispatch events.

`nicodes/tools/dependencies@FULL_40_CHARACTER_COMMIT` inspects signed bot updates against the caller's protected `.github/dependency-policy.toml`. Supply a new `output` report path and use `apply: 'true'` only from the trusted policy workflow. It verifies the bot commit, current required checks, integration tree and unchanged base/head before merging. It builds its metadata parser from the pinned action source and never loads the product's tool hooks or PR code.

The recovery helpers accept caller-owned declarations and commands. The snapshot helper packages a product capture; the PostgreSQL helper retains ownership/ACL metadata; restore drills verify restoration in isolated disposable containers. Products supply database/image choices, role and data assertions, resource limits and retention policy. Transport helpers preserve conditioned creation, bounded responses and private credentials. The backup-store capability probe creates a uniquely named synthetic object only when explicitly invoked; it is never part of ordinary setup. Historical activation auditing requires an explicit repository, image components and deployment environment. It authenticates the complete publication inventory and an exact successful ready-operation receipt from the delivery run and attempt. Reports use `activation_at_time` and `current_state = "unobserved"`; they make no claim about current host installation. Declared OS/library image components receive the full image scan alongside applicable Rust or Go runtime checks. Production host backup/restore authority and admission remain in the deployment system.

The snapshot `seal` and `unseal` APIs exchange `snapshot.cms` and `receipt.json`. Format 2 encrypts independent chunks of at most 4 MiB with AES-256-GCM CMS and binds their order, sizes, hashes and complete plaintext digest in an encrypted index. Recovery authenticates the entire envelope before exposing restored files. The ciphertext/export limit is 8 GiB; the separate expanded archive limit is 16 GiB. The S3 uploader uses one conditional PUT and accepts at most 5 GiB (5,368,709,120 bytes), including the complete encrypted envelope. It refuses larger uploads before reading credentials or contacting the store.

Use the optional helpers action at a reviewed full commit to expose TOOLS_ROOT without installing SDKs. The runner supplies Python or other interpreters needed by the chosen helper. The setup action instead installs the caller's native SDK pins.

Reusable dependency maintenance and security scanning live in tools. Security scanning requires a caller-owned source-command; deployed-image inspection is optional and uses the caller's explicit scan declaration. These workflows grant no host or OIDC authority. The optional setup-godot action caches reviewed archives and runs the caller-owned installer and checksum declaration.

## Source and release evidence

The optional `helpers/verification.py` wraps a command chosen by the product. It records the source and input digest, exact build outputs, public workflow identifiers and completed tests in `.artifacts/verification`. It installs no SDKs and defines no tasks. For example:

```sh
python3 "$TOOLS_ROOT/helpers/verification.py" build --artifact dist -- mise run build
python3 "$TOOLS_ROOT/helpers/verification.py" session --test unit -- \
  python3 "$TOOLS_ROOT/helpers/verification.py" test --name unit --artifact dist -- mise run unit
```

Every required test must complete within the same session. Stale receipts, changed source inputs, linked artifacts and conflicting production build variants or revision inputs are refused. Declare relevant environment variable names with `--environment` and revision variable names with `--revision-environment`; their values contribute to the input digest and are never written to receipts. Local edits can be checked, and receipts identify whether tracked source was clean.

Container publishers use `helpers/release.py` to record and publish the exact images checked by their Build job. They own a tracked `deploy/release.toml` artifact definition:

```toml
version = 1
project = "example"
image_base = "ghcr.io/example/example"
tests = ["unit", "integration"]

[inputs]
artifacts = [".artifacts/release"]
images = ["ghcr.io/example/example-api:{revision}"]
environment = []
revision_environment = []
```

Pass `--definition deploy/release.toml` to verification commands instead of individual input flags. The definition contains output selectors and required tests; product task commands and SDK pins stay in native configuration. An optional `stateful_contract` names the product's tracked data compatibility declaration. Publication retains immutable image IDs, archive hashes, clean source and same-run test checks. Stateful declarations alone cannot authorize automatic image rollback. Ordinary library, CLI and static builds need no release definition or deployment configuration.

## Development sessions and optional auth checks

`helpers/dev.py` owns one Git checkout's foreground process tree and authenticated local stop socket. Its Git lease remains held through descendant shutdown and explicit cleanup, even when cleanup removes runtime output. It never discovers shutdown targets by port or command name. For an offline static-site session with parent credentials removed:

```sh
python3 "$TOOLS_ROOT/helpers/dev.py" run --state .artifacts/dev -- \
  ghenv local -- python3 "$TOOLS_ROOT/helpers/static-site.py"
```

Install the optional [ghenv](https://github.com/nicodes/ghenv) CLI for credential isolation. `ghenv local` needs no account or remote credentials; `ghenv dev --config ghenv.toml` applies the product's explicit local or Preview credential policy. Products own setup commands and credential selection. The static-site helper keeps supported Astro development scripts in the foreground and preserves caller arguments and exit status.

Use `dev.py stop --state .artifacts/dev` to stop that checkout. `dev.py clean --state .artifacts/dev -- COMMAND` refuses cleanup while its session is active. A run can supply `--cleanup '["ghenv","local","--","bash","scripts/stop.sh"]'` before `--` to execute caller-owned cleanup after its descendants exit, while retaining the lease. Cleanup commands own their environment and resource selection.

For dependency preparation before credential selection, supply an isolated explicit command with `--prepare '["ghenv","local","--","mise","run","install"]'` and repeat `--policy-input` for the product's native task and credential policy files. The supervisor acquires the checkout lease first and refuses application launch if those files or the source revision change during preparation.

`helpers/auth-boundary.mjs` is an optional source check for products that choose its Clerk environment rules. Import `checkAuthBoundary(root, policy)` from product tests, or run `bun "$TOOLS_ROOT/helpers/auth-boundary.mjs" --policy AUTH_POLICY.toml` with `provider = "clerk"` and `api = true` entries. It reads no deployment configuration. Provider choice, runtime authorization and authentication acceptance tests remain product-owned.

## Protected operator

The optional Rust `tools-operator` CLI manages repository-scoped code merges and source validation through the protected engineering operator. It is separate from ordinary build/test setup and requires a root-owned authority policy before installation. Its releases use the `operator-v` tag prefix and the usual Linux/macOS archive names.

Before running the CLI, verify the archive checksum and GitHub attestation against this repository's `cli-release.yml` builder, binding both the signer and source digest to the exact release revision on `main`. Also require the verified certificate's `buildConfigURI` to name `operator-release.yml@refs/heads/main` and its `buildConfigDigest` to match that revision. Then run:

```sh
sudo ./tools-operator plan
sudo ./tools-operator apply --expected-plan REVIEWED_PLAN_SHA256
sudo ./tools-operator status
```

`recover` restores a prepared installation after interruption. `export --directory NEW_ABSOLUTE_PATH` stages the embedded operator files without installing them. Installation preserves the protected policy, consumed grants and outcome history. Policy contains only `version` and `repositories`; host bindings, owner delivery grants and publication are refused. The operator can request reviewed full source validation or merge the exact accepted PR head. Native publication, deployment and host inspection belong to their separate controllers.

An earlier installation must stop its operator socket/service, preserve private policy, grants and outcome journals, archive and remove the retired publisher/helper code against the previous installed bundle identity, and install a reviewed repository-only policy before applying this source. The retired files are `fleet_publisher.py`, `oci_registry.py`, `release_packet.py`, `oci_archive.py` and `container_runtime.py` under `/usr/local/libexec/fleet/`. Planning refuses while any remains; it neither deletes history nor converts old host authority.

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
      check-command: mise run check
      bump: ${{ inputs.bump }}
```

The caller supplies its own `mise.toml` and required `check-command`. Verification and builds run with those tools; deployment software and deployment configuration are not required. Optional inputs select supported platforms (`linux,darwin` by default), a Go module directory, Go version/revision string symbols, a tag prefix, or a directory of archives already built and checked by that gate. Repositories with additional acceptance requirements can retain those jobs and call `cli-publish.yml` with their accepted artifact ID, binary name, tag and pinned tools reference. Explicit `extra-files` select additional release assets. For coordinated native jobs, `requested-tag` must match the freshly computed release tag. `prepared-artifact-pattern` downloads artifacts from the same run into a bounded `prepared-artifact-directory` under `.artifacts/` before the caller verification command. That command must verify the complete native set and stage `prepared-assets`. The operator release uses this path while retaining the existing `cli-release.yml` signing identity.

Publication requires an explicit dispatch from `main`. It refuses existing tags/releases, checks the complete archive matrix and SHA-256 checksums, stages a draft, verifies every uploaded asset digest, and publishes the draft only after the payload matches. Products own their versions, release assets, installer and additional acceptance checks. Installation does not require a tools checkout, a Go compiler or a server connection.

Website exports can declare product byte limits with `helpers/site-contract.py OUTPUT --budget scripts/site-budget.toml`. The TOML requires `max_total_bytes` and `max_file_bytes` for the complete export, `max_html_gzip_bytes` per page and `max_javascript_gzip_bytes` for all JavaScript. These reproducible byte checks complement browser acceptance; they do not claim network timing.

## Exact installed tools

Native tasks can use `helpers/pinned-tools.py exec --tool python=python3 --tool rust=cargo --tool rust=rustc -- COMMAND ARGUMENTS` to select only the actual SDK executables needed by that stage. The helper reads their exact caller-owned Mise versions, refuses an absent install or an executable outside that install, disables implicit installation and preserves source/execution identity. Package backend options stay in the caller's original Mise configuration. Platform tools that have no declared SDK requirement remain native prerequisites.

For a native project that explicitly depends on Tools, the shared bootstrap renderer emits one literal immutable Tools source entrypoint. Its `[vars]` data is `tools_contract = 1` and one full `tools_ref`. Explicit preparation creates `.artifacts/tools/<full-SHA>`; a supplied `TOOLS_ROOT` is accepted only after the exact repository origin, commit, owning Git root and clean source are verified. Execution refuses a missing source and never fetches it. Bootstrap and SDK Python execution are isolated from caller Python paths. A requested pinned Python bootstrap uses the exact caller version and verifies the actual interpreter before loading SDK code.

Cookbook consumers bind Cookbook independently. Their `COOKBOOK_ROOT` and `.artifacts/cookbook/<full-SHA>` identify recipe code; Cookbook's own reviewed immutable dependency supplies `TOOLS_ROOT`. The two origins are never aliases. A native helper consumer does not become a recipe consumer.

The generic `podman-ci` action prepares the native local Podman engine and compiler caches when invoked. Builds use operation-owned registry authentication and job-local image layers. The caller owns build declarations and resource limits. OCI archives retain exact manifest, configuration and layer identities for scanning and publication without a shared engine socket.

`postgres-runtime/Containerfile` constructs a minimal PostgreSQL 18.6 runtime from authenticated upstream and Debian source with a pinned base, explicit runtime packages and retained package/source inventories. The runtime provides the standard entrypoint, UID/GID 999, the `en_US.utf8` libc locale and the canonical `/run/postgresql` socket directory. Products own their database features, storage declarations, resource policy, image labels, scanning, publication and application assertions.

The constructor is an independently pinned build dependency. From a reviewed Tools checkout, render a consumer Containerfile with the exact Tools Git commit and its independently verified source archive SHA256:

```sh
python3 -I -B postgres-runtime/consumer.py \
  --revision "$TOOLS_BUILD_REF" \
  --archive-sha256 "$TOOLS_ARCHIVE_SHA256" > Containerfile.postgres
```

The generated file verifies that archive before extracting the shared constructor and preserves both source identities as image labels. Add product source/revision labels in the consuming declaration. Products select their runtime/helper dependencies separately; using this constructor does not change those selections.

The generic authenticated image archive implementation checks closed tags, config IDs, RootFS layers and complete archive bytes before deriving a no-op, load or scoped repair. Existing aliases, digests and containers stay protected. Its caller supplies the already-admitted complete image set and retains the single total deadline; no recipe or product policy is discovered.

Source-policy SDK preparation is available through `helpers/source_policy.py setup`; it uses the caller's exact pinned Python and existing hash-locked parser requirements. Read-only checks consume that prepared SDK and refuse stale identity. Recipe declaration selection belongs to Cookbook.

The optional operator source supports a separately reviewed generic `trusted-workflow-request-v1` contract in policy v5. It binds the actual trusted source repository and revision, the exact protected-main request workflow path and hash, current head/base and required CI producer identity before requesting complete validation. Requesting full validation does not itself merge source. Repository-only v3/v4 integration contracts remain supported; the retired Cookbook-specific request protocol requires a new explicit review. These source changes do not update an installed operator, its credentials or protected policy.
