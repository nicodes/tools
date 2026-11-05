"""Require exact native shard closure and preserve distinct source-declared hosts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import godot_ci


class GodotTopologyControls(unittest.TestCase):
    def config(self):
        return {'recipe': 'godot-game', 'tests': {
            'unit': [{'name': 'test-case_' + str(index), 'runner': 'godot-check', 'path': f'app/tests/case_{index}.gd'} for index in range(59)] +
                    [{'runner': 'python', 'path': 'tests/native/test_sources.py'}],
            'integration': [{'name': 'test-backup', 'runner': 'python', 'path': 'tests/backup/test_restore.py'}]},
            'ci': {'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 20,
                   'godot': {'unit_shards': 2, 'unit_workers': 4, 'build_timeout_minutes': 25,
                             'unit_timeout_minutes': 20, 'host_timeout_minutes': 20}}}

    def test_exact_sixty_original_groups_are_split_without_duplicate_assertions(self):
        config = self.config(); plan = godot_ci.plan(None, config)
        units = plan['units']['include']; host = next(value for value in plan['requirements'] if value['kind'] == 'host')
        self.assertEqual(len(plan['unit_inventory']), 60)
        self.assertEqual(len(plan['godot_unit_inventory']), 59)
        first, second = map(lambda value: set(value['cases']), units)
        self.assertFalse(first & second)
        all_cases = set(plan['written_case_inventory'])
        self.assertEqual(first | second | set(host['cases']), all_cases)
        self.assertEqual([value['timeout_minutes'] for value in units], [20, 20])
        self.assertEqual(next(value['timeout_minutes'] for value in plan['requirements'] if value['kind'] == 'build'), 25)

    def test_backup_and_stock_browser_use_their_actual_runner_and_deadlines(self):
        config = self.config(); config['node'] = {'packages': [{'directory': 'tests/e2e', 'manager': 'bun'}]}
        config['ci']['godot']['gates'] = [
            {'name': 'backup-fixture', 'runner': 'blacksmith-4vcpu-ubuntu-2404', 'timeout_minutes': 20,
             'suite': 'integration', 'tests': ['test-backup']},
            {'name': 'browser-capability', 'kind': 'capability', 'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 8,
             'setup': 'browser', 'packages': ['tests/e2e'], 'entry': {'runner': 'bun', 'path': 'tests/e2e/browser_preflight.mjs', 'timeout': 120}},
            {'name': 'production', 'kind': 'production', 'runner': 'blacksmith-4vcpu-ubuntu-2204', 'timeout_minutes': 10,
             'setup': 'browser', 'packages': ['tests/e2e'], 'entry': {'runner': 'bun', 'path': 'tests/e2e/production_smoke.mjs', 'timeout': 360}}]
        plan = godot_ci.plan(None, config); gates = {value['id']: value for value in plan['gates']['include']}
        self.assertEqual(gates['gate-backup-fixture']['runner'], 'blacksmith-4vcpu-ubuntu-2404')
        self.assertEqual(gates['gate-browser-capability']['runner'], 'blacksmith-4vcpu-ubuntu-2204')
        self.assertEqual(gates['gate-browser-capability']['timeout_minutes'], 8)
        self.assertNotIn('gate-production', [value['id'] for value in plan['requirements']])
        self.assertEqual(plan['production']['include'][0]['timeout_minutes'], 10)
        backup = gates['gate-backup-fixture']['cases'][0]
        host = next(value for value in plan['requirements'] if value['kind'] == 'host')
        self.assertNotIn(backup, host['cases'])

    def test_unknown_empty_or_duplicate_slices_fail_before_execution(self):
        for shard in ['0/2', '1/0', '3/2', '1/3', '1', '1/2;echo', '-1/2']:
            with self.subTest(shard=shard), self.assertRaises(ValueError):
                godot_ci.partition(['test-one', 'test-two'], shard)
        for targets in [[], ['test-one', 'test-one'], ['--eval=bad']]:
            with self.assertRaises(ValueError): godot_ci.partition(targets, '1/1')
        config = self.config(); config['ci']['godot']['unit_workers'] = 8
        with self.assertRaises(ValueError): godot_ci.plan(None, config)

    def test_real_parallel_children_propagate_failure_and_restored_success(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'other-ran'
            cases = {'test-failure': [[sys.executable, '-c', 'raise SystemExit(19)']],
                     'test-other': [[sys.executable, '-c', 'from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("ran")']]}
            def execute(commands):
                for command in commands:
                    result = subprocess.run(command, check=False)
                    if result.returncode: return result.returncode
                return 0
            self.assertEqual(godot_ci.run_cases(cases, 2, execute), 19)
            self.assertEqual(marker.read_text(), 'ran')
            cases['test-failure'] = [[sys.executable, '-c', 'raise SystemExit(0)']]
            self.assertEqual(godot_ci.run_cases(cases, 2, execute), 0)


class SelectionEvidenceControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.targets = ['test-one', 'test-three', 'test-two']
        for number, selected in [(1, ['test-one', 'test-two']), (2, ['test-three'])]:
            folder = self.root / str(number); folder.mkdir()
            (folder / 'godot-test-selection.json').write_text(json.dumps({
                'source_commit': 'a' * 40, 'run_id': '123', 'attempt': '2',
                'shard': f'{number}/2', 'workers': 4, 'inventory': self.targets,
                'selected': selected, 'status': 'passed'}))

    def verify(self):
        return godot_ci.verify_selections(self.root, 'a' * 40, self.targets, 2, '123', '2')

    def test_exact_current_run_disjoint_inventory_passes(self):
        self.assertEqual(self.verify()['suite_targets'], 3)

    def test_stale_failed_repeated_or_partial_evidence_cannot_pass(self):
        path = self.root / '2/godot-test-selection.json'; original = json.loads(path.read_text())
        for key, value in [('source_commit', 'b' * 40), ('run_id', '122'), ('attempt', '1'),
                           ('status', 'failed'), ('workers', 8), ('shard', '1/2'),
                           ('selected', ['test-one']), ('inventory', self.targets[:-1])]:
            with self.subTest(field=key):
                path.write_text(json.dumps({**original, key: value}))
                with self.assertRaises(ValueError): self.verify()
        path.unlink()
        with self.assertRaises(ValueError): self.verify()

    def test_linked_proof_cannot_impersonate_completed_coverage(self):
        path = self.root / '2/godot-test-selection.json'; saved = self.root / 'saved.json'
        path.rename(saved); path.symlink_to(saved)
        with self.assertRaises(ValueError): self.verify()


if __name__ == '__main__':
    unittest.main()
