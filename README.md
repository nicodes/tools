<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 70a2eba8af2d40765ac6b22c4b058c1676516316ad17fd3b0f6b1b985ffb5ff9 -->

# Tools

Shared Go libraries maintained for our applications:

- `github.com/nicodes/tools/sessionauth`: Clerk session verification.
- `github.com/nicodes/tools/pgbudget`: bounded PostgreSQL pools and query deadlines.

Users of products built with these libraries do not need to clone or configure this repository. Install and use the product directly.

Add the required module to your Go application with `go get`. For library development, install the [Komizo release](https://github.com/nicodes/komizo#install) matching `komizo.lock`, then run `komizo check`. Both modules retain vet, race and vulnerability checks.
