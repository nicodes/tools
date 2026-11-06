import importlib.util
import hashlib
import os
from pathlib import Path
import unittest
import sys
import tomllib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
spec = importlib.util.spec_from_file_location('cookbook_request_merge', Path(__file__).resolve().parents[1] / 'cookbook/request_merge.py')
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


class RequestedMerge(unittest.TestCase):
    def setUp(self):
        self.environment = {'GITHUB_REPOSITORY': 'owner/project', 'GITHUB_RUN_ID': '123',
                            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REF': 'refs/heads/main',
                            'GITHUB_EVENT_NAME': 'workflow_dispatch',
                            'GITHUB_WORKFLOW_REF': 'owner/project/.github/workflows/request-merge.yml@refs/heads/main'}
        self.receipt = {'version': 1, 'repository': 'owner/project', 'pull_request': 7,
                        'head': 'b' * 40, 'base': 'a' * 40, 'candidate': 'c' * 40,
                        'tree': 'd' * 40, 'cookbook_commit': 'e' * 40,
                        'test_groups': ['lint', 'security', 'unit']}
        self.receipt.update(recipe='godot-addon', configuration_sha256='1' * 64,
            declaration={'version': 1, 'recipe': 'godot-addon', 'tests': {'unit': [{'path': 'tests/unit.py'}]}})
        self.proof = {'repository': 'owner/project', 'source_commit': 'c' * 40,
                      'source_tree': 'd' * 40, 'head': 'b' * 40, 'base': 'a' * 40,
                      'cookbook_commit': 'e' * 40, 'test_groups': self.receipt['test_groups'],
                      'status': 'passed', 'tracked_source_clean': True,
                      'build_verified': True, 'tests_verified': True, 'execution_context': self.environment}
        self.proof.update(recipe='godot-addon', configuration_sha256='1' * 64)
        self.run = {'id': 123, 'run_attempt': 1, 'event': 'workflow_dispatch',
                    'head_branch': 'main', 'path': '.github/workflows/request-merge.yml',
                    'repository': {'full_name': 'owner/project'}}
        self.jobs = {'total_count': 3, 'jobs': [
            {'name': 'Request / Snapshot current candidate', 'conclusion': 'success'},
            {'name': 'Request / Validate candidate / Make full', 'conclusion': 'success'},
            {'name': 'Request / Merge validated candidate', 'conclusion': None}]}
        self.pull = {'number': 7, 'state': 'open', 'draft': False, 'mergeable': True,
                     'head': {'sha': 'b' * 40}, 'base': {'sha': 'a' * 40, 'ref': 'main',
                     'repo': {'full_name': 'owner/project'}}, 'merge_commit_sha': 'c' * 40}
        self.commit = {'tree': {'sha': 'd' * 40}, 'parents': [{'sha': 'a' * 40}]}
        self.base = 'a' * 40
        self.calls = []

    def api(self, path, method='GET', data=None):
        self.calls.append((path, method, data))
        if method == 'PUT':
            return {'merged': True, 'sha': 'f' * 40}
        if method == 'POST':
            return None
        if '/jobs?' in path:
            return self.jobs
        if '/actions/runs/' in path:
            return self.run
        if '/pulls/' in path:
            return self.pull
        if '/branches/main' in path:
            return {'commit': {'sha': self.base}}
        if '/git/commits/' in path:
            return self.commit
        raise AssertionError(path)

    def finish(self, result='success'):
        with patch.dict(os.environ, self.environment), patch.object(merge, 'api', side_effect=self.api):
            return merge.finish(self.receipt, self.proof, result)

    def test_success_merges_expected_head_and_requests_exact_final_source_validation(self):
        accepted = self.finish()
        mutation = next(data for _, method, data in self.calls if method == 'PUT')
        self.assertEqual(mutation, {'sha': 'b' * 40, 'merge_method': 'squash'})
        dispatch = next(data for path, _, data in self.calls if path.endswith('/dispatches'))
        self.assertEqual(dispatch['inputs'], {'source': 'f' * 40, 'tools': 'e' * 40})
        self.assertEqual(accepted['final_source_ci'], 'requested')
        self.assertFalse(accepted['candidate_artifacts_promoted'])

    def test_changed_head_or_base_never_requests_a_merge(self):
        for changed in ['head', 'base']:
            with self.subTest(changed=changed):
                self.calls.clear()
                if changed == 'head':
                    self.pull['head']['sha'] = '9' * 40
                else:
                    self.pull['head']['sha'] = 'b' * 40
                    self.base = '9' * 40
                with self.assertRaises(ValueError):
                    self.finish()
                self.assertFalse(any(method == 'PUT' for _, method, _ in self.calls))

    def test_skipped_or_cancelled_validation_cannot_be_success(self):
        for result in ['skipped', 'cancelled', 'failure']:
            with self.subTest(result=result):
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.finish(result)
                self.assertFalse(any(method == 'PUT' for _, method, _ in self.calls))
                self.assertEqual(self.calls[-1][2]['state'], 'failure')

    def test_actual_job_completion_is_checked_independently_of_aggregate_result(self):
        for result in ['skipped', 'failure', None]:
            with self.subTest(result=result):
                self.jobs['jobs'][1]['conclusion'] = result
                with self.assertRaisesRegex(ValueError, 'actually succeed'):
                    self.finish()
                self.assertFalse(any(method == 'PUT' for _, method, _ in self.calls))

    def test_other_run_attempt_or_missing_groups_cannot_authorize_merge(self):
        for field, value in [('execution_context', self.environment | {'GITHUB_RUN_ATTEMPT': '2'}),
                             ('test_groups', ['unit'])]:
            with self.subTest(field=field):
                proof = self.proof | {field: value}
                with self.assertRaises(ValueError):
                    merge.workflow_acceptance(self.receipt, proof, self.run, self.jobs, self.environment)

    def test_changed_merge_result_blocks_downstream_dispatch(self):
        self.commit['tree']['sha'] = '8' * 40
        with self.assertRaisesRegex(ValueError, 'no downstream dispatch'):
            self.finish()
        self.assertFalse(any(path.endswith('/dispatches') for path, _, _ in self.calls))

    def test_dependency_request_refuses_changed_or_incomplete_source_selection(self):
        merge.expected_candidate(self.receipt)
        merge.expected_candidate(self.receipt, self.receipt['head'], self.receipt['base'])
        for head, base in [('9'*40, self.receipt['base']), (self.receipt['head'], '9'*40),
                           (self.receipt['head'], ''), ('', self.receipt['base']), ('bad', 'bad')]:
            with self.subTest(head=head, base=base), self.assertRaises(ValueError):
                merge.expected_candidate(self.receipt, head, base)

    def test_only_main_branch_owner_dispatch_is_eligible(self):
        merge.dispatch_context('owner/project', self.environment)
        for override in [{'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_EVENT_NAME': 'pull_request'},
                         {'GITHUB_REPOSITORY': 'other/project'}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                merge.dispatch_context('owner/project', self.environment | override)

    def test_recipe_source_pin_must_be_exact_and_supported(self):
        declaration = {'version': 1, 'recipe': 'godot-addon', 'tests': {'unit': [{}], 'integration': [{}]}}
        pin, groups = merge.recipe_pin('COOKBOOK_REF := ' + 'e' * 40, declaration)
        self.assertEqual(pin, 'e' * 40)
        self.assertEqual(groups, ['integration', 'lint', 'security', 'unit'])
        for makefile in ['COOKBOOK_REF := main', 'COOKBOOK_REF := ' + 'e' * 40 + '\nCOOKBOOK_REF := ' + 'e' * 40]:
            with self.assertRaises(ValueError):
                merge.recipe_pin(makefile, declaration)

    def test_snapshot_expands_case_groups_before_deriving_complete_matrix_and_hashes_raw_source(self):
        source = '''version=1
recipe="godot-game"
[ci.godot]
unit_shards=1
[[tests.unit]]
runner="godot-check"
cwd="app"
timeout=180
cases=[{name="test-first",path="app/tests/first.gd"},{name="test-second",path="app/tests/second.gd"}]
'''
        flat = tomllib.loads(source)
        flat['tests']['unit'] = [dict(runner='godot-check', cwd='app', timeout=180,
            name='test-'+name, path='app/tests/'+name+'.gd') for name in ['first', 'second']]
        commit = self.commit | {'sha': 'c'*40, 'parents': [{'sha': 'a'*40}, {'sha': 'b'*40}]}
        def api(path, method='GET', data=None):
            if '/compare/' in path:
                return {'status': 'identical'}
            if '/git/commits/' in path:
                return commit
            return self.api(path, method, data)
        files = {'Makefile': 'COOKBOOK_REF := '+'e'*40, 'cookbook.toml': source}
        with patch.dict(os.environ, self.environment), patch.object(merge, 'api', side_effect=api), \
                patch.object(merge, 'source_file', side_effect=lambda repository, revision, path: files[path]):
            receipt = merge.snapshot('owner/project', 7)
        self.assertEqual(receipt['declaration'], flat)
        self.assertEqual(merge.orchestration.contract(receipt['declaration']), merge.orchestration.contract(flat))
        self.assertEqual(receipt['configuration_sha256'], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(receipt['test_groups'], ['lint', 'security', 'unit'])
        self.assertEqual(sum(method == 'POST' for _, method, _ in self.calls), 1)

    def test_source_recipe_hash_and_partial_coverage_cannot_authorize_merge(self):
        for overrides in [{'configuration_sha256': '2' * 64}, {'recipe': 'rust-cli'},
                          {'coverage': 'native-partial'}, {'native_matrix': {}}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                merge.workflow_acceptance(self.receipt, self.proof | overrides, self.run, self.jobs, self.environment)

    def test_native_matrix_acceptance_is_rederived_from_snapshot_declaration(self):
        import test_native_ci
        fixture = test_native_ci.NativeMatrix()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config['version'] = 1
        plan = fixture.declaration()
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            proofs = []
            for entry in plan['requirements']:
                target = Path(directory) / entry['id']
                target.mkdir()
                proofs.append(fixture.packet(entry, target))
        jobs = fixture.jobs(plan['requirements'])
        matrix = {'version': 1, 'requirements': plan['requirements'], 'host_groups': plan['host_groups'],
                  'source_commit': fixture.source, 'cookbook_commit': fixture.tools, 'proofs': proofs,
                  'jobs': merge.orchestration.matrix_module(fixture.config).validate_jobs(plan['requirements'], jobs)}
        receipt = self.receipt | {'recipe': 'rust-cli', 'declaration': fixture.config,
                  'candidate': fixture.source, 'tree': fixture.tree, 'cookbook_commit': fixture.tools,
                  'test_groups': sorted(plan['host_groups']), 'configuration_sha256': proofs[0]['configuration_sha256']}
        environment = fixture.environment | self.environment
        # The fixture packet context and controller share the same exact run/attempt.
        environment['GITHUB_RUN_ATTEMPT'] = fixture.environment['GITHUB_RUN_ATTEMPT']
        run = self.run | {'run_attempt': int(environment['GITHUB_RUN_ATTEMPT'])}
        proof = self.proof | {'recipe': 'rust-cli', 'source_commit': fixture.source, 'source_tree': fixture.tree,
                  'cookbook_commit': fixture.tools, 'test_groups': receipt['test_groups'],
                  'configuration_sha256': receipt['configuration_sha256'], 'execution_context': environment,
                  'native_matrix': matrix, 'coverage': 'complete-host-and-native-matrix'}
        # Native packets bind all protected workflow context, not merely run IDs.
        for packet in proofs:
            packet['execution_context'].update(environment)
        jobs['jobs'] += self.jobs['jobs']
        jobs['total_count'] = len(jobs['jobs'])
        merge.workflow_acceptance(receipt, proof, run, jobs, environment)
        matrix['proofs'].pop()
        with self.assertRaisesRegex(ValueError, 'omitted or repeated'):
            merge.workflow_acceptance(receipt, proof, run, jobs, environment)


if __name__ == '__main__':
    unittest.main()
