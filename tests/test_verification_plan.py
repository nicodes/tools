import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'helpers/verification-plan.py'
spec = importlib.util.spec_from_file_location('verification_plan', HELPER)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class VerificationPlanning(unittest.TestCase):
    def setUp(self):
        self.value = {'version': 1, 'full_targets': ['lint', 'unit', 'integration', 'build', 'e2e'],
                      'always': ['lint'], 'rules': [
                          {'paths': ['README.md', 'docs/*'], 'targets': ['lint']},
                          {'paths': ['unit-tests/*'], 'targets': ['unit']}]}

    def test_documentation_and_test_inputs_have_explicit_owners(self):
        self.assertEqual(planner.select(self.value, ['README.md'], 'pull_request')['targets'], ['lint'])
        self.assertEqual(planner.select(self.value, ['unit-tests/check.py'], 'local')['targets'], ['lint', 'unit'])
        self.assertEqual(planner.select(self.value, ['README.md', 'unit-tests/check.py'], 'local')['targets'], ['lint', 'unit'])
        legacy = {**self.value, 'full_targets': ['lint', 'test', 'build']}
        self.assertEqual(planner.select(legacy, ['runtime.py'], 'local')['targets'], ['lint', 'test', 'build'])

    def test_shared_unknown_deleted_renamed_or_production_inputs_require_full_gate(self):
        for names, event in [(['app/code.py'], 'pull_request'), (['engineering-plan.json'], 'pull_request'),
                             (['.github/workflows/ci.yml'], 'local'), (['app/package.json'], 'local'),
                             (['docs/deleted.md', 'app/renamed.py'], 'local'),
                             (['README.md'], 'push'), (['README.md'], 'workflow_dispatch')]:
            with self.subTest(names=names, event=event):
                self.assertEqual(planner.select(self.value, names, event)['mode'], 'full')

    def test_real_git_diff_covers_untracked_changes_and_both_sides_of_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', root], check=True)
            (root / 'README.md').write_text('original')
            (root / 'engineering-plan.json').write_text(json.dumps(self.value))
            subprocess.run(['git', '-C', root, 'add', '.'], check=True)
            subprocess.run(['git', '-C', root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                            'commit', '-qm', 'baseline'], check=True)
            base = subprocess.check_output(['git', '-C', root, 'rev-parse', 'HEAD'], text=True).strip()
            (root / 'README.md').write_text('changed')
            def run(revision=base):
                return json.loads(subprocess.check_output(['python3', HELPER, '--base', revision], cwd=root, text=True))
            self.assertEqual(run()['targets'], ['lint'])
            (root / 'new-code.py').write_text('code')
            self.assertEqual(run()['mode'], 'full')
            (root / 'new-code.py').unlink()
            (root / 'README.md').rename(root / 'runtime.py')
            self.assertEqual(run()['changed_paths'], ['README.md', 'runtime.py'])
            self.assertEqual(run()['mode'], 'full')
            self.assertEqual(run('f' * 40)['mode'], 'full')

    def test_invalid_policy_cannot_select_an_empty_or_injected_full_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            for patch in [{'full_targets': []}, {'full_targets': ['build;true']},
                          {'always': ['unknown']}, {'rules': [{'paths': ['../outside'], 'targets': ['lint']}]}]:
                path.write_text(json.dumps({**self.value, **patch}))
                with self.assertRaises(ValueError):
                    planner.policy(path)

    def test_actual_composite_skips_only_unaffected_work_and_keeps_its_check_running(self):
        action = json.loads(subprocess.check_output(['bun', '-e',
            "import {readFileSync} from 'node:fs'; console.log(JSON.stringify(Bun.YAML.parse(readFileSync(process.argv[1], 'utf8'))))",
            str(HELPER.parent.parent / 'make/action.yml')], text=True))
        step = next(s for s in action['runs']['steps'] if s.get('name') == 'Run the engineering contract')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'checkout'; root.mkdir()
            (root / '.gitignore').write_text('.artifacts/\n')
            (root / 'README.md').write_text('original')
            (root / 'engineering-plan.json').write_text(json.dumps(self.value))
            subprocess.run(['git', 'init', '-q', root], check=True)
            subprocess.run(['git', '-C', root, 'add', '.'], check=True)
            subprocess.run(['git', '-C', root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                            'commit', '-qm', 'baseline'], check=True)
            base = subprocess.check_output(['git', '-C', root, 'rev-parse', 'HEAD'], text=True).strip()
            binary = Path(directory) / 'bin'; binary.mkdir()
            (binary / 'make').write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RESULT"\n')
            (binary / 'make').chmod(0o755)
            result = Path(directory) / 'make-arguments'
            context = {'inputs.target': 'install build', 'inputs.changes-base': base,
                       'github.action_path': str(HELPER.parent.parent / 'make'),
                       'inputs.build-cache': 'false', 'inputs.web-cache': 'false', 'inputs.go-cache': 'false'}
            environment = {**os.environ, 'GITHUB_EVENT_NAME': 'pull_request', 'RESULT': str(result),
                           'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                           **{k: re.sub(r'\$\{\{\s*(.*?)\s*\}\}', lambda m: context[m[1]], v)
                              for k, v in step['env'].items()}}
            (root / 'README.md').write_text('changed')
            subprocess.run(['bash', '-c', step['run']], cwd=root, env=environment, check=True,
                           capture_output=True, text=True)
            self.assertFalse(result.exists())
            self.assertEqual(json.loads((root / '.artifacts/contract/verification-plan.json').read_text())['mode'], 'focused')
            (root / 'new-code.py').write_text('new runtime input')
            subprocess.run(['bash', '-c', step['run']], cwd=root, env=environment, check=True,
                           capture_output=True, text=True)
            self.assertEqual(result.read_text().splitlines(), ['--', 'install', 'build'])
            legacy = {**self.value, 'full_targets': ['lint', 'test', 'build'], 'rules': self.value['rules'][:1]}
            (root / 'engineering-plan.json').write_text(json.dumps(legacy))
            environment['ENGINEERING_TARGET'] = 'test'
            subprocess.run(['bash', '-c', step['run']], cwd=root, env=environment, check=True,
                           capture_output=True, text=True)
            self.assertEqual(result.read_text().splitlines(), ['--', 'test'])
