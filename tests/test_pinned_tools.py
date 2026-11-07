import importlib.util
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
import unittest

SPEC = importlib.util.spec_from_file_location('pinned_tool_control', Path(__file__).parents[1] / 'helpers/pinned-tools.py')
tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tools)


class InstalledExecution(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'mise.toml').write_text('[tools]\npython="3.13.11"\nrust="1.94.0"\n')
        self.install = self.root / 'installed-python'
        self.install.mkdir()
        self.bin = self.install / 'bin'
        self.bin.mkdir()
        self.python = self.bin / ('python3.exe' if os.name == 'nt' else 'python3')
        self.python.write_bytes(b'finite executable control')
        self.python.chmod(0o755)
        self.queries = []

    def lookup(self, arguments, root, environment):
        self.queries.append(arguments)
        self.assertEqual(root, self.root)
        self.assertEqual(environment['MISE_TASK_RUN_AUTO_INSTALL'], 'false')
        self.assertEqual(environment['MISE_EXEC_AUTO_INSTALL'], 'false')
        version = tools.pins(root)['python']
        version = version['version'] if isinstance(version, dict) else version
        selector = 'python@' + version
        if arguments == ['where', selector]:
            return str(self.install)
        self.assertEqual(arguments, ['env', '--json', selector])
        return json.dumps({'PATH': str(self.bin)})

    def test_selects_only_actual_stage_SDK_and_preserves_native_environment(self):
        result = tools.activation(self.root, [('python', 'python3')],
                                  {'GITHUB_RUN_ID': '41', 'TOOLS_VERIFICATION_SESSION': 'same', 'NATIVE_FLAG': '0'},
                                  lookup=self.lookup)
        self.assertEqual(result['NATIVE_FLAG'], '0')
        self.assertEqual(result['GITHUB_RUN_ID'], '41')
        self.assertEqual(result['TOOLS_VERIFICATION_SESSION'], 'same')
        self.assertEqual(self.queries, [['where', 'python@3.13.11'], ['env', '--json', 'python@3.13.11']])

    def test_missing_install_refuses_before_activation_or_PATH_execution(self):
        self.python.unlink()
        self.bin.rmdir()
        self.install.rmdir()
        with self.assertRaisesRegex(ValueError, 'not installed'):
            tools.activation(self.root, [('python', 'python3')], lookup=self.lookup)
        self.assertEqual(self.queries, [['where', 'python@3.13.11']])

    def test_foreign_PATH_executable_refused_despite_existing_install(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        executable = foreign / self.python.name
        executable.write_bytes(b'unrelated executable')
        executable.chmod(0o755)
        original = self.lookup

        def lookup(arguments, root, environment):
            value = original(arguments, root, environment)
            return json.dumps({'PATH': str(foreign) + os.pathsep + str(self.bin)}) if arguments[0] == 'env' else value

        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            tools.activation(self.root, [('python', 'python3')], lookup=lookup)

    def test_changed_execution_identity_refused(self):
        def lookup(arguments, root, environment):
            return str(self.install) if arguments[0] == 'where' else json.dumps({'PATH': str(self.bin), 'GITHUB_RUN_ID': 'other'})
        with self.assertRaisesRegex(ValueError, 'execution identity'):
            tools.activation(self.root, [('python', 'python3')], {'GITHUB_RUN_ID': '41'}, lookup=lookup)

    def test_mutable_or_missing_pin_refuses_before_lookup(self):
        (self.root / 'mise.toml').write_text('[tools]\npython="latest"\n')
        with self.assertRaisesRegex(ValueError, 'exact existing'):
            tools.activation(self.root, [('python', 'python3')], lookup=self.lookup)
        self.assertFalse(self.queries)

    def test_binding_collisions_and_path_commands_refused(self):
        for values in [[], ['python=../python3'], ['python=python3', 'rust=python3'], ['python=python3', 'python=python3']]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                tools.bindings(values)

    def test_table_pin_keeps_frozen_native_version_and_options(self):
        (self.root / 'mise.toml').write_text('[tools]\npython={version="v3.13.11",asset_pattern="unchanged",bin="python3"}\n')
        tools.activation(self.root, [('python', 'python3')], lookup=self.lookup)


class RealExactSelection(unittest.TestCase):
    @unittest.skipUnless(shutil.which('mise'), 'existing Mise installation is required')
    def test_exact_pin_overrides_hostile_version_env_without_installing_unrelated_SDKs(self):
        with tempfile.TemporaryDirectory(prefix='exact SDK source ') as directory:
            root = Path(directory)
            (root/'mise.toml').write_text('[tools]\npython="3.13.11"\ngo="0.0.0"\n')
            environment = dict(os.environ, MISE_PYTHON_VERSION='0.0.0',
                MISE_TRUSTED_CONFIG_PATHS=str(root/'mise.toml'),
                MISE_CEILING_PATHS=str(root.parent), **tools.DISABLED)
            # Both where and env receive the exact source version, rather than
            # allowing MISE_PYTHON_VERSION to replace the declared authority.
            activated = tools.activation(root,[('python','python3')],environment)
            result = subprocess.run(['python3','-I','-B','-c',
                'import sys; print(".".join(map(str,sys.version_info[:3])))'],
                cwd=root,env=activated,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'3.13.11')
            self.assertEqual(activated['MISE_TASK_RUN_AUTO_INSTALL'],'false')
            self.assertFalse((root/'.artifacts').exists())



if __name__ == '__main__':
    unittest.main()
