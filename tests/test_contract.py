import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1]/'helpers/contract.py'
spec = importlib.util.spec_from_file_location('contract', HELPER)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class ContractBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        subprocess.run(['git', 'init', '-q', self.root], check=True)
        (self.root/'.gitignore').write_text('.artifacts/\noutput/\n')
        (self.root/'input.txt').write_text('source')
        subprocess.run(['git', '-C', self.root, 'add', '.'], check=True)
        subprocess.run(['git', '-C', self.root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'fixture'], check=True)
        self.data = {'version': 1, 'stages': {stage: {'inapplicable': 'fixture has no such capability'}
            for stage in contract.STAGES}, 'artifacts': ['output'], 'build_environment': ['BUILD_INPUT']}
        self.data['stages']['build'] = [['sh', '-c', 'mkdir -p output; cp input.txt output/value; echo build >> .artifacts/build-count']]
        self.data['stages']['e2e'] = [['sh', '-c', 'test -s output/value']]

    def run_target(self, target, success=True, environment=None):
        (self.root/'engineering.json').write_text(json.dumps(self.data))
        result = subprocess.run(['python3', HELPER, target], cwd=self.root, text=True,
            capture_output=True, env={**os.environ, **(environment or {})})
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def count(self):
        return len((self.root/'.artifacts/build-count').read_text().splitlines())

    def test_build_reuse_requires_source_environment_and_exact_artifact_bytes(self):
        self.run_target('e2e')
        self.run_target('e2e')
        self.assertEqual(self.count(), 1)
        (self.root/'input.txt').write_text('new source')
        self.run_target('e2e')
        self.assertEqual(self.count(), 2)
        (self.root/'output/value').write_text('corrupted artifact')
        self.run_target('e2e')
        self.assertEqual(self.count(), 3)
        self.run_target('e2e', environment={'BUILD_INPUT': 'different configuration'})
        self.assertEqual(self.count(), 4)

    def test_missing_required_suite_fails_and_explicit_inapplicability_is_reported(self):
        self.data['stages']['unit'] = []
        self.run_target('test', success=False)
        self.data['stages']['unit'] = {'inapplicable': 'No pure logic in this fixture'}
        self.assertIn('inapplicable', self.run_target('test').stdout)

    def test_failed_build_and_source_mutation_cannot_leave_reusable_evidence(self):
        self.run_target('build')
        self.data['stages']['build'] = [['sh', '-c', 'exit 23']]
        self.run_target('build', success=False)
        self.assertFalse((self.root/'.artifacts/contract/build.json').exists())
        self.data['stages']['build'] = [['sh', '-c', 'echo mutation > input.txt']]
        self.run_target('build', success=False)
        self.assertFalse((self.root/'.artifacts/contract/build.json').exists())

    def test_parallel_make_processes_build_once(self):
        (self.root/'engineering.json').write_text(json.dumps(self.data))
        children = [subprocess.Popen(['python3', HELPER, 'e2e'], cwd=self.root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
        self.assertEqual(self.count(), 1)

    def test_symlinked_artifacts_are_rejected(self):
        self.data['stages']['build'] = [['sh', '-c', 'mkdir -p output; ln -s ../input.txt output/value']]
        self.run_target('build', success=False)

class ImageNamespaceInputs(unittest.TestCase):
    def test_default_and_selected_namespaces_bind_the_declared_build_input(self):
        from unittest.mock import patch
        data = {'images':['{IMAGE_BASE}-gate:{revision}'], 'build_environment':['IMAGE_BASE'],
                'build_environment_defaults':{'IMAGE_BASE':'ghcr.io/example/product'}}
        with patch.dict(os.environ, {'IMAGE_BASE':''}):
            self.assertEqual(contract.resolve_images(data,'a'*40), ['ghcr.io/example/product-gate:'+'a'*40])
        with patch.dict(os.environ, {'IMAGE_BASE':'ghcr.io/example/preview'}):
            self.assertEqual(contract.resolve_images(data,'a'*40), ['ghcr.io/example/preview-gate:'+'a'*40])
        with patch.dict(os.environ, {'IMAGE_BASE':'untrusted; command'}), self.assertRaises(ValueError):
            contract.resolve_images(data,'a'*40)
        with self.assertRaises(ValueError):
            contract.resolve_images({**data,'build_environment':[]},'a'*40)


class MakeActionBoundary(unittest.TestCase):
    def test_target_lists_preserve_hyphenated_stages_and_reject_shell_text(self):
        import json
        action = json.loads(subprocess.check_output(["bun", "-e", "import {readFileSync} from 'node:fs'; console.log(JSON.stringify(Bun.YAML.parse(readFileSync(process.argv[1], 'utf8'))))", str(HELPER.parent.parent / "make/action.yml")], text=True))
        script = next(step['run'] for step in action['runs']['steps']
                      if step.get('name') == 'Run the engineering contract')
        with tempfile.TemporaryDirectory() as directory:
            fake_make = Path(directory) / "make"
            fake_make.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            fake_make.chmod(0o755)
            for target in ("lint test vuln", "install build artifact-check e2e", "browser-install"):
                env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"], "ENGINEERING_TARGET": target, "CACHE_ENABLED": "false"}
                result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["--", *target.split()])
            for target in ("build;true", "build\ne2e", "unknown", " build", "build  e2e", "build -f other"):
                env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"], "ENGINEERING_TARGET": target, "CACHE_ENABLED": "false"}
                result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, target)
                self.assertEqual(result.stdout, "")
