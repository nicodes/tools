The gate images build Caddy 2.11.4 with Go 1.27.0 and the dependency versions in
caddy.go.mod/caddy.go.sum. The official image's Go 1.26.3 binary and dependency
set fail the live shipped-image vulnerability scan.

CEL 0.30.0 fixes GO-2026-6094 but changes NewCall's argument slice to
InterpretableV2. caddy-build.sh adapts the two Caddy matcher call sites using a
Go source overlay over a disposable vendor tree after checking the exact upstream file hash. It preserves
the original Go module identity and version for vulnerability scanning; it
does not mutate the module cache or use a local module replacement.

Validation includes upstream caddyhttp race tests, a scan of the resulting
binary, and each product's real routing/browser gate. The build preserves
the standard Caddy module set. Dependency updates, including the locked
crypto dependencies, require review.

Owner: nicodes. Review by 2026-10-04; retire this compatibility overlay when
an upstream Caddy release supports the fixed CEL dependency. This is a
tested source compatibility repair, not an advisory suppression.

Sources:
- https://github.com/caddyserver/caddy/releases/tag/v2.11.4
- https://pkg.go.dev/vuln/GO-2026-6094
- https://caddyserver.com/docs/build
