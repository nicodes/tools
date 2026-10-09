import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from vendored import skip_module_if_vendored

skip_module_if_vendored('source-only operation broker')
spec = importlib.util.spec_from_file_location('operations', Path(__file__).parents[1]/'scripts/operations.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
REPO, SHA, DIGEST = 'example/app', 'a'*40, 'b'*64


class DurableOperations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'private'/'operations.sqlite'
        self.journal = ops.Journal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.grant = {'version': 1, 'repositories': {REPO: {'rerun_workflows': ['cd.yml', 'backup.yml']}}}
        self.run = {'id': 42, 'run_attempt': 1, 'repository': {'full_name': REPO},
                    'path': '.github/workflows/cd.yml', 'head_sha': SHA, 'head_branch': 'main',
                    'status': 'completed', 'conclusion': 'failure'}
        self.head = SHA
        self.posts = []

    def api(self, path, method='GET'):
        if method == 'POST':
            self.posts.append(path)
            return None
        return {'sha': self.head} if '/commits/' in path else copy.deepcopy(self.run)

    def prepare(self):
        with patch.object(ops, 'api', self.api):
            return ops.prepare(self.journal, self.grant, DIGEST, REPO, 42, SHA, now=100)

    def execute(self, identity):
        with patch.object(ops, 'api', self.api):
            return ops.execute(self.journal, self.grant, DIGEST, identity, now=101)

    def observe(self, identity, now=105):
        with patch.object(ops, 'api', self.api):
            return ops.observe(self.journal, identity, now=now)

    def test_exact_failed_main_run_can_be_rerun_and_success_releases_scope(self):
        row = self.execute(self.prepare()['id'])
        self.assertEqual(row['state'], 'waiting')
        self.assertEqual(len(self.posts), 1)
        self.run.update(run_attempt=2, conclusion='success')
        self.assertEqual(self.observe(row['id'])['state'], 'succeeded')
        self.assertEqual(self.journal.db.execute('SELECT count(*) FROM locks').fetchone()[0], 0)

    def test_changed_main_or_attempt_rejects_before_post(self):
        row = self.prepare()
        self.head = 'c'*40
        self.assertEqual(self.execute(row['id'])['state'], 'rejected')
        self.assertEqual(self.posts, [])

    def test_unapproved_repo_workflow_wrong_source_and_running_run_fail_plan(self):
        for changes in ({'path': '.github/workflows/recovery.yml'}, {'head_sha': 'c'*40},
                        {'status': 'in_progress'}, {'head_branch': 'untrusted'}, {'conclusion': 'success'}):
            original = self.run.copy()
            self.run.update(changes)
            with self.assertRaises(ValueError):
                self.prepare()
            self.run = original
        with patch.object(ops, 'api', self.api), self.assertRaises(ValueError):
            ops.prepare(self.journal, self.grant, DIGEST, 'outside/repo', 42, SHA)

    def test_two_workers_cannot_claim_one_operation_or_one_production_scope(self):
        first, second = self.prepare(), self.prepare()
        other = ops.Journal(self.path)
        self.addCleanup(other.close)
        self.journal.claim(first['id'], now=101)
        for identity in (first['id'], second['id']):
            with self.assertRaises(ValueError):
                other.claim(identity, now=102)

    def test_worker_crash_and_expired_lease_never_permit_a_second_execution(self):
        row = self.prepare()
        self.journal.claim(row['id'], now=101, lease_seconds=2)
        observed = self.observe(row['id'], now=104)
        self.assertEqual(observed['state'], 'uncertain')
        with self.assertRaises(ValueError):
            self.execute(row['id'])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.journal.db.execute('SELECT count(*) FROM locks').fetchone()[0], 1)

    def test_lost_http_response_is_reconciled_without_another_post(self):
        row = self.prepare()
        def lost(path, method='GET'):
            if method == 'POST':
                self.posts.append(path)
                self.run.update(run_attempt=2, conclusion='success')
                raise ValueError('network failed after acceptance with secret stderr')
            return self.api(path, method)
        with patch.object(ops, 'api', lost):
            result = ops.execute(self.journal, self.grant, DIGEST, row['id'], now=101)
        self.assertEqual(result['state'], 'uncertain')
        self.assertNotIn('secret stderr', json.dumps(result))
        self.assertEqual(self.observe(row['id'])['state'], 'succeeded')
        self.assertEqual(len(self.posts), 1)

    def test_multiple_external_attempts_do_not_silently_match(self):
        row = self.execute(self.prepare()['id'])
        self.run.update(run_attempt=3, conclusion='success')
        self.assertEqual(self.observe(row['id'])['state'], 'uncertain')

    def test_expired_intent_and_changed_policy_cannot_execute(self):
        row = self.prepare()
        with self.assertRaises(ValueError):
            self.journal.claim(row['id'], now=1001)
        with self.assertRaises(ValueError):
            ops.execute(self.journal, self.grant, 'c'*64, row['id'], now=101)

    def test_wrong_worker_cannot_complete_execution(self):
        row = self.prepare()
        self.journal.claim(row['id'], now=101)
        with self.assertRaises(ValueError):
            self.journal.transition(row['id'], 'waiting', 'unrelated')

    def test_existing_world_readable_or_symlink_journal_is_refused(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            ops.Journal(self.path)
        self.path.chmod(0o600)
        linked = self.path.parent/'linked.sqlite'
        linked.symlink_to(self.path)
        with self.assertRaises(OSError):
            ops.Journal(linked)

    def test_records_survive_reopening_and_have_ordered_events(self):
        row = self.execute(self.prepare()['id'])
        other = ops.Journal(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.get(row['id'])['state'], 'waiting')
        states = [item[0] for item in other.db.execute('SELECT state FROM events ORDER BY sequence')]
        self.assertEqual(states, ['prepared', 'executing', 'waiting'])
        self.assertEqual(other.get(row['id'])['telemetry']['queue_seconds'], 1)
        self.assertEqual(other.export()['execution_scope'], 'single-broker')
