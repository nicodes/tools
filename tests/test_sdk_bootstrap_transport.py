"""The compact bootstrap transports exactly its readable, reviewed source."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch
import zlib

import test_sdk_bootstrap as original_controls
from native_shell import bash

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('compact_bootstrap_control', ROOT / 'helpers/sdk-bootstrap.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapTransport(unittest.TestCase):
    def test_fixed_payload_is_byte_exact_readable_seed_and_one_short_native_command(self):
        decoded = zlib.decompress(base64.b64decode(bootstrap.ENCODED_SEED, validate=True))
        self.assertEqual(decoded, bootstrap.SEED.encode('utf-8'))
        self.assertEqual(len(decoded), bootstrap.SEED_LENGTH)
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), bootstrap.SEED_SHA256)
        for pinned in [False, True]:
            command = bootstrap.command('a' * 40, pinned_python=pinned)
            self.assertNotIn('\n', command)
            self.assertNotIn('\r', command)
            self.assertLess(len(command.encode('ascii')), 6000)
            argv = shlex.split(command)
            self.assertEqual(argv[:5], ['exec', 'python3', '-I', '-B', '-c'])
            self.assertEqual(argv[-2:], ['a' * 40, 'pinned' if pinned else 'host'])
            compile(argv[5], '<bounded bootstrap control>', 'exec')

    def test_rendering_never_uses_a_platform_dependent_encoder(self):
        expected = bootstrap.command('b' * 40)
        with (patch.object(bootstrap.zlib, 'compress', side_effect=AssertionError('runtime compression')),
                patch.object(bootstrap.zlib, 'ZLIB_VERSION', 'different encoder version')):
            self.assertEqual(bootstrap.command('b' * 40), expected)
        with patch.object(bootstrap, 'SEED', bootstrap.SEED + '\n'), self.assertRaisesRegex(ValueError, 'integrity'):
            bootstrap.command('b' * 40)

    def test_source_renderer_refuses_corrupt_truncated_trailing_or_wrong_identity_payloads(self):
        compressed = base64.b64decode(bootstrap.ENCODED_SEED)
        for changes in [
            {'encoded': bootstrap.ENCODED_SEED[:-4]},
            {'encoded': base64.b64encode(compressed + b'foreign trailing data').decode()},
            {'encoded': '!' + bootstrap.ENCODED_SEED[1:]},
            {'length': bootstrap.SEED_LENGTH - 1},
            {'sha256': '0' * 64},
        ]:
            arguments = {'encoded': bootstrap.ENCODED_SEED, 'length': bootstrap.SEED_LENGTH,
                         'sha256': bootstrap.SEED_SHA256, **changes}
            with self.subTest(changes=changes.keys()), self.assertRaises(ValueError):
                bootstrap.seed_transport(bootstrap.SEED, **arguments)

    def test_runtime_tamper_truncation_trailing_and_oversize_refuse_before_any_SDK_code(self):
        fixture = original_controls.StandaloneToolsBootstrap(
            'test_clean_exact_Tools_exec_uses_isolated_Python_sibling_import_and_original_argv')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = bootstrap.command(fixture.ref)
        compressed = base64.b64decode(bootstrap.ENCODED_SEED)
        expanded = zlib.compress(bootstrap.SEED.encode() + b'X' * 65536, 9)
        mutations = {
            'truncated': command.replace(bootstrap.ENCODED_SEED, bootstrap.ENCODED_SEED[:-4]),
            'tampered': command.replace(bootstrap.ENCODED_SEED, '!' + bootstrap.ENCODED_SEED[1:]),
            'trailing': command.replace(bootstrap.ENCODED_SEED,
                base64.b64encode(compressed + b'foreign trailing data').decode()),
            'oversize': command.replace(bootstrap.ENCODED_SEED, base64.b64encode(expanded).decode()),
            'wrong-size': command.replace('len(_sdk_seed)==' + str(bootstrap.SEED_LENGTH),
                'len(_sdk_seed)==' + str(bootstrap.SEED_LENGTH - 1)),
            'wrong-digest': command.replace(bootstrap.SEED_SHA256, '0' * 64),
        }
        results = []
        for name, mutated in mutations.items():
            result = subprocess.run([bash(fixture.environment), '-euc', mutated + ' exec helpers/probe.py'],
                cwd=fixture.root, env=fixture.environment, capture_output=True, text=True, timeout=10)
            with self.subTest(mutation=name):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Only base64 data is allowed' if name == 'tampered'
                    else 'SDK bootstrap seed failed integrity validation', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertFalse((fixture.root / 'executed').exists())
                self.assertFalse(any(fixture.sdk.rglob('__pycache__')))
                self.assertEqual(fixture.git('status', '--porcelain', '--untracked-files=all', '--ignored=matching').strip(), '')
            results.append({'mutation': name, 'exit': result.returncode, 'SDK_executed': False})
        print(json.dumps({'compact_seed_refusals': results}), flush=True)


if __name__ == '__main__':
    unittest.main()
