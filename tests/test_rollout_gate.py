import base64
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from vendored import skip_module_if_vendored

skip_module_if_vendored('source-only adoption operator')
spec = importlib.util.spec_from_file_location('rollout_gate', Path(__file__).parents[1]/'scripts/rollout_gate.py')
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
NOW = datetime.datetime.now(datetime.timezone.utc)
REVISION, MAIN, DIGEST = 'a'*40, 'b'*40, 'c'*64


class CohortAcceptance(unittest.TestCase):
    def plan(self):
        return {'version': 1, 'cohorts': [[{'repository': 'org/canary', 'workflows':
                 [{'file': 'cd.yml', 'max_age_hours': 48}]}],
                 [{'repository': 'org/product', 'workflows': [{'file': 'cd.yml', 'max_age_hours': 48}]}]]}

    def api(self, path):
        if '/commits/' in path:
            return {'sha': MAIN}
        if '/contents/' in path:
            text = (json.dumps({'revision': REVISION}) if 'engineering-pin' in path else
                    '[tools]\n"http:cicd-engineering" = {version="0.15.0", checksum="sha256:'+DIGEST+'"}\n')
            return {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
        return {'workflow_runs': [self.run]}

    def setUp(self):
        self.run = {'id': 1, 'head_sha': MAIN, 'status': 'completed', 'conclusion': 'success',
                    'updated_at': NOW.isoformat(), 'html_url': 'https://github.com/org/canary/actions/runs/1'}

    def gate(self, plan=None, cohort=1, repos=None):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory)/'plan.json'
            policy.write_text(json.dumps(plan or self.plan()))
            with patch.object(rollout, 'api', self.api):
                return rollout.gate(policy, cohort, repos or ['org/product'], 'tools', '0.15.0', REVISION, DIGEST)

    def test_canary_must_be_adopted_and_pass_exact_main_before_expansion(self):
        proof = self.gate()
        self.assertEqual(proof['accepted_consumers'][0]['main_revision'], MAIN)
        self.assertEqual(proof['cohort'], 1)

    def test_failed_pending_wrong_revision_and_expired_runs_stop_expansion(self):
        for changes in ({'conclusion': 'failure'}, {'status': 'in_progress'}, {'head_sha': 'd'*40},
                        {'updated_at': (NOW-datetime.timedelta(hours=49)).isoformat()}):
            with self.subTest(changes=changes):
                old = dict(self.run)
                self.run.update(changes)
                with self.assertRaises(ValueError):
                    self.gate()
                self.run = old

    def test_no_runs_or_inaccessible_evidence_fail_closed(self):
        member = self.plan()['cohorts'][0][0]
        with patch.object(rollout, 'api', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError):
                rollout.verify_consumer(member, 'tools', '0.15.0', REVISION, DIGEST, NOW)

    def test_wrong_release_wrong_cohort_or_duplicate_consumer_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gate(repos=['org/canary'])
        plan = self.plan()
        plan['cohorts'][1] = plan['cohorts'][0]
        with self.assertRaises(ValueError):
            self.gate(plan)
        member = self.plan()['cohorts'][0][0]
        with patch.object(rollout, 'api', self.api):
            with self.assertRaises(ValueError):
                rollout.verify_consumer(member, 'tools', '0.15.1', REVISION, DIGEST, NOW)

    def test_first_cohort_needs_no_prior_acceptance(self):
        proof = self.gate(cohort=0, repos=['org/canary'])
        self.assertEqual(proof['accepted_consumers'], [])
