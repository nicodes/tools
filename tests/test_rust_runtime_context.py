"""Finite source-bound controls for noninstalling SDK metadata selection."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('existing_sdk_fixtures', Path(__file__).with_name('test_pinned_tools.py'))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
tools = fixtures.tools


class RustRuntimeContext(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OwnedRustupExecution()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.root / 'current-runtime'
        self.runtime.mkdir()
        for name in ['cargo', 'rustc', 'rustup']:
            (self.runtime / (name + self.fixture.suffix)).symlink_to(self.fixture.manager)
        self.queries = []

    def lookup(self, arguments, root, environment):
        self.queries.append(arguments)
        if arguments[0] == 'where':
            return str(self.fixture.proxy)
        if arguments[0] == 'which':
            self.assertEqual(arguments[:2], ['which', '--tool=rust@1.94.0'])
            self.assertEqual(environment['MISE_ENABLE_TOOLS'], 'rust')
            return str(self.runtime / (arguments[-1] + self.fixture.suffix))
        self.assertEqual(arguments, ['env', '--json', 'rust@1.94.0'])
        return json.dumps({'PATH': str(self.runtime), 'RUSTUP_HOME': str(self.fixture.home)})

    def native(self, arguments, root, environment):
        self.assertEqual(Path(arguments[0]).parent.resolve(), self.runtime)
        self.assertEqual(environment['RUSTUP_AUTO_INSTALL'], '0')
        self.assertEqual(environment['RUSTUP_TOOLCHAIN'], '1.94.0')
        return str(self.fixture.native / (arguments[-1] + self.fixture.suffix))

    def test_current_runtime_owns_all_proxies_despite_unchanged_external_record(self):
        result = tools.activation(self.fixture.root, [('rust', 'cargo')], {},
                                  lookup=self.lookup, rustup_lookup=self.native)
        self.assertNotEqual(self.fixture.proxy, self.runtime)
        self.assertEqual(Path(shutil.which('cargo', path=result['PATH'])).parent.resolve(), self.runtime)
        self.assertEqual(self.queries[2:], [['which', '--tool=rust@1.94.0', name]
                                          for name in ['rustc', 'rustup', 'cargo']])

    def test_runtime_queries_keep_input_homes_rather_than_new_exports(self):
        def lookup(arguments, root, environment):
            self.assertNotIn('RUSTUP_HOME', environment)
            return self.lookup(arguments, root, environment)
        result = tools.activation(self.fixture.root, [('rust', 'cargo')], {},
                                  lookup=lookup, rustup_lookup=self.native)
        self.assertEqual(result['RUSTUP_HOME'], str(self.fixture.home))

    def test_foreign_PATH_refuses_before_Rustup_despite_valid_runtime_metadata(self):
        def lookup(arguments, root, environment):
            value = self.lookup(arguments, root, environment)
            if arguments[0] == 'env':
                return json.dumps({'PATH': str(self.fixture.proxy) + os.pathsep + str(self.runtime),
                                   'RUSTUP_HOME': str(self.fixture.home)})
            return value
        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            tools.activation(self.fixture.root, [('rust', 'cargo')], {}, lookup=lookup,
                             rustup_lookup=lambda *_: self.fail('foreign PATH reached Rustup'))

    def test_mixed_runtime_provider_roots_refuse(self):
        def lookup(arguments, root, environment):
            value = self.lookup(arguments, root, environment)
            return str(self.fixture.proxy / ('rustup' + self.fixture.suffix)) if arguments[-1] == 'rustup' else value
        with self.assertRaisesRegex(ValueError, 'mixed installed SDK proxy roots'):
            tools.activation(self.fixture.root, [('rust', 'cargo')], {}, lookup=lookup,
                             rustup_lookup=lambda *_: self.fail('mixed proxies reached Rustup'))

    def test_missing_current_proxy_cannot_fall_back_to_recorded_external_install(self):
        (self.runtime / ('rustc' + self.fixture.suffix)).unlink()
        with self.assertRaisesRegex(ValueError, 'exact installed SDK'):
            tools.activation(self.fixture.root, [('rust', 'cargo')], {}, lookup=self.lookup,
                             rustup_lookup=lambda *_: self.fail('missing proxy reached Rustup'))

    def test_ambiguous_relative_or_wrong_runtime_outputs_refuse(self):
        for value in ['relative/rustc', str(self.runtime / ('rustc' + self.fixture.suffix)) + '\nsecond',
                      str(self.runtime / ('cargo' + self.fixture.suffix)), '']:
            def lookup(arguments, root, environment):
                return value if arguments[0] == 'which' else self.lookup(arguments, root, environment)
            with self.subTest(value=value), self.assertRaises(ValueError):
                tools.activation(self.fixture.root, [('rust', 'cargo')], {}, lookup=lookup,
                                 rustup_lookup=lambda *_: self.fail('invalid metadata reached Rustup'))

    def test_query_only_allowlist_and_dispatch_metadata_restore_exact_inheritance(self):
        keys = ['MISE_ENABLE_TOOLS', '__MISE_DIFF', '__MISE_ENV_CACHE_KEY', '__MISE_TASK_TOOL_ARGS']
        for inherited in [{}, {key: 'original-' + key for key in keys}]:
            def lookup(arguments, root, environment):
                self.assertEqual(environment['MISE_ENABLE_TOOLS'], 'rust')
                value = self.lookup(arguments, root, environment)
                return json.dumps({**json.loads(value), **{key: 'query-only' for key in keys}}) if arguments[0] == 'env' else value
            result = tools.activation(self.fixture.root, [('rust', 'cargo')], inherited,
                                      lookup=lookup, rustup_lookup=self.native)
            for key in keys:
                if key in inherited:
                    self.assertEqual(result[key], inherited[key])
                else:
                    self.assertNotIn(key, result)

    def test_environment_query_uses_noninstalling_fresh_no_deps_absolute_interpreter(self):
        observed = []
        def run(arguments, **options):
            observed.append(arguments)
            self.assertEqual(options['env']['MISE_EXEC_AUTO_INSTALL'], 'false')
            self.assertEqual(arguments[:5], ['mise', 'exec', '--no-deps', '--fresh-env', 'rust@1.94.0'])
            self.assertEqual(arguments[5:10], ['--', tools.sys.executable, '-I', '-B', '-c'])
            return SimpleNamespace(returncode=0, stdout='{}')
        with patch.object(tools.subprocess, 'run', run):
            self.assertEqual(tools.query(['env', '--json', 'rust@1.94.0'], self.fixture.root,
                                        tools.DISABLED), '{}')
        self.assertEqual(len(observed), 1)


if __name__ == '__main__':
    unittest.main()
