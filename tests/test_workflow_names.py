import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('workflow_names', Path(__file__).parents[1] / 'helpers/workflow-names.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class WorkflowNames(unittest.TestCase):
    def setUp(self):
        self.documents = {
            '.github/workflows/build-and-test.yml': {
                'name': 'Build and test', 'on': {'pull_request': {}},
                'jobs': {'test': {'name': 'Test (${{ matrix.platform }})'}}},
            '.github/workflows/dependency-policy.yml': {
                'name': 'Dependency policy', True: {'workflow_run': {'workflows': ['Build and test']}},
                'jobs': {'auto-merge': {'name': 'Apply dependency policy'}}},
        }

    def test_actual_names_derive_filenames_and_callback_references_with_acronyms_and_dimensions(self):
        module.validate(self.documents)
        self.assertEqual(module.filename('Release addon (API key)'), 'release-addon-api-key.yml')
        self.assertEqual(module.filename('Build and test (Godot)'), 'build-and-test-godot.yml')
        self.assertEqual(module.filename('Ubuntu SDK setup'), 'ubuntu-sdk-setup.yml')

    def test_renamed_workflow_cannot_leave_its_old_filename_or_callback(self):
        documents = copy.deepcopy(self.documents)
        documents['.github/workflows/ci.yml'] = documents.pop('.github/workflows/build-and-test.yml')
        with self.assertRaisesRegex(ValueError, 'filename must match'):
            module.validate(documents)
        self.documents['.github/workflows/dependency-policy.yml'][True]['workflow_run']['workflows'] = ['Build & Test']
        with self.assertRaisesRegex(ValueError, 'unknown workflow'):
            module.validate(self.documents)

    def test_names_and_job_labels_refuse_ampersands_missing_values_and_ambiguous_spacing(self):
        for value in ['Build & Test', '', None, ' Test', 'Test\nagain', 'Test\tagain', 'Test  again']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.title(value, 'fixture')
        self.documents['.github/workflows/build-and-test.yml']['jobs']['test'].pop('name')
        with self.assertRaisesRegex(ValueError, 'job test'):
            module.validate(self.documents)

    def test_callback_requires_literal_known_names_and_nonempty_job_mapping(self):
        for value in ['Build and test', [], [None], ['Missing']]:
            documents = copy.deepcopy(self.documents)
            documents['.github/workflows/dependency-policy.yml'][True]['workflow_run']['workflows'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate(documents)
        self.documents['.github/workflows/build-and-test.yml']['jobs'] = {}
        with self.assertRaisesRegex(ValueError, 'nonempty mapping'):
            module.validate(self.documents)

    def test_executable_steps_require_clear_names_while_reusable_jobs_need_no_steps(self):
        job = self.documents['.github/workflows/build-and-test.yml']['jobs']['test']
        job['steps'] = [{'name': 'Check out source', 'uses': 'actions/checkout@' + 'a' * 40}]
        module.validate(self.documents)
        for value in [[], [None], [{}], [{'name': 'Build & test'}]]:
            job['steps'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'step'):
                module.validate(self.documents)


if __name__ == '__main__':
    unittest.main()
