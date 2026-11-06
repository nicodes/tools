"""Keep authored imports and invalidate only their stale generated editor index."""
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('authoring_cache', ROOT / 'helpers/godot_authoring.py')
authoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(authoring)
BEFORE = b'[remap]\nuid="fixture"\n[params]\ncompress/mode=0\n'
AFTER = BEFORE + b'compress/uastc_level=0\nprocess/channel_remap/red=0\n'


class EditorCacheControls(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.source = self.project / 'fixture.png.import'
        self.source.write_bytes(BEFORE)
        self.cache = self.project / '.godot/editor/filesystem_cache10'
        self.cache.parent.mkdir(parents=True)
        self.cache.write_bytes(b'generated index')

    def changed(self, result=None):
        self.source.write_bytes(AFTER)
        return result

    def test_actual_restore_only_discards_exact_cache_and_preserves_result_and_siblings(self):
        siblings = [self.cache.with_name('filesystem_cache9'), self.cache.with_name('filesystem_cache10.other'),
                    self.cache.with_name('script_editor_cache.cfg'), self.project / '.godot/imported/fixture.md5']
        for path in siblings:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'foreign sibling bytes')
        result = subprocess.CompletedProcess(['native-editor'], 7)
        self.assertIs(authoring.preserve_imports(self.project, lambda: self.changed(result)), result)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.source.read_bytes(), BEFORE)
        self.assertFalse(self.cache.exists())
        for path in siblings:
            self.assertEqual(path.read_bytes(), b'foreign sibling bytes')

    def test_no_authored_change_keeps_cache_and_callback_result(self):
        result = object()
        with patch.object(authoring, 'editor_filesystem_cache', side_effect=AssertionError('cache inspected without restoration')):
            self.assertIs(authoring.preserve_imports(self.project, lambda: result), result)
        self.assertEqual(self.cache.read_bytes(), b'generated index')

    def test_missing_cache_or_cache_directories_are_not_created(self):
        for absent in [self.cache, self.cache.parent, self.project / '.godot']:
            with self.subTest(absent=absent):
                if absent.is_dir(): shutil.rmtree(absent)
                elif absent.exists(): absent.unlink()
                self.source.write_bytes(BEFORE)
                self.assertIsNone(authoring.preserve_imports(self.project, self.changed))
                self.assertEqual(self.source.read_bytes(), BEFORE)
                self.assertFalse(absent.exists())

    def test_callback_exception_remains_identical_without_restoration_or_cache_effects(self):
        error = RuntimeError('native callback failure')
        def execute():
            self.changed()
            raise error
        with self.assertRaises(RuntimeError) as raised:
            authoring.preserve_imports(self.project, execute)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.source.read_bytes(), AFTER)
        self.assertEqual(self.cache.read_bytes(), b'generated index')

    def test_real_authored_change_refuses_before_any_restore_or_cache_effect(self):
        other = self.project / 'other.png.import'
        other.write_bytes(BEFORE)
        for invalid in [AFTER + b'unknown=true\n', AFTER.replace(b'mode=0', b'mode=1'), BEFORE.replace(b'fixture', b'changed')]:
            with self.subTest(invalid=invalid):
                self.source.write_bytes(BEFORE)
                other.write_bytes(BEFORE)
                def execute():
                    self.source.write_bytes(AFTER)
                    other.write_bytes(invalid)
                with self.assertRaisesRegex(ValueError, 'authored import settings'):
                    authoring.preserve_imports(self.project, execute)
                self.assertEqual(self.source.read_bytes(), AFTER)
                self.assertEqual(other.read_bytes(), invalid)
                self.assertEqual(self.cache.read_bytes(), b'generated index')

    def test_symlinked_cache_refuses_without_touching_target_or_authored_bytes(self):
        target = self.root / 'foreign'
        target.write_bytes(b'foreign bytes')
        self.cache.unlink()
        try: self.cache.symlink_to(target)
        except (OSError, NotImplementedError) as error: self.skipTest(str(error))
        with self.assertRaisesRegex(ValueError, 'regular file'):
            authoring.preserve_imports(self.project, self.changed)
        self.assertEqual(target.read_bytes(), b'foreign bytes')
        self.assertEqual(self.source.read_bytes(), AFTER)
        self.assertTrue(self.cache.is_symlink())

    def test_hardlinked_cache_refuses_without_touching_either_link(self):
        sibling = self.root / 'foreign'
        try: os.link(self.cache, sibling)
        except (OSError, NotImplementedError) as error: self.skipTest(str(error))
        with self.assertRaisesRegex(ValueError, 'regular file'):
            authoring.preserve_imports(self.project, self.changed)
        self.assertEqual(self.source.read_bytes(), AFTER)
        self.assertEqual(sibling.read_bytes(), b'generated index')
        self.assertTrue(self.cache.exists())

    def test_linked_cache_parent_refuses_without_following_it(self):
        for directory in [self.project / '.godot', self.cache.parent]:
            with self.subTest(directory=directory):
                self.source.write_bytes(BEFORE)
                foreign = self.root / ('foreign-' + directory.name)
                shutil.move(str(directory), foreign)
                try: directory.symlink_to(foreign, target_is_directory=True)
                except (OSError, NotImplementedError) as error:
                    shutil.move(str(foreign), directory)
                    self.skipTest(str(error))
                try:
                    with self.assertRaisesRegex(ValueError, 'unlinked project directories'):
                        authoring.preserve_imports(self.project, self.changed)
                    self.assertTrue(directory.is_symlink())
                    self.assertEqual(self.cache.read_bytes(), b'generated index')
                    self.assertEqual(self.source.read_bytes(), AFTER)
                finally:
                    directory.unlink()
                    shutil.move(str(foreign), directory)

    def test_nonregular_or_oversized_cache_refuses_before_restoring_source(self):
        self.cache.unlink()
        self.cache.mkdir()
        with self.assertRaisesRegex(ValueError, 'regular file'):
            authoring.preserve_imports(self.project, self.changed)
        self.cache.rmdir()
        self.source.write_bytes(BEFORE)
        with self.cache.open('wb') as stream: stream.truncate(authoring.MAX_FILESYSTEM_CACHE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            authoring.preserve_imports(self.project, self.changed)
        self.assertEqual(self.source.read_bytes(), AFTER)
        self.assertEqual(self.cache.stat().st_size, authoring.MAX_FILESYSTEM_CACHE_BYTES + 1)

    def test_non_directory_cache_parent_refuses_without_replacing_it(self):
        saved = self.root / 'saved-generated-cache'
        shutil.move(str(self.project / '.godot'), saved)
        (self.project / '.godot').write_bytes(b'foreign parent file')
        with self.assertRaisesRegex(ValueError, 'unlinked project directories'):
            authoring.preserve_imports(self.project, self.changed)
        self.assertEqual((self.project / '.godot').read_bytes(), b'foreign parent file')
        self.assertEqual((saved / 'editor/filesystem_cache10').read_bytes(), b'generated index')
        self.assertEqual(self.source.read_bytes(), AFTER)

    def test_project_replaced_with_link_by_callback_refuses_without_following_it(self):
        saved = self.root / 'moved-project'
        def execute():
            self.changed()
            shutil.move(str(self.project), saved)
            try: self.project.symlink_to(saved, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                shutil.move(str(saved), self.project)
                self.skipTest(str(error))
        with self.assertRaisesRegex(ValueError, 'unlinked project directories'):
            authoring.preserve_imports(self.project, execute)
        self.assertEqual((saved / 'fixture.png.import').read_bytes(), AFTER)
        self.assertEqual((saved / '.godot/editor/filesystem_cache10').read_bytes(), b'generated index')
        self.assertTrue(self.project.is_symlink())

    def test_callback_linking_a_distant_project_ancestor_cannot_unlink_external_cache(self):
        nested = self.root / 'one/two/project'
        nested.parent.mkdir(parents=True)
        shutil.move(str(self.project), nested)
        self.project = nested
        self.source = nested / 'fixture.png.import'
        saved = self.root.with_name(self.root.name + '-moved')
        def execute():
            self.changed()
            self.root.rename(saved)
            try: self.root.symlink_to(saved, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                saved.rename(self.root)
                self.skipTest(str(error))
        try:
            with self.assertRaisesRegex(ValueError, 'unlinked project directories'):
                authoring.preserve_imports(self.project, execute)
            self.assertTrue(self.root.is_symlink())
            self.assertEqual((saved / 'one/two/project/fixture.png.import').read_bytes(), AFTER)
            self.assertEqual((saved / 'one/two/project/.godot/editor/filesystem_cache10').read_bytes(), b'generated index')
        finally:
            if self.root.is_symlink():
                self.root.unlink()
                saved.rename(self.root)

    def test_changed_cache_identity_is_refused_before_unlinking_replacement(self):
        original = authoring.editor_filesystem_cache
        calls = 0
        def inspect(project):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.cache.unlink()
                self.cache.write_bytes(b'foreign replacement index')
            return original(project)
        with patch.object(authoring, 'editor_filesystem_cache', side_effect=inspect):
            with self.assertRaisesRegex(ValueError, 'changed during import restoration'):
                authoring.preserve_imports(self.project, self.changed)
        self.assertEqual(self.source.read_bytes(), BEFORE)
        self.assertEqual(self.cache.read_bytes(), b'foreign replacement index')

    def test_old_restoration_without_invalidation_leaves_stale_cache_counterfactual(self):
        with patch.object(authoring, 'editor_filesystem_cache', return_value=None):
            authoring.preserve_imports(self.project, self.changed)
        self.assertEqual(self.source.read_bytes(), BEFORE)
        self.assertEqual(self.cache.read_bytes(), b'generated index')


@unittest.skipUnless(os.environ.get('GODOT_BIN'), 'CI prepares the pinned engine for real native import controls')
class NativeEditorCacheWitness(unittest.TestCase):
    def test_pinned_synthetic_PNG_import_has_no_redundant_next_editor_import(self):
        engine = os.environ['GODOT_BIN']
        self.assertEqual(subprocess.check_output([engine, '--version'], text=True, timeout=10).strip(),
                         '4.7.2.stable.official.ed1daf0bf')
        with tempfile.TemporaryDirectory(prefix='native-editor-cache-') as directory:
            root = Path(directory)
            base = root / 'base'
            base.mkdir()
            (base / 'project.godot').write_text('config_version=5\n[application]\nconfig/name="Import cache fixture"\n'
                '[rendering]\nrenderer/rendering_method="gl_compatibility"\n[editor]\nimport/use_multiple_threads=false\n')
            def chunk(kind, body):
                return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
            pixels = b''.join(b'\0' + bytes([255, 32, 64, 255]) * 16 for _ in range(16))
            png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 16, 16, 8, 6, 0, 0, 0))
            png += chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')
            (base / 'fixture.png').write_bytes(png)
            env = dict(os.environ, GODOT_SILENCE_ROOT_WARNING='1', XDG_DATA_HOME=str(root / 'data'),
                       XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'))
            def execute(project):
                result = subprocess.run([engine, '--headless', '--editor', '--import', '--quit', '--path', str(project)],
                    env=env, text=True, capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('ERROR:', result.stdout + result.stderr)
                return result
            def prepare_stale_index(project):
                # The synthetic counterfactual needs a stale index even within one clock second.
                cache = project / '.godot/editor/filesystem_cache10'
                rows = cache.read_bytes().splitlines(keepends=True)
                selected, = [index for index, row in enumerate(rows) if row.startswith(b'fixture.png::')]
                fields = rows[selected].split(b'::')
                self.assertEqual(len(fields), 9)
                self.assertEqual(fields[1], b'CompressedTexture2D')
                self.assertTrue(fields[3].isdigit() and fields[4].isdigit())
                self.assertEqual(fields[5], b'1')
                sidecar_second = (project / 'fixture.png.import').stat().st_mtime_ns // 1_000_000_000
                self.assertGreater(sidecar_second, 0)
                fields[4] = str(sidecar_second - 1).encode()
                rows[selected] = b'::'.join(fields)
                cache.write_bytes(b''.join(rows))
            def execute_with_stale_index(project):
                result = execute(project)
                prepare_stale_index(project)
                return result
            execute(base)
            sidecar = base / 'fixture.png.import'
            complete = sidecar.read_bytes()
            authored = b''.join(line for line in complete.splitlines(keepends=True)
                if line.rstrip(b'\n').decode() not in authoring.DEFAULT_INSERTIONS)
            self.assertNotEqual(authored, complete)
            self.assertTrue(authoring.default_insertions_only(authored, complete))
            sidecar.write_bytes(authored)
            self.assertTrue((base / '.godot/editor/filesystem_cache10').is_file())
            prepare_stale_index(base)
            def imported(project):
                return {p.relative_to(project).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in (project / '.godot/imported').rglob('*') if p.is_file()}
            for counterfactual in [True, False]:
                project = root / ('old' if counterfactual else 'new')
                shutil.copytree(base, project)
                if counterfactual:
                    with patch.object(authoring, 'editor_filesystem_cache', return_value=None):
                        authoring.preserve_imports(project, lambda: execute_with_stale_index(project))
                else:
                    authoring.preserve_imports(project, lambda: execute_with_stale_index(project))
                self.assertEqual((project / 'fixture.png.import').read_bytes(), authored)
                self.assertEqual((project / '.godot/editor/filesystem_cache10').exists(), counterfactual)
                before = imported(project)
                self.assertTrue(before)
                execute(project)
                if counterfactual:
                    self.assertNotEqual((project / 'fixture.png.import').read_bytes(), authored)
                    self.assertNotEqual(imported(project), before)
                else:
                    self.assertEqual((project / 'fixture.png.import').read_bytes(), authored)
                    self.assertEqual(imported(project), before)
        print('NATIVE_EDITOR_CACHE_COHERENCE_OK', flush=True)


if __name__ == '__main__': unittest.main()
