# Isolated security negative control

These files preserve a known vulnerable dependency graph to prove that the
binary scanner detects the linked gRPC advisory GO-2026-6443. They are test
inputs only: no release archive includes this directory and no fixture binary
is published. The production Caddy builder remains under `helpers/`.

`SOURCE.json` binds the four build inputs to their reviewed hashes. The CI
negative-control job verifies those hashes, builds with Go 1.27.0, checks the
linked gRPC version, and requires detection of the expected advisory. Updating
the fixture to remove its vulnerability would invalidate that test.
