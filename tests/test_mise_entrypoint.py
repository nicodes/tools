import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'cookbook'))
import generate
import mise_entrypoint as entry


def future(source, revision='e' * 40, recipe='godot-addon', groups=('lint', 'unit', 'security')):
    lines = ['[tools]', 'python="3.13.11"', '', entry.START.rstrip(), '[settings]',
             'task.run_auto_install=false', '[vars]', 'tools_contract=1',
             'tools_ref="' + revision + '"', 'cookbook_recipe="' + recipe + '"',
             'cookbook_sha256="' + hashlib.sha256(source.encode()).hexdigest() + '"']
    for name in ['_sdk', 'help', 'setup', 'fmt', 'build', 'test', 'clean', *('test:' + name for name in groups)]:
        lines += ['[tasks."' + name + '"]', 'run="source inspection must never execute this"']
    return '\n'.join([*lines, entry.END.rstrip(), ''])


class FutureIdentity(unittest.TestCase):
    source = 'version=1\nrecipe="godot-addon"\n[[tests.unit]]\nrunner="python"\npath="tests/check.py"\n'

    def test_decodes_closed_identity_and_preserves_native_receipt_group_order(self):
        value = entry.decode(future(self.source))
        self.assertEqual(value, {'version': 1, 'revision': 'e' * 40, 'recipe': 'godot-addon',
            'configuration_sha256': hashlib.sha256(self.source.encode()).hexdigest(),
            'test_groups': ['lint', 'security', 'unit'], 'capabilities': []})
        self.assertEqual(entry.matches(value, tomllib.loads(self.source), self.source.encode()), value)

    def test_native_text_newlines_preserve_future_identity_and_raw_configuration_hash(self):
        original = future(self.source)
        digest = hashlib.sha256(self.source.encode()).hexdigest()
        for newline in ['\n', '\r\n', '\r']:
            raw = original.replace('\n', newline).encode()
            before = hashlib.sha256(raw).hexdigest()
            with self.subTest(newline=repr(newline)):
                value = entry.decode(raw)
                self.assertEqual(value['configuration_sha256'], digest)
                self.assertEqual(entry.matches(value, tomllib.loads(self.source), self.source.encode()), value)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), before)
                self.assertEqual(raw.count(digest.encode()), 1)
                changed = raw.replace(digest.encode(), b'f' * 64)
                with self.assertRaises(ValueError):
                    entry.matches(entry.decode(changed), tomllib.loads(self.source), self.source.encode())

    def test_parser_does_not_execute_native_source_or_mise_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'unexpected-execution'
            malicious = "python3 -c " + repr("from pathlib import Path;Path(" + repr(str(marker)) + ").write_text('executed')")
            source = future(self.source).replace('"source inspection must never execute this"', json.dumps(malicious))
            with patch('subprocess.run', side_effect=AssertionError('command executed')), \
                    patch('subprocess.check_output', side_effect=AssertionError('command executed')):
                entry.decode(source)
            self.assertFalse(marker.exists())

    def test_unknown_partial_duplicate_oversize_and_control_overrides_refused(self):
        original = future(self.source)
        mutations = [original.replace('tools_contract=1', 'tools_contract=2'),
                     original.replace('tools_ref="' + 'e' * 40 + '"', 'tools_ref="main"'),
                     original.replace('tools_contract=1', 'tools_contract=1\nunknown_identity=true'),
                     original.replace('tools_contract=1', 'tools_contract=1\ntools_contract=1'),
                     original.replace('task.run_auto_install=false', 'task.run_auto_install=0'),
                     original.replace('task.run_auto_install=false', 'task.run_auto_install=true'),
                     original.replace('[tasks."test:unit"]', '[tasks."undeclared"]'),
                     original.replace('[tasks."_sdk"]', '[tasks."unknown-dispatcher"]'),
                     original.replace('cookbook_recipe="godot-addon"', 'cookbook_recipe="custom"'),
                     original.replace(entry.END, entry.END + '[task_config]\nincludes=["remote"]\n'),
                     original + 'x' * entry.MAX_SOURCE]
        for value in mutations:
            with self.subTest(value=value[:200]), self.assertRaises(ValueError):
                entry.decode(value)

    def test_changed_config_recipe_or_missing_suite_cannot_select_another_contract(self):
        value = entry.decode(future(self.source))
        for override in [{'configuration_sha256': 'f' * 64}, {'recipe': 'rust-cli'},
                         {'test_groups': ['unit']}, {'capabilities': ['dev']}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                entry.matches(value | override, tomllib.loads(self.source), self.source.encode())

    def test_partial_new_identity_is_recognized_so_caller_cannot_fall_back(self):
        self.assertFalse(entry.declared('[tools]\npython="3.13.11"\n'))
        self.assertTrue(entry.declared('[vars]\ntools_contract=1\n'))
        self.assertTrue(entry.declared('[vars]\ncookbook_recipe="godot-addon"\n'))
        self.assertTrue(entry.declared('[tasks.extra]\nrun="never execute"\n'))
        for value in ['[vars]\ntools_contract=1\n', '[vars]\ncookbook_recipe="godot-addon"\n']:
            with self.assertRaises(ValueError):
                entry.decode(value)

    def test_phase_A_entrypoint_and_native_command_remain_the_existing_Make_ABI(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/check.py').write_text('raise RuntimeError("must not execute")\n')
            (root / 'cookbook.toml').write_text(self.source)
            (root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n')
            expected = generate.makefile(root, 'e' * 40)
            self.assertEqual(generate.entrypoint_files(root, 'e' * 40), {'Makefile': expected})
            self.assertEqual(generate.task_command('test:security'), ['make', 'test:security'])
            (root / 'Makefile').write_text(expected)
            generate.check_entrypoint(root, 'e' * 40)
            (root / 'Makefile').write_text(expected + '\n# changed\n')
            with self.assertRaises(ValueError):
                generate.check_entrypoint(root, 'e' * 40)

    def test_phase_A_requires_its_bounded_regular_SDK_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/check.py').write_text('raise RuntimeError("must not execute")\n')
            (root / 'cookbook.toml').write_text(self.source)
            (root / 'Makefile').write_text(generate.makefile(root, 'e' * 40))
            with self.assertRaises(OSError):
                generate.check_entrypoint(root, 'e' * 40)
            (root / 'mise.toml').write_text('[tools]\npython="3.13.11"\n' + '#' * entry.MAX_SOURCE)
            with self.assertRaises(ValueError):
                generate.check_entrypoint(root, 'e' * 40)

    def test_native_text_newlines_preserve_canonical_Make_comparison_and_raw_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/check.py').write_bytes(b'raise RuntimeError("must not execute")\n')
            (root / 'cookbook.toml').write_bytes(self.source.encode())
            (root / 'mise.toml').write_bytes(b'[tools]\npython="3.13.11"\n')
            expected = generate.makefile(root, 'e' * 40)
            for newline in ['\n', '\r\n', '\r']:
                raw = expected.replace('\n', newline).encode()
                (root / 'Makefile').write_bytes(raw)
                with self.subTest(newline=repr(newline)):
                    generate.check_entrypoint(root, 'e' * 40)
                    self.assertEqual((root / 'Makefile').read_bytes(), raw)
                (root / 'Makefile').write_bytes(raw + b'# changed canonical source\n')
                with self.assertRaises(ValueError):
                    generate.check_entrypoint(root, 'e' * 40)

    def test_future_identity_selecting_A_refuses_even_with_its_exact_legacy_Makefile(self):
        import ci
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tests').mkdir()
            (root / 'tests/check.py').write_text('raise RuntimeError("must never execute")\n')
            (root / 'cookbook.toml').write_text(self.source)
            (root / 'Makefile').write_text(generate.makefile(root, 'e' * 40))
            (root / 'mise.toml').write_text(future(self.source))
            with self.assertRaisesRegex(ValueError, 'final Mise SDK'):
                generate.check_entrypoint(root, 'e' * 40)
            with patch.object(ci, 'git', side_effect=lambda path, *args: 'e' * 40), \
                    patch.object(ci.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as native, \
                    self.assertRaisesRegex(ValueError, 'final Mise SDK'):
                ci.identity(root, 'e' * 40, 'e' * 40)
            self.assertEqual(len(native.call_args_list), 2)
            self.assertTrue(all(call.args[0][:4] == ['git', '-C', str(path), 'diff']
                                for call, path in zip(native.call_args_list, [root, ci.TOOLS])))


if __name__ == '__main__':
    unittest.main()
