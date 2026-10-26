<!-- Generated from private documentation source. Do not edit directly. Source SHA256: 5c44714c92ff1913b3bc4ea759301e2a0fac3eb709eaa1cc8daf2b5d4f5fbf2f -->

# Go Libraries

Shared Go libraries:

- `github.com/nicodes/tools/sessionauth`: session verification and authentication request limits.
- `github.com/nicodes/tools/pgbudget`: bounded PostgreSQL pools and query deadlines.

Add the required module to your Go application with `go get`. For library development, install the [Komizo release](https://github.com/nicodes/komizo#install) matching `komizo.lock`, then run `komizo check`. Both modules retain vet, race and vulnerability checks.
