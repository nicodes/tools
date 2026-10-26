# Repository instructions

This public repository contains shared Go runtime libraries maintained for our applications.

- Keep `tools` focused on shared application code. Development recipes, installers and deployment automation belong to their owning products.
- Products must install and run without a separate `tools` checkout or configuration. Compile shared libraries into product binaries; Go application authors import only the modules they need.
- Keep product inventories, deployment hostnames and operational incident histories in the private `nicodes/docs` repository.
- Accept product-specific settings through caller-owned inputs or policies. Use synthetic names, reserved example domains and loopback addresses in tests.
- Explain reusable contracts and behavior in source comments. Public references to dependencies and existing configuration interfaces are permitted.
- Keep detailed documentation in `nicodes/docs`, verified against current code. This repository contains its public usage README and necessary agent instructions.
- Preserve consumer compatibility and immutable release checksums when cleaning up public content. Release adoption is a separate, verified change.
