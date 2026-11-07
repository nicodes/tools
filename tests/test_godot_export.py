import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('godot_export', Path(__file__).resolve().parents[1]/'helpers/godot-export.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ExportIsolation(unittest.TestCase):
    def test_engine_mutations_do_not_change_authored_project_and_real_theme_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)/'source'; project.mkdir()
            config = b'[gui]\ntheme/custom="res://theme.tres"\n'
            (project/'project.godot').write_bytes(config)
            (project/'asset.import').write_bytes(b'authored settings')
            (project/'theme.tres').write_bytes(b'authored theme')
            output = Path(directory)/'output/index.html'
            calls = []
            def run(command):
                copied = Path(command[command.index('--path')+1]); calls.append(command)
                (copied/'asset.import').write_bytes(b'engine normalization')
                if len(calls) == 1: self.assertIn(b'theme/custom=""', (copied/'project.godot').read_bytes())
                elif '--import' in command:
                    self.assertEqual((copied/'project.godot').read_bytes(), config + b'\n[editor]\nimport/use_multiple_threads=false\n')
                else: self.assertEqual((copied/'project.godot').read_bytes(), config)
                if '--export-release' in command: Path(command[-1]).write_bytes(b'export')
            helper.export(project, [('Web', output)], run=run)
            self.assertEqual((project/'asset.import').read_bytes(), b'authored settings')
            self.assertEqual((project/'project.godot').read_bytes(), config)
            self.assertEqual(output.read_bytes(), b'export')
            self.assertFalse(Path(calls[0][calls[0].index('--path')+1]).exists())

    def test_zero_exit_script_errors_fail(self):
        from unittest.mock import patch
        import subprocess
        with patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ERROR: invalid asset\n')):
            with self.assertRaises(RuntimeError): helper.checked(['godot'])

    def test_debug_and_linux_exports_keep_distinct_native_modes_and_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'app'
            source.mkdir()
            (source / 'project.godot').write_text('[application]\nconfig/name="fixture"\n')
            debug = root / 'debug/index.html'
            linux = root / 'linux/game.x86_64'
            commands = []
            def run(command):
                commands.append(command)
                if '--export-debug' in command or '--export-release' in command:
                    Path(command[-1]).write_bytes(b'native export')
            helper.export(source, [('Web', debug, 'debug'), ('Linux', linux, 'release')], run=run)
            self.assertEqual(commands[-2][-3:], ['--export-debug', 'Web', str(debug)])
            self.assertEqual(commands[-1][-3:], ['--export-release', 'Linux', str(linux)])
            self.assertTrue(debug.is_file())
            self.assertTrue(linux.is_file())

    def test_invalid_or_conflicting_export_identity_never_starts_the_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'project.godot').write_text('[application]\n')
            for presets in [[('Web', root / 'out', 'unknown')], [('Web', root / 'out'), ('Linux', root / 'out')]]:
                commands = []
                with self.assertRaises(ValueError):
                    helper.export(root, presets, run=commands.append)
                self.assertEqual(commands, [])

    def test_native_import_flags_and_distinct_deadlines_preserve_existing_controls(self):
        from unittest.mock import patch
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'project.godot').write_text('[application]\n')
            with patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '')) as execute:
                helper.export(root, [('Web', root / 'out/index.html')],
                              import_arguments=['--frame-delay', '1000'], timeout=1200, import_timeout=90)
            self.assertEqual([call.kwargs['timeout'] for call in execute.call_args_list], [90, 1200])
            self.assertEqual(execute.call_args_list[0].args[0][-2:], ['--frame-delay', '1000'])
            for arguments in [['--export-release', 'Web'], ['--script', 'hidden-build.gd'], ['--frame-delay']]:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    helper.export(root, [('Web', root / 'out/index.html')], import_arguments=arguments)
