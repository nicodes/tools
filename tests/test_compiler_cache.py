import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'cookbook'))
import compiler_cache as cache


class CompilerCaches(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'api/.cargo').mkdir(parents=True)
        for directory in [self.root, self.root / 'api']:
            (directory / 'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.0.1"\n')
            (directory / 'Cargo.lock').write_text('version=4\n')
        (self.root / 'api/.cargo/config.toml').write_text('[build]\nrustflags=["-C", "debuginfo=0"]\n')
        (self.root / 'mise.toml').write_text('[tools]\nrust="1.94.0"\npython="3.13.11"\n')
        self.config = {'components': {'rust': [{'manifest': 'Cargo.toml'},
            {'manifest': 'api/Cargo.toml', 'cwd': 'api', 'target_directory': '.artifacts/api-target'}]}}
        self.environment = {'CARGO_TARGET_DIR': str(self.root / '.artifacts/rust-target'),
                            'CARGO_HOME': str(self.root / '.cargo'), 'GITHUB_JOB': 'native_one'}

    def plan(self, source='a' * 40, target='x86_64-unknown-linux-musl'):
        return cache.plan(self.root, self.config, {}, self.environment, source, 'Linux', 'X64', target)

    def test_only_actual_compiler_and_cargo_download_directories_are_selected(self):
        value = self.plan()
        paths = set(value['paths'])
        self.assertIn(str(self.root / '.artifacts/rust-target'), paths)
        self.assertIn(str(self.root / '.artifacts/api-target'), paths)
        self.assertIn(str(self.root / '.cargo/git/db'), paths)
        self.assertNotIn(str(self.root / '.cargo/credentials.toml'), paths)
        self.assertFalse(any('/verification' in value or '/dist' in value or 'godot' in value for value in paths))
        self.assertEqual(len(paths), 7)

    def test_source_changes_keep_safe_restore_prefix_but_sdk_lock_flags_and_target_change_keys(self):
        original = self.plan()
        next_source = self.plan('b' * 40)
        self.assertNotEqual(original['key'], next_source['key'])
        self.assertEqual(original['restore_keys'], next_source['restore_keys'])
        for change in ['sdk', 'lock', 'flags', 'target', 'job']:
            with self.subTest(change=change):
                old_environment = dict(self.environment)
                if change == 'sdk': (self.root / 'mise.toml').write_text('[tools]\nrust="1.95.0"\n')
                if change == 'lock': (self.root / 'Cargo.lock').write_text('version=4\n# changed dependencies\n')
                if change == 'flags': self.environment['RUSTFLAGS'] = '-C debuginfo=0'
                if change == 'job': self.environment['GITHUB_JOB'] = 'native_two'
                selected = self.plan(target='aarch64-unknown-linux-musl' if change == 'target' else 'x86_64-unknown-linux-musl')
                self.assertNotEqual(original['key'], selected['key'])
                self.environment = old_environment

    def test_cache_paths_cannot_include_product_evidence_or_escape_the_checkout(self):
        for path in ['.artifacts/verification', 'dist', '../outside-target', '.artifacts/*/target']:
            self.config['components']['rust'][0]['target_directory'] = path
            with self.subTest(path=path), self.assertRaises(ValueError): self.plan()
        self.config['components']['rust'][0].pop('target_directory')
        (self.root / '.cargo').symlink_to(self.root / 'api', target_is_directory=True)
        with self.assertRaises(ValueError): self.plan()

    def test_save_is_bounded_and_skips_empty_or_linked_payloads_without_breaking_validation(self):
        directory = self.root / 'target'; directory.mkdir()
        self.assertFalse(cache.within_budget([str(directory)]))
        (directory / 'intermediate').write_bytes(b'compiled')
        self.assertTrue(cache.within_budget([str(directory)]))
        with patch.object(cache, 'MAX_BYTES', 1): self.assertFalse(cache.within_budget([str(directory)]))
        (directory / 'linked').symlink_to(self.root / 'Cargo.lock')
        self.assertFalse(cache.within_budget([str(directory)]))


if __name__ == '__main__': unittest.main()
