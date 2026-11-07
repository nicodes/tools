"""Fixture shell selection must not let Windows System32 shadow Git Bash."""
from pathlib import Path
import tempfile
import unittest

from native_shell import bash


class NativeShellSelection(unittest.TestCase):
    def test_windows_selects_the_native_Git_Bash_even_if_bare_bash_names_WSL(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'Git/cmd').mkdir(parents=True)
            (root/'Git/bin').mkdir()
            git = root/'Git/cmd/git.exe'
            git.write_bytes(b'finite Git path fixture')
            native = root/'Git/bin/bash.exe'
            native.write_bytes(b'finite native Bash path fixture')
            calls = []
            def lookup(name, **_options):
                calls.append(name)
                return str(git) if name == 'git' else str(root/'Windows/System32/bash.exe')
            self.assertEqual(bash({},system='Windows',lookup=lookup),str(native.resolve()))
            self.assertEqual(calls,['git'])

    def test_missing_native_Git_shell_refuses_instead_of_using_another_bash(self):
        with tempfile.TemporaryDirectory() as directory:
            git = Path(directory)/'cmd/git.exe'
            git.parent.mkdir()
            git.write_bytes(b'finite Git path fixture')
            with self.assertRaisesRegex(ValueError,'no Git Bash executable'):
                bash({},system='Windows',lookup=lambda *_args,**_options:str(git))


if __name__ == '__main__':
    unittest.main()
