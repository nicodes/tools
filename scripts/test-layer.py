#!/usr/bin/env python3
"""Keep process/socket lifecycle checks in the component integration stage."""
import argparse
import unittest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('layer', choices=('unit', 'integration'))
args = parser.parse_args()


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


suite = unittest.defaultTestLoader.discover('tests')
selected = unittest.TestSuite(test for test in cases(suite)
    if (test.__class__.__module__ in ('test_dev', 'test_fixture_proxy')) == (args.layer == 'integration'))
if not selected.countTestCases():
    raise SystemExit('required test layer selected no tests')
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(selected).wasSuccessful())
