"""Whether this tests/ directory is a vendored copy inside a product.

helpers/ and tests/ are vendored wholesale into every product. There, ROOT is
the product's scripts/engineering directory: this repository's workflows,
scripts and docs are simply absent, and a test asserting things about them
fails for a reason the product cannot fix and should not care about.

SOURCE.json beside the tests/ directory is the signal. vendor-snapshot.py
writes it into a consumer and it never exists here.
"""
from pathlib import Path
import unittest

VENDORED = (Path(__file__).resolve().parents[1] / 'SOURCE.json').exists()


def skip_module_if_vendored(reason='cicd-only: asserts things about the cicd repository itself'):
    """Skip the importing module wholesale when it is running inside a product.

    Raised at import time, which unittest discovery reports as a skipped
    module rather than an error -- the distinction between "not our business"
    and "broken".
    """
    if VENDORED:
        raise unittest.SkipTest(reason)
