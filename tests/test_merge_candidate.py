import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('merge_candidate', Path(__file__).resolve().parents[1] / 'helpers/merge-candidate.py')
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


class MergeCandidate(unittest.TestCase):
    def setUp(self):
        self.head, self.base, self.merge, self.tree = [letter * 40 for letter in 'abcd']
        self.pull = {'number': 7, 'state': 'open', 'draft': False, 'mergeable': True,
                     'head': {'sha': self.head},
                     'base': {'sha': self.base, 'ref': 'main', 'repo': {'full_name': 'owner/product'}},
                     'merge_commit_sha': self.merge}
        self.commit = {'sha': self.merge, 'tree': {'sha': self.tree},
                       'parents': [{'sha': self.base}, {'sha': self.head}]}
        self.receipt = candidate.snapshot('owner/product', self.pull, self.commit)
        self.proof = {'repository': 'owner/product', 'source_commit': self.merge,
                      'source_tree': self.tree, 'head': self.head, 'base': self.base,
                      'status': 'passed', 'tracked_source_clean': True,
                      'build_verified': True, 'tests_verified': True}

    def test_exact_current_candidate_and_build_test_proof_are_accepted(self):
        candidate.current_candidate(self.receipt, self.pull, self.base)
        candidate.tested_candidate(self.receipt, self.proof)

    def test_head_or_base_advancement_requires_fresh_validation(self):
        for field in ['head', 'base']:
            pull = copy.deepcopy(self.pull)
            pull[field]['sha'] = 'e' * 40
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'fresh validation'):
                candidate.current_candidate(self.receipt, pull, self.base)
        with self.assertRaisesRegex(ValueError, 'fresh validation'):
            candidate.current_candidate(self.receipt, self.pull, 'f' * 40)

    def test_aggregate_cannot_use_another_products_or_an_old_candidates_proof(self):
        for field, value in [('repository', 'other/product'), ('source_commit', self.head),
                             ('source_tree', 'e' * 40), ('head', self.base), ('base', self.head)]:
            proof = {**self.proof, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'another candidate'):
                candidate.tested_candidate(self.receipt, proof)

    def test_test_success_alone_does_not_establish_complete_acceptance(self):
        for field, value in [('status', 'failed'), ('tracked_source_clean', False),
                             ('build_verified', False), ('tests_verified', False),
                             ('build_verified', 'true'), ('tests_verified', 1)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                candidate.tested_candidate(self.receipt, {**self.proof, field: value})

    def test_wrong_candidate_parentage_or_tree_is_refused(self):
        for commit in [{**self.commit, 'parents': [{'sha': self.head}, {'sha': self.base}]},
                       {**self.commit, 'sha': self.head},
                       {**self.commit, 'tree': {'sha': 'main'}}]:
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                candidate.snapshot('owner/product', self.pull, commit)

    def test_closed_draft_or_conflicting_request_is_refused(self):
        for field, value in [('state', 'closed'), ('draft', True), ('mergeable', False), ('mergeable', None)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                candidate.snapshot('owner/product', {**self.pull, field: value}, self.commit)

    def test_target_change_or_candidate_replacement_invalidates_receipt(self):
        for mutate in ['target', 'candidate', 'number']:
            pull = copy.deepcopy(self.pull)
            if mutate == 'target':
                pull['base']['ref'] = 'other'
            elif mutate == 'candidate':
                pull['merge_commit_sha'] = 'e' * 40
            else:
                pull['number'] = 8
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                candidate.current_candidate(self.receipt, pull, self.base)


if __name__ == '__main__':
    unittest.main()
