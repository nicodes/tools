# Repository instructions

This public repository contains shared Go runtime libraries and CLI release automation.

- Keep shared application libraries and generic CLI build/release automation here. Each product owns its release versions, assets, installer and product-specific acceptance. Development recipes and deployment automation remain with their owning products.
- Reusable release workflows run in GitHub Actions. Product users must not need a tools checkout, Go compiler or server connection to install a prebuilt CLI.
- Products must install and run without a separate `tools` checkout or configuration. Compile shared libraries into product binaries; Go application authors import only the modules they need.
- Keep product inventories, deployment hostnames and operational incident histories in the private `nicodes/docs` repository.
- Accept product-specific settings through caller-owned inputs or policies. Use synthetic names, reserved example domains and loopback addresses in tests.
- Explain reusable contracts and behavior in source comments. Public references to dependencies and existing configuration interfaces are permitted.
- Keep detailed documentation in `nicodes/docs`, verified against current code. This repository contains its public usage README and necessary agent instructions.
- Preserve consumer compatibility and immutable release checksums when cleaning up public content. Release adoption is a separate, verified change.
