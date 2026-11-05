import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('native_payload', Path(__file__).resolve().parents[1] / 'helpers/native_payload.py')
payload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(payload)


class NativePayload(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native payload ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / 'api'
        self.binary.write_bytes(b'native exact executable')
        self.args = ('a' * 40, 'v1.2.3', 'api', 'Linux/x86_64', {'host-arm64': 'b' * 64})

    def test_exact_identity_and_every_previous_negative_control_are_retained(self):
        original = payload.record(self.binary, *self.args)
        self.assertEqual(payload.verify(self.binary, *self.args), original)
        receipt = payload.receipt_path(self.binary)
        for key, value in [('source', '0' * 40), ('version', 'v1.2.4'), ('kind', 'cli'), ('platform', 'Linux/arm64'), ('sha256', '0' * 64), ('dependencies', {}), ('unexpected', True)]:
            with self.subTest(key=key):
                receipt.write_text(json.dumps(original | {key: value}))
                with self.assertRaisesRegex(ValueError, 'mismatch'):
                    payload.verify(self.binary, *self.args)
        receipt.write_text(json.dumps(original))
        self.binary.write_bytes(b'changed native executable')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            payload.verify(self.binary, *self.args)

    def test_deterministic_compression_preserves_the_verified_input(self):
        payload.record(self.binary, *self.args)
        destination = self.root / 'payload.gz'
        receipt = payload.compress(self.binary, destination, *self.args)
        before = destination.read_bytes()
        payload.compress(self.binary, destination, *self.args)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(gzip.decompress(before), self.binary.read_bytes())
        self.assertEqual(receipt['source'], self.args[0])

    def test_empty_symlink_oversized_or_modified_receipt_inputs_fail(self):
        original = self.binary.read_bytes()
        self.binary.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            payload.record(self.binary, *self.args)
        with self.binary.open('wb') as stream:
            stream.truncate(payload.MAXIMUM + 1)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            payload.record(self.binary, *self.args)
        self.binary.unlink()
        target = self.root / 'actual';target.write_bytes(original)
        self.binary.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            payload.record(self.binary, *self.args)
        self.binary.unlink();self.binary.write_bytes(original)
        payload.record(self.binary, *self.args)
        receipt = payload.receipt_path(self.binary);saved = receipt.read_bytes();receipt.unlink()
        other = self.root / 'receipt';other.write_bytes(saved);receipt.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            payload.verify(self.binary, *self.args)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO control requires Unix')
    def test_nonregular_payload_fails_before_opening_it(self):
        self.binary.unlink()
        os.mkfifo(self.binary)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            payload.record(self.binary, *self.args)

    def test_missing_unverified_or_rebound_payload_cannot_produce_output(self):
        destination = self.root / 'payload.gz'
        with self.assertRaises(FileNotFoundError):
            payload.compress(self.binary, destination, *self.args)
        self.assertFalse(destination.exists())
        payload.record(self.binary, *self.args)
        with self.assertRaises(ValueError):
            payload.compress(self.binary, destination, self.args[0], self.args[1], 'host', self.args[3], self.args[4])
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
