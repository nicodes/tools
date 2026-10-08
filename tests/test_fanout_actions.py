import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('fanout_actions', Path(__file__).resolve().parents[1]/'scripts/fanout_actions.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OLD, NEW = 'a'*40, 'b'*40


class ActionAdoption(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        files = {
            'ACTION-PINS.json': json.dumps({'repository': 'https://github.com/nicodes/komizo-actions', 'pins': {'deploy': {'tag': 'v0.0.30', 'sha': OLD}}}, indent=2)+'\n',
            '.github/workflows/cd.yml': 'jobs:\n  deploy:\n    uses: nicodes/komizo-actions/deploy@'+OLD+' # v0.0.30\n',
            '.github/actions/publish/action.yml': 'runs:\n  steps:\n    - uses: nicodes/komizo-actions/deploy@'+OLD+'\n',
            'fleet-policy.json': json.dumps({'baseline': {'action_tag': 'v0.0.30', 'komizo_actions_tag': 'v0.0.30', 'snapshot_revision': 'c'*40}}, indent=2)+'\n',
            'tools/test_supply_chain.py': 'pin = "nicodes/komizo-actions/deploy@'+OLD+' # v0.0.30"\n',
        }
        for relative, value in files.items():
            self.write(relative, value)
        self.write('scripts/engineering-env.sh', 'export FLEET_BASELINE_SHA256='+module.digest((self.root/'fleet-policy.json').read_bytes())+'\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)

    def write(self, relative, value):
        path = self.root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def test_dry_run_coherent_and_idempotent(self):
        before = (self.root/'ACTION-PINS.json').read_bytes()
        edits = module.changes(self.root, '0.0.31', NEW)
        self.assertEqual((self.root/'ACTION-PINS.json').read_bytes(), before)
        self.assertEqual(len(edits), 6)
        for relative, (_, after) in edits.items():
            (self.root/relative).write_bytes(after)
        self.assertEqual(module.changes(self.root, '0.0.31', NEW), {})
        policy = json.loads((self.root/'fleet-policy.json').read_text())
        self.assertEqual(policy['baseline']['snapshot_revision'], 'c'*40)

    def test_unknown_or_moving_refs_reject_without_writing(self):
        for ref in ['main', 'c'*40]:
            self.write('.github/workflows/cd.yml', 'uses: nicodes/komizo-actions/deploy@'+ref+'\n')
            with self.assertRaises(ValueError):
                module.changes(self.root, '0.0.31', NEW)

    def test_unknown_action_rejected(self):
        self.write('.github/workflows/cd.yml', 'uses: nicodes/komizo-actions/unknown@'+OLD+'\n')
        with self.assertRaises(ValueError):
            module.changes(self.root, '0.0.31', NEW)

    def test_bad_digest_and_policy_rejected(self):
        self.write('scripts/engineering-env.sh', 'wrong checksum\n')
        with self.assertRaises(ValueError):
            module.changes(self.root, '0.0.31', NEW)

    def test_unhandled_tracked_pin_rejected(self):
        self.write('other.py', 'pin = "'+OLD+'"\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'other.py'], check=True)
        with self.assertRaisesRegex(ValueError, 'unhandled old release'):
            module.changes(self.root, '0.0.31', NEW)


if __name__ == '__main__':
    unittest.main()
