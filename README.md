<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 9b50c70f6454e156f20cc34246b3e2600f6e7886c0e26394b6862eeeb7ad747b -->

# Tools

Shared Go libraries maintained for our applications:

- `github.com/nicodes/tools/sessionauth`: Clerk session verification.
- `github.com/nicodes/tools/pgbudget`: bounded PostgreSQL pools and query deadlines.

Users of products built with these libraries do not need to clone or configure this repository. Install and use the product directly.

Add the required module to your Go application with `go get`. For library development, install the [komizo release](https://github.com/nicodes/komizo#install) matching `komizo.lock`, then run `komizo check`. Both modules retain vet, race and vulnerability checks.
