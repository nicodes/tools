"""Exact source, private input hashes and every applicable test fence publication."""
from contextlib import chdir
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / 'helpers/release.py'
spec = importlib.util.spec_from_file_location('native_release_binding', HELPER)
release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)


class NativeReleaseBinding(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'cookbook.toml').write_text('version = 1\n')
        (self.root / '.gitignore').write_text('.artifacts/\n')
        for command in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'source']]:
            subprocess.run(['git', '-C', str(self.root), *command], check=True, capture_output=True)
        self.source = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.context = {'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'example/product',
                        'GITHUB_SHA': self.source, 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                        'GITHUB_JOB': 'validate'}
        self.definition = {'tests': ['unit', 'integration'], 'inputs': {'artifacts': [], 'images': [],
                           'environment': ['PRIVATE_BUILD_INPUT'], 'revision_environment': []}}
        self.environment = patch.dict(os.environ, self.context | {'PRIVATE_BUILD_INPUT': 'private input never serialized'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.cwd = chdir(self.root); self.cwd.__enter__(); self.addCleanup(self.cwd.__exit__, None, None, None)
        self.build = release.build_provenance(self.source, self.definition)
        binding = release.source_binding()
        self.test = {'version': 1, 'source_commit': self.source, 'tested_commit': self.source, **binding,
                     'input_identity': self.build['input_identity'], 'execution_context': self.context,
                     'test_receipts': {name: {'source_commit': self.source, 'input_identity': self.build['input_identity']}
                                       for name in self.definition['tests']}}
        self.directory = self.root / '.artifacts/verification'; self.directory.mkdir(parents=True)
        self.write_test(self.test)

    def write_test(self, value): (self.directory / 'test.json').write_text(json.dumps(value))

    def test_source_configuration_and_private_inputs_are_bound_without_fake_test_claim(self):
        proof = self.build
        self.assertEqual(proof['source_commit'], self.source)
        self.assertEqual(proof['tested_commit'], None)
        self.assertTrue(proof['tracked_source_clean'])
        self.assertIn('cookbook.toml', proof['configuration_sha256'])
        self.assertEqual(len(proof['input_identity']), 64)
        self.assertNotIn('private input never serialized', json.dumps(proof))
        self.assertEqual(release.validate_provenance({'provenance': proof}, self.source), proof)
        with patch.dict(os.environ, {'PRIVATE_BUILD_INPUT': 'different private input'}):
            other = release.build_provenance(self.source, self.definition)
        self.assertNotEqual(proof['input_identity'], other['input_identity'])

    def test_changed_source_tree_config_run_repository_and_dirty_source_refuse(self):
        mutations = [('source_commit', 'a'*40), ('source_tree', 'a'*40), ('tracked_source_clean', False),
                     ('input_identity', ''), ('configuration_sha256', {})]
        for key, changed in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_provenance({'provenance': self.build | {key: changed}}, self.source)
        for key in ['GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_SHA']:
            proof = copy.deepcopy(self.build); proof['build_context'][key] = 'other'
            with self.assertRaises(ValueError): release.validate_provenance({'provenance': proof}, self.source)
        (self.root / 'cookbook.toml').write_text('version = 2\n')
        with self.assertRaises(ValueError): release.validate_provenance({'provenance': self.build}, self.source)

    def test_complete_same_run_test_receipts_are_required(self):
        self.assertEqual(release.validate_test_evidence(self.directory, self.source, 'validate', self.definition), self.test)
        cases = [self.test | {'tested_commit': 'a'*40}, self.test | {'source_tree': 'a'*40},
                 self.test | {'tracked_source_clean': False}, self.test | {'test_receipts': {}},
                 self.test | {'input_identity': ''}]
        for value in cases:
            self.write_test(value)
            with self.assertRaises(ValueError): release.validate_test_evidence(self.directory, self.source, 'validate', self.definition)
        for key, value in [('GITHUB_REPOSITORY', 'foreign/product'), ('GITHUB_RUN_ID', '124'),
                           ('GITHUB_SHA', 'b'*40), ('GITHUB_JOB', 'publish'), ('GITHUB_RUN_ATTEMPT', '3')]:
            self.write_test(self.test | {'execution_context': self.context | {key: value}})
            with self.assertRaises(ValueError): release.validate_test_evidence(self.directory, self.source, 'validate', self.definition)

    def test_missing_applicable_test_or_mismatched_input_receipt_never_establishes_acceptance(self):
        for receipts in [{'unit': self.test['test_receipts']['unit']},
                         self.test['test_receipts'] | {'integration': {'source_commit': self.source, 'input_identity': 'c'*64}}]:
            self.write_test(self.test | {'test_receipts': receipts})
            with self.assertRaises(ValueError): release.validate_test_evidence(self.directory, self.source, 'validate', self.definition)
        with self.assertRaises(ValueError): release.validate_test_evidence(self.directory, self.source, 'validate', self.definition | {'tests': []})

    def test_linked_receipt_or_unmatched_build_proof_is_refused(self):
        path = self.directory / 'test.json'; path.unlink(); path.symlink_to(self.root / 'cookbook.toml')
        with self.assertRaises(ValueError): release.validate_test_evidence(self.directory, self.source, 'validate', self.definition)
        with self.assertRaises(ValueError): release.validate_provenance({}, self.source)

    def test_isolated_file_import_never_loads_caller_modules(self):
        (self.root / 'verification.py').write_text('raise RuntimeError("caller code executed")')
        script = 'import importlib.util,sys; s=importlib.util.spec_from_file_location("checked_release",sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.build_provenance(sys.argv[2], {"inputs":{"artifacts":[],"images":[],"environment":[],"revision_environment":[]}})["source_commit"]==sys.argv[2]'
        subprocess.run([os.sys.executable, '-I', '-B', '-c', script, str(HELPER), self.source], check=True, timeout=10)


if __name__ == '__main__': unittest.main()
