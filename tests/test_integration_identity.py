import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('integration_identity', Path(__file__).resolve().parents[1]/'helpers/integration-identity.py')
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


class IntegrationIdentity(unittest.TestCase):
    def test_raw_parents_work_without_a_parent_checkout(self):
        env = {'EXPECTED_HEAD': 'a'*40, 'GITHUB_REPOSITORY': 'example/product',
               'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_JOB': 'test'}
        def git(args):
            return ('f'*40 if args == ['rev-parse', 'HEAD'] else 'd'*40
                    if args[0] == 'rev-parse' else 'tree '+'d'*40+'\nparent '+'c'*40+'\nparent '+'a'*40+'\n\nmessage')
        value = identity.identity(env, git)
        self.assertEqual(value['base'], 'c'*40)
        self.assertEqual(value['head'], env['EXPECTED_HEAD'])
        self.assertEqual(value['commit'], 'f'*40)
        self.assertEqual(value['attempt'], 2)
        for parents in ['', 'parent '+'a'*40, 'parent '+'c'*40+'\nparent '+'b'*40]:
            with self.assertRaises(ValueError):
                identity.identity(env, lambda args: git(args) if args[0] != 'cat-file' else parents)


if __name__ == '__main__':
    unittest.main()
