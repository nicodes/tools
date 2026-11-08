import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('export_web', Path(__file__).parents[1]/'helpers/export-web.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExportFailures(unittest.TestCase):
    def run_export(self, source, timeout=5):
        with tempfile.TemporaryDirectory() as app:
            module.export(app, 'dist', timeout, [sys.executable, '-c', source])

    def test_artifact_cannot_hide_failed_process(self):
        with self.assertRaisesRegex(RuntimeError, 'exited 7'):
            self.run_export("from pathlib import Path; p=Path('dist'); p.mkdir(); (p/'index.html').write_text('ok'); (p/'app.js').write_text('ok'); raise SystemExit(7)")

    def test_timeout_cannot_be_passed_by_partial_export(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_export("import time; from pathlib import Path; p=Path('dist'); p.mkdir(); (p/'index.html').write_text('partial'); time.sleep(30)", 0.1)

    def test_success_requires_javascript(self):
        with self.assertRaisesRegex(RuntimeError, 'JavaScript'):
            self.run_export("from pathlib import Path; p=Path('dist'); p.mkdir(); (p/'index.html').write_text('partial')")

    def test_successful_complete_export(self):
        self.run_export("from pathlib import Path; p=Path('dist'); p.mkdir(); (p/'index.html').write_text('ok'); (p/'app.js').write_text('ok')")

    def test_named_disposable_export_runs_real_compiler_and_binds_its_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            app=Path(directory)
            module.export(app,'dist-local-fixture',5,[sys.executable,'-c',
                "from pathlib import Path; p=Path('dist-local-fixture'); p.mkdir(); (p/'index.html').write_text('ok'); (p/'app.js').write_text('bundle')"])
            import json,hashlib
            values=json.loads((app/'dist-local-fixture/export-manifest.json').read_text())
            self.assertEqual(values['app.js'],hashlib.sha256(b'bundle').hexdigest())
            for name in ['../dist','dist-/escape','dist-*','dist-.git']:
                with self.subTest(name=name),self.assertRaises(ValueError):
                    module.export(app,name,5,[sys.executable,'-c',"raise RuntimeError('must not execute')"])


if __name__ == '__main__':
    unittest.main()

class CompilerCacheBoundaries(unittest.TestCase):
    def test_current_mise_pin_changes_select_fresh_compiler_namespaces(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root)
            app = source/'app'; app.mkdir()
            cache = source/'cache'
            pins = source/'mise.toml'
            pins.write_text('[tools]\nnode="24.18.1"\n')
            with module.compiler_cache(app, 'dist', cache) as env:
                before = env['TMPDIR']
            pins.write_text('[tools]\nnode="26.7.0"\n')
            with module.compiler_cache(app, 'dist', cache) as env:
                self.assertNotEqual(env['TMPDIR'], before)
            (source/'.mise.toml').write_text(pins.read_text())
            with self.assertRaisesRegex(ValueError, 'one Mise'):
                with module.compiler_cache(app, 'dist', cache): pass

    def test_variant_isolation_and_corrupt_transform_refusal(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            app = Path(root)/'app'; app.mkdir()
            cache = Path(root)/'cache'
            with patch.dict(os.environ, {'EXPO_PUBLIC_API_URL': '/api'}, clear=True):
                with module.compiler_cache(app, 'dist', cache) as env:
                    production = Path(env['TMPDIR']); (production/'metro-cache').mkdir()
                    (production/'metro-cache/transform').write_bytes(b'reviewed')
                with module.compiler_cache(app, 'dist', cache) as env:
                    self.assertEqual(Path(env['TMPDIR']), production)
                with patch.dict(os.environ, {'ENGINEERING_E2E_AUTH': '1'}):
                    with module.compiler_cache(app, 'dist-e2e', cache) as env:
                        self.assertNotEqual(Path(env['TMPDIR']), production)
                (production/'metro-cache/transform').write_bytes(b'corrupt')
                with self.assertRaisesRegex(ValueError, 'successful export inventory'):
                    with module.compiler_cache(app, 'dist', cache): pass

    def test_failed_compile_does_not_leave_success_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            app = Path(root)/'app'; app.mkdir()
            cache = Path(root)/'cache'
            with self.assertRaises(RuntimeError):
                with module.compiler_cache(app, 'dist', cache) as env:
                    namespace = Path(env['TMPDIR'])
                    raise RuntimeError('compile failed')
            self.assertFalse((namespace/'state.json').exists())

    def test_declared_product_modes_select_fresh_namespaces_and_dotenv_is_refused(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            app = Path(root)/'app'; app.mkdir()
            cache = Path(root)/'cache'
            with patch.dict(os.environ, {'FLEET_WEB_CACHE_ENV': 'APP_MODE', 'APP_MODE': 'production'}, clear=True):
                with module.compiler_cache(app, 'dist', cache) as env:
                    production = env['TMPDIR']
                with patch.dict(os.environ, {'APP_MODE': 'local', 'EXPO_NO_DOTENV': '1'}):
                    with module.compiler_cache(app, 'dist', cache) as env:
                        self.assertNotEqual(env['TMPDIR'], production)
                (app/'.env.production').write_text('EXPO_PUBLIC_API_URL=https://new.invalid')
                with self.assertRaisesRegex(ValueError, 'dotenv inputs are not supported'):
                    with module.compiler_cache(app, 'dist', cache): pass
                (app/'.env.production').unlink()
                with patch.dict(os.environ, {'FLEET_WEB_CACHE_ENV': 'bad-name'}):
                    with self.assertRaisesRegex(ValueError, 'variable names'):
                        with module.compiler_cache(app, 'dist', cache): pass


class InjectedExportEnvironment(unittest.TestCase):
    def test_dotenv_is_refused_before_reading_deleting_outputs_or_creating_cache(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)/'app'; app.mkdir()
            (app/'.env.example.local').write_text('PUBLIC_FLAG=fixture-only\n')
            output = app/'dist'; output.mkdir(); (output/'index.html').write_text('retained')
            cache = Path(directory)/'cache'
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('dotenv content read')), \
                    patch.object(module.subprocess, 'Popen', side_effect=AssertionError('compiler executed')):
                with self.assertRaisesRegex(ValueError, 'dotenv inputs are not supported'):
                    module.export(app, 'dist', cache_root=cache)
                with self.assertRaisesRegex(ValueError, 'dotenv inputs are not supported'):
                    with module.compiler_cache(app, 'dist', cache): pass
            self.assertFalse(cache.exists())
            self.assertEqual((output/'index.html').read_text(), 'retained')

    def test_dangling_dotenv_link_is_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            (app/'.env').symlink_to(app/'absent')
            with self.assertRaisesRegex(ValueError, 'dotenv inputs are not supported'):
                module.export(app, 'dist')

    def test_default_bun_argv_and_actual_child_environment_disable_dotenv(self):
        import json
        import os
        from unittest.mock import patch
        original = module.subprocess.Popen
        observed = []
        child = ("import json,os; from pathlib import Path; p=Path('dist'); p.mkdir(); "
                 "(p/'index.html').write_text('ok'); (p/'app.js').write_text('ok'); "
                 "(p/'child.json').write_text(json.dumps({'dotenv':os.environ['EXPO_NO_DOTENV']}))")
        def launch(command, **kwargs):
            observed.append(command)
            return original([sys.executable, '-c', child], **kwargs)
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'EXPO_NO_DOTENV':'0'}, clear=True), \
                patch.object(module.subprocess, 'Popen', side_effect=launch):
            module.export(directory, 'dist')
            self.assertEqual(observed[0][:5], ['bun','--no-env-file','x','--no-install','expo'])
            self.assertEqual(json.loads((Path(directory)/'dist/child.json').read_text()), {'dotenv':'1'})

    def test_explicit_bun_command_cannot_enable_a_dotenv_file(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(module.subprocess, 'Popen', side_effect=AssertionError('compiler executed')):
            for argument in ['--env-file', '--env-file=outside', '--no-env-file=false']:
                with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, 'Bun dotenv command'):
                    module.export(directory, 'dist', command=['bun',argument,'x','expo'])

    def test_effective_no_dotenv_flag_selects_one_cache_namespace(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)/'app'; app.mkdir()
            cache = Path(directory)/'cache'
            with patch.dict(os.environ, {'EXPO_NO_DOTENV':'0'}, clear=True):
                with module.compiler_cache(app, 'dist', cache) as environment:
                    before = environment['TMPDIR']
            with patch.dict(os.environ, {'EXPO_NO_DOTENV':'1'}, clear=True):
                with module.compiler_cache(app, 'dist', cache) as environment:
                    self.assertEqual(environment['TMPDIR'], before)
