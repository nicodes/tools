import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('windows_verification_controls', Path(__file__).resolve().parents[1] / 'helpers/verification.py')
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class WindowsEvidencePolicy(unittest.TestCase):
    def policy(self):
        return {'owner': 'S-1-5-21-100', 'current': 'S-1-5-21-100', 'administrator': False,
                'rules': [{'sid': 'S-1-3-4', 'allow': True, 'rights': 0x1f01ff},
                          {'sid': 'S-1-5-32-544', 'allow': True, 'rights': 0x1f01ff}]}

    def test_current_owner_and_platform_administrators_are_allowed(self):
        verification.protected_windows_acl(self.policy())
        policy = self.policy() | {'owner': 'S-1-5-32-544', 'administrator': True}
        verification.protected_windows_acl(policy)

    def test_other_owner_or_writable_users_cannot_produce_protected_evidence(self):
        policy = self.policy() | {'owner': 'S-1-5-21-999'}
        with self.assertRaises(ValueError):
            verification.protected_windows_acl(policy)
        policy = self.policy()
        policy['rules'].append({'sid': 'S-1-5-32-545', 'allow': True, 'rights': 0x2})
        with self.assertRaises(ValueError):
            verification.protected_windows_acl(policy)

    def test_missing_acl_never_counts_as_a_private_directory(self):
        for rules in [None, []]:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                verification.protected_windows_acl(self.policy() | {'rules': rules})


if __name__ == '__main__':
    unittest.main()
