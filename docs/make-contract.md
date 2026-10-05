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
