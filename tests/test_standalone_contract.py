"""Standalone tools use native Mise stages with ordered, fail-closed execution."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StandaloneSourceContract(unittest.TestCase):
    def test_recipe_execution_and_first_party_Make_are_not_standalone_Tools_interfaces(self):
        self.assertFalse((ROOT/'cookbook').exists())
        self.assertFalse((ROOT/'Makefile').exists())
        self.assertFalse((ROOT/'compiler-cache/action.yml').exists())
        self.assertFalse((ROOT/'native-diagnostics/action.yml').exists())
        self.assertFalse(list((ROOT/'.github/workflows').glob('cookbook-*')))
        data = tomllib.loads((ROOT/'mise.toml').read_text())
        self.assertIs(data['settings']['task']['run_auto_install'],False)
        for task in data['tasks'].values():
            self.assertNotIn('depends',task)
            self.assertNotIn('depends_post',task)
            self.assertNotIn('make ',str(task))

    def test_unit_native_programs_and_argument_order_are_preserved(self):
        expected = [
            'node --test tests/fixture-origins.test.mjs tests/fixture-ports.test.mjs tests/prepare-metro.test.mjs',
            'cargo test --workspace --all-targets --locked',
            'cargo build --locked -p tools-operator',
            'bash tests/publish-inputs.test.sh',
            'bash tests/setup-godot-inputs.test.sh',
            "python3 -m unittest discover -s scripts -p 'test_*.py' -v",
            "python3 -m unittest discover -s tests -p 'test_*.py' -v",
            "python3 -m unittest discover -s crates/operator/internal/operator -p 'test_*.py' -v",
        ]
        data = tomllib.loads((ROOT/'mise.toml').read_text())
        commands = []
        for run in data['tasks']['unit']['run']:
            argv = shlex.split(run)
            self.assertEqual(argv[:5],['python3','-I','-B','helpers/pinned-tools.py','exec'])
            separator = argv.index('--')
            self.assertEqual(argv[separator+1:separator+3],['bash','-euc'])
            commands.append(argv[-1])
        self.assertEqual(commands,expected)
        self.assertEqual(data['tasks']['check']['run'],
                         [{'task':name} for name in ['lint','unit','build','vuln','integration']])

    def test_generic_Docker_action_does_not_read_recipe_capability_or_source(self):
        action = (ROOT/'docker-ci/action.yml').read_text()
        self.assertNotIn('cookbook.toml',action)
        self.assertNotIn('GODOT_ENTRY',action)
        self.assertIn('ci-registry-mirror.sh',action)
        self.assertIn('docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069',action)


@unittest.skipUnless(shutil.which('mise'), 'existing Mise is required for native task scheduling')
class NativeMiseScheduling(unittest.TestCase):
    def exercise(self, failing=None):
        with tempfile.TemporaryDirectory(prefix='native ordered task fixture ') as directory:
            root = Path(directory)
            actual = tomllib.loads((ROOT/'mise.toml').read_text())['tasks']['check']['run']
            source = '[tasks.check]\nraw=true\nrun=['+', '.join('{task="'+row['task']+'"}' for row in actual)+']\n'
            for row in actual:
                name = row['task']
                program = 'printf \'%s\\n\' '+name+' >> order'
                if name == failing:
                    program += '; exit 37'
                import json
                source += '\n[tasks.'+name+']\nraw=true\nrun='+json.dumps(program)+'\n'
            config = root/'mise.toml'
            config.write_text(source)
            environment = dict(os.environ, MISE_CONFIG_DIR=str(root/'control/config'),
                MISE_DATA_DIR=str(root/'control/data'), MISE_CACHE_DIR=str(root/'control/cache'),
                MISE_STATE_DIR=str(root/'control/state'), MISE_CEILING_PATHS=str(root.parent),
                MISE_AUTO_INSTALL='false', MISE_EXEC_AUTO_INSTALL='false',
                MISE_TASK_RUN_AUTO_INSTALL='false')
            trust = subprocess.run(['mise','trust',str(config)],cwd=root,env=environment,
                                   text=True,capture_output=True,timeout=10)
            self.assertEqual(trust.returncode,0,trust.stderr)
            result = subprocess.run(['mise','run','--skip-tools','check'],cwd=root,env=environment,
                                    text=True,capture_output=True,timeout=15)
            return result,(root/'order').read_text().splitlines()

    def test_actual_Mise_runs_each_native_stage_in_original_order(self):
        result,order = self.exercise()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(order,['lint','unit','build','vuln','integration'])

    def test_failed_native_stage_stops_later_build_scan_and_integration(self):
        result,order = self.exercise('unit')
        self.assertEqual(result.returncode,37)
        self.assertEqual(order,['lint','unit'])


if __name__ == '__main__':
    unittest.main()
