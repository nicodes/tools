import importlib.util
import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location('pinned_tool_control', Path(__file__).parents[1] / 'helpers/pinned-tools.py')
tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tools)


class InstalledExecution(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
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
        shutil.copyfile(sys.executable, executable)
        executable.chmod(0o755)
        selected = shutil.which('python3', path=str(foreign) + os.pathsep + str(self.bin))
        self.assertIsNotNone(selected, 'the foreign native executable must actually resolve before its refusal')
        self.assertEqual(Path(selected).resolve(), executable.resolve())
        if os.name == 'nt':
            self.assertEqual(executable.suffix.lower(), '.exe')
            with executable.open('rb') as stream:
                self.assertEqual(stream.read(2), b'MZ')
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

    @unittest.skipUnless(os.name == 'nt', 'native short/long path aliases use the Windows path API')
    def test_native_short_and_long_aliases_bind_the_same_canonical_fixture(self):
        import ctypes
        from ctypes import wintypes
        library = ctypes.WinDLL('kernel32', use_last_error=True)
        aliases = {}
        for name in ['GetShortPathNameW', 'GetLongPathNameW']:
            function = getattr(library, name)
            function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(str(self.root), buffer, len(buffer))
            self.assertGreater(length, 0, ctypes.get_last_error())
            self.assertLess(length, len(buffer))
            aliases[name] = Path(buffer.value)
        short, long = aliases['GetShortPathNameW'], aliases['GetLongPathNameW']
        self.assertTrue(short.samefile(long))
        self.assertEqual(short.resolve(), self.root)
        self.assertEqual(long.resolve(), self.root)
        self.assertEqual(Path(self.temporary.name).resolve(), self.root)
        result = tools.activation(short, [('python', 'python3')], lookup=self.lookup)
        self.assertEqual(Path(shutil.which('python3', path=result['PATH'])).resolve(), self.python.resolve())
        print(json.dumps({'native_path_aliases': {'short': str(short), 'long': str(long),
            'different_spelling': str(short).casefold() != str(long).casefold(),
            'same_native_directory': True, 'canonical_fixture': str(self.root)}}), flush=True)


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



class OwnedRustupExecution(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\n')
        self.proxy = self.root / 'owned-proxies'
        self.proxy.mkdir()
        self.global_bin = self.root / 'package-manager-bin'
        self.global_bin.mkdir()
        self.suffix = '.exe' if os.name == 'nt' else ''
        self.manager = self.global_bin / ('rustup' + self.suffix)
        shutil.copyfile(sys.executable, self.manager)
        self.manager.chmod(0o755)
        for name in ['cargo', 'rustc', 'rustup']:
            (self.proxy / (name + self.suffix)).symlink_to(self.manager)
        self.home = self.root / 'selected-rustup'
        self.native = self.home / 'toolchains' / '1.94.0-fixture-native-host' / 'bin'
        self.native.mkdir(parents=True)
        for name in ['cargo', 'rustc']:
            path = self.native / (name + self.suffix)
            path.write_bytes(b'finite installed native executable')
            path.chmod(0o755)
        self.path = str(self.proxy)
        self.rustup_queries = []

    def mise(self, arguments, root, environment):
        if arguments[:2] == ['which', '--tool=rust@1.94.0']:
            return str(self.proxy / (arguments[2] + self.suffix))
        self.assertEqual(root, self.root)
        self.assertEqual(environment['MISE_TASK_RUN_AUTO_INSTALL'], 'false')
        if arguments == ['where', 'rust@1.94.0']:
            return str(self.proxy)
        self.assertEqual(arguments, ['env', '--json', 'rust@1.94.0'])
        return json.dumps({'PATH': self.path, 'RUSTUP_HOME': str(self.home), 'RUSTUP_TOOLCHAIN': '1.94.0'})

    def rustup(self, arguments, root, environment):
        self.rustup_queries.append(arguments)
        self.assertEqual(root, self.root)
        self.assertEqual(Path(arguments[0]).parent.resolve(), self.proxy)
        self.assertEqual(arguments[1:4], ['which', '--toolchain', '1.94.0'])
        self.assertEqual(environment['RUSTUP_AUTO_INSTALL'], '0')
        self.assertEqual(environment['RUSTUP_TOOLCHAIN'], '1.94.0')
        self.assertEqual(environment['MISE_EXEC_AUTO_INSTALL'], 'false')
        return str(self.native / (arguments[4] + self.suffix)) + '\n'

    def activate(self, **kwargs):
        return tools.activation(self.root, [('rust', 'cargo'), ('rust', 'rustc')],
                                {'GITHUB_RUN_ID': '41', 'NATIVE_FLAG': '0', 'RUSTUP_AUTO_INSTALL': '1'},
                                lookup=self.mise, rustup_lookup=kwargs.get('rustup_lookup', self.rustup))

    def test_owned_external_proxy_selects_exact_real_native_toolchain(self):
        selected = shutil.which('cargo', path=str(self.proxy))
        self.assertFalse(Path(selected).resolve().is_relative_to(self.proxy), 'counterfactual must exercise the old physical-root refusal')
        result = self.activate()
        self.assertEqual(Path(shutil.which('cargo', path=result['PATH'])).parent.resolve(), self.proxy)
        self.assertEqual(Path(shutil.which('rustc', path=result['PATH'])).parent.resolve(), self.proxy)
        self.assertEqual(result['GITHUB_RUN_ID'], '41')
        self.assertEqual(result['NATIVE_FLAG'], '0')
        self.assertEqual(result['RUSTUP_AUTO_INSTALL'], '0')
        self.assertEqual([q[-1] for q in self.rustup_queries], ['rustc', 'cargo'])

    def test_foreign_PATH_proxy_refuses_before_any_rustup_query(self):
        foreign = self.root / 'foreign'
        foreign.mkdir()
        path = foreign / ('cargo' + self.suffix)
        shutil.copyfile(sys.executable, path)
        path.chmod(0o755)
        self.path = str(foreign) + os.pathsep + str(self.proxy)
        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            self.activate()
        self.assertFalse(self.rustup_queries)

    def test_missing_owned_manager_refuses_without_foreign_PATH_fallback(self):
        (self.proxy / ('rustup' + self.suffix)).unlink()
        self.path += os.pathsep + str(self.global_bin)
        with self.assertRaisesRegex(ValueError, 'owned Rustup proxy'):
            self.activate()
        self.assertFalse(self.rustup_queries)

    def test_foreign_toolchain_home_refuses(self):
        outside = self.root / 'foreign-toolchain' / ('cargo' + self.suffix)
        outside.parent.mkdir()
        outside.write_bytes(b'foreign')
        outside.chmod(0o755)
        def lookup(arguments, root, environment):
            return str(outside) if arguments[-1] == 'cargo' else self.rustup(arguments, root, environment)
        with self.assertRaisesRegex(ValueError, 'foreign toolchain home'):
            self.activate(rustup_lookup=lookup)

    def test_wrong_release_and_mixed_native_roots_refuse(self):
        for name in ['1.93.0-fixture-native-host', '1.94.0-other-native-host']:
            other = self.home / 'toolchains' / name / 'bin' / ('cargo' + self.suffix)
            other.parent.mkdir(parents=True)
            other.write_bytes(b'other native executable')
            other.chmod(0o755)
            def lookup(arguments, root, environment):
                return str(other) if arguments[-1] == 'cargo' else self.rustup(arguments, root, environment)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'foreign version or mixed'):
                self.activate(rustup_lookup=lookup)

    def test_missing_toolchain_refuses_despite_present_owned_proxies(self):
        def lookup(arguments, root, environment):
            self.assertEqual(environment['RUSTUP_AUTO_INSTALL'], '0')
            raise ValueError('required exact Rust toolchain is not prepared; run explicit setup')
        with self.assertRaisesRegex(ValueError, 'not prepared'):
            self.activate(rustup_lookup=lookup)

    def test_multiline_and_wrong_executable_output_refuse(self):
        for text in [str(self.native / ('rustc' + self.suffix)) + '\nsecond path', str(self.native / ('cargo' + self.suffix))]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.activate(rustup_lookup=lambda *_: text)

    def test_owned_manager_remains_bound_when_explicitly_required(self):
        result = tools.activation(self.root, [('rust', 'rustup')], lookup=self.mise, rustup_lookup=self.rustup)
        self.assertEqual(Path(shutil.which('rustup', path=result['PATH'])).resolve(), self.manager.resolve())
        self.assertEqual(result['RUSTUP_AUTO_INSTALL'], '0')

    def rewritten_tracking_lookup(self, foreign=False):
        tracking = self.root / 'mise-recorded-rust'
        tracking.symlink_to(self.proxy, target_is_directory=True)
        runtime = self.root / 'selected-runtime-proxies'
        runtime.mkdir()
        for name in ['cargo', 'rustc', 'rustup']:
            (runtime / (name + self.suffix)).symlink_to(self.manager)
        chosen = runtime
        if foreign:
            chosen = self.root / 'foreign-PATH'
            chosen.mkdir()
            path = chosen / ('cargo' + self.suffix)
            shutil.copyfile(sys.executable, path)
            path.chmod(0o755)
        queries = []

        def lookup(arguments, root, environment):
            if arguments[:2] == ['which', '--tool=rust@1.94.0']:
                return str(runtime / (arguments[2] + self.suffix))
            self.assertEqual(root, self.root)
            queries.append(arguments)
            if arguments == ['where', 'rust@1.94.0']:
                return str(tracking)
            self.assertEqual(arguments, ['env', '--json', 'rust@1.94.0'])
            tracking.unlink()
            tracking.symlink_to(runtime, target_is_directory=True)
            self.proxy = runtime
            return json.dumps({'PATH': str(chosen) + os.pathsep + str(runtime),
                'RUSTUP_HOME': str(self.home), 'RUSTUP_TOOLCHAIN': '1.94.0'})

        return lookup, runtime, tracking, queries

    def test_tracking_link_rewritten_by_Mise_env_selects_current_owned_runtime(self):
        lookup, runtime, tracking, queries = self.rewritten_tracking_lookup()
        result = tools.activation(self.root, [('rust', 'cargo'), ('rust', 'rustc')],
                                  lookup=lookup, rustup_lookup=self.rustup)
        self.assertEqual(tracking.resolve(), runtime)
        self.assertEqual(Path(shutil.which('cargo', path=result['PATH'])).parent.resolve(), runtime)
        self.assertEqual(queries, [['where', 'rust@1.94.0'], ['env', '--json', 'rust@1.94.0']])

    def test_foreign_PATH_still_refuses_after_tracking_link_rewrite(self):
        lookup, runtime, tracking, queries = self.rewritten_tracking_lookup(foreign=True)
        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            tools.activation(self.root, [('rust', 'cargo'), ('rust', 'rustc')],
                             lookup=lookup, rustup_lookup=self.rustup)
        self.assertEqual(tracking.resolve(), runtime)
        self.assertFalse(self.rustup_queries)
        self.assertEqual(queries, [['where', 'rust@1.94.0'], ['env', '--json', 'rust@1.94.0']])

    def test_exact_default_pin_overrides_inherited_toolchain_without_install(self):
        result = tools.activation(self.root, [('rust', 'cargo'), ('rust', 'rustc')],
                                  {'RUSTUP_TOOLCHAIN': 'unrelated-inherited-toolchain', 'RUSTUP_AUTO_INSTALL': '1',
                                   'GITHUB_RUN_ID': '41', 'NATIVE_FLAG': 'same'},
                                  lookup=self.mise, rustup_lookup=self.rustup)
        self.assertEqual(result['RUSTUP_TOOLCHAIN'], '1.94.0')
        self.assertEqual(result['RUSTUP_AUTO_INSTALL'], '0')
        self.assertEqual(result['GITHUB_RUN_ID'], '41')
        self.assertEqual(result['NATIVE_FLAG'], 'same')
        self.assertEqual(Path(result['PATH'].split(os.pathsep)[0]), self.proxy)
        self.assertEqual([q[-1] for q in self.rustup_queries], ['rustc', 'cargo'])

    def test_cargo_selector_argv_uses_owned_proxy_without_rewriting(self):
        activation = tools.activation
        command = ['cargo', '+1.94.0', '--version']
        observed = []
        def selected(root, required, environment):
            return activation(root, required, environment, lookup=self.mise, rustup_lookup=self.rustup)
        def run(argv, *, cwd, env):
            observed.append(argv)
            self.assertEqual(cwd, self.root)
            self.assertEqual(argv, command)
            self.assertEqual(Path(shutil.which('cargo', path=env['PATH'])).parent.resolve(), self.proxy)
            self.assertEqual(env['RUSTUP_TOOLCHAIN'], '1.94.0')
            self.assertEqual(env['RUSTUP_AUTO_INSTALL'], '0')
            return SimpleNamespace(returncode=0)
        with patch.object(tools, 'activation', selected), patch.object(tools.subprocess, 'run', run):
            self.assertEqual(tools.execute(self.root, [('rust', 'cargo')], command), 0)
        self.assertEqual(observed, [command])

    def test_explicit_missing_selector_failure_is_returned_without_fallback(self):
        activation = tools.activation
        command = ['cargo', '+uninstalled-explicit-toolchain', '--version']
        observed = []
        def selected(root, required, environment):
            return activation(root, required, environment, lookup=self.mise, rustup_lookup=self.rustup)
        def run(argv, *, cwd, env):
            observed.append(argv)
            self.assertEqual(argv, command)
            self.assertEqual(env['RUSTUP_AUTO_INSTALL'], '0')
            self.assertEqual(Path(shutil.which('cargo', path=env['PATH'])).parent.resolve(), self.proxy)
            return SimpleNamespace(returncode=1)
        with patch.object(tools, 'activation', selected), patch.object(tools.subprocess, 'run', run):
            self.assertEqual(tools.execute(self.root, [('rust', 'cargo')], command), 1)
        self.assertEqual(observed, [command])

    def test_cargo_only_still_refuses_foreign_compiler_proxy_before_metadata(self):
        foreign = self.root / 'foreign-compiler'
        foreign.mkdir()
        executable = foreign / ('rustc' + self.suffix)
        shutil.copyfile(sys.executable, executable)
        executable.chmod(0o755)
        self.path = str(foreign) + os.pathsep + str(self.proxy)
        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            tools.activation(self.root, [('rust', 'cargo')], lookup=self.mise, rustup_lookup=self.rustup)
        self.assertFalse(self.rustup_queries)

    def test_cargo_only_requires_its_owned_compiler_proxy(self):
        (self.proxy / ('rustc' + self.suffix)).unlink()
        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            tools.activation(self.root, [('rust', 'cargo')], lookup=self.mise, rustup_lookup=self.rustup)
        self.assertFalse(self.rustup_queries)

    def test_final_Rust_PATH_guard_refuses_foreign_entrypoint_after_native_proof(self):
        foreign = self.root / 'foreign-final-cargo'
        foreign.mkdir()
        executable = foreign / ('cargo' + self.suffix)
        shutil.copyfile(sys.executable, executable)
        executable.chmod(0o755)
        rustup_activation = tools.rustup_activation
        def changed(*arguments):
            environment, native, manager = rustup_activation(*arguments)
            environment['PATH'] = str(foreign) + os.pathsep + environment['PATH']
            return environment, native, manager
        with patch.object(tools, 'rustup_activation', changed), self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            self.activate()
        self.assertEqual([q[-1] for q in self.rustup_queries], ['rustc', 'cargo'])


if __name__ == '__main__':
    unittest.main()
