# Verified Godot setup

Pin this action to a reviewed immutable tools commit. It supports Linux x86_64 stable releases from the official `godot` or `godot-builds` repository. The caller supplies `version`, `binary-checksum` (`sha256:` or `sha512:` plus the reviewed digest), and optionally `install-templates: 'true'` with `templates-checksum`. `expected-version` can preserve a caller's exact official build assertion.

The cache contains archives only. Every hit is checksum verified; corrupt cache entries are replaced only after a verified download. The executable is extracted fresh and checked with `--version`; optional templates respect `XDG_DATA_HOME`. No elevated installation is required. Download and archive validation failures fail the job.

For local use, invoke `python3 helpers/godot-setup.py --version VERSION --binary-checksum ALGORITHM:DIGEST --root PATH`, adding `--templates --templates-checksum ALGORITHM:DIGEST` when needed. Local callers own the install directory and release identities. CI adds the executable directory to `GITHUB_PATH`.
