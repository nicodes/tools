import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('rollback', Path(__file__).resolve().parents[1]/'helpers/rollback-decision.py')
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


class RollbackProof(unittest.TestCase):
    def test_absent_failed_or_other_revision_proof_never_authorizes(self):
        declaration = {'automatic_image_rollback': True}
        current, previous = 'a'*40, 'b'*40
        proof = {'run_id': '123', 'current_revision': current, 'previous_revision': previous,
                 'status': 'passed', 'snapshot_sha256': 'c'*64,
                 'operations': {name: 'passed' for name in rollback.OPERATIONS}}
        def decide(d, e):
            return rollback.decide(d, e, current, previous, '123')['allowed']
        self.assertTrue(decide(declaration, proof))
        self.assertFalse(decide({}, proof))
        self.assertFalse(decide({'automatic_image_rollback': False}, proof))
        self.assertFalse(decide(declaration, None))
        for key, value in [('current_revision', 'd'*40), ('previous_revision', 'd'*40),
                           ('run_id', '456'), ('status', 'failed'), ('snapshot_sha256', '')]:
            changed = {**proof, key: value}
            self.assertFalse(decide(declaration, changed))
        for operation in rollback.OPERATIONS:
            changed = copy.deepcopy(proof)
            del changed['operations'][operation]
            self.assertFalse(decide(declaration, changed))


if __name__ == '__main__':
    unittest.main()
