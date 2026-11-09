"""Real CMS framing controls, including authenticated metadata substitutions."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('framed_snapshot', Path(__file__).parents[1]/'helpers/snapshot.py')
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SnapshotFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.key, cls.cert = cls.root/'key.pem', cls.root/'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-noenc',
                        '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
                        '-subj', '/CN=disposable-framing-test'], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.payload = cls.root/'payload'
        cls.payload.write_bytes(os.urandom(2 * snapshot.FRAME_BYTES + 12345))
        cls.original = cls.root/'original.cms'
        snapshot._encrypt_payload(cls.payload, cls.original, cls.cert)
        cls.raw = cls.original.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(dir=self.root)
        self.work = Path(self.workspace.name)
        self.addCleanup(self.workspace.cleanup)

    def decrypt(self, data, *, key=None):
        cipher, destination = self.work/'input.cms', self.work/'plain'
        cipher.write_bytes(data)
        snapshot._decrypt_payload(cipher, destination, key or self.key, hashlib.sha256(data).hexdigest())
        return destination

    def rejected(self, data):
        with self.assertRaises((ValueError, OSError)):
            self.decrypt(data)
        self.assertFalse((self.work/'plain').exists(), 'failed authentication exposed plaintext')
        self.assertFalse(list(self.work.glob('.snapshot-*')), 'private scratch survived refusal')

    def index(self):
        magic, offset, size = snapshot.FOOTER.unpack(self.raw[-snapshot.FOOTER.size:])
        self.assertEqual(magic, snapshot.END_MAGIC)
        cipher, plain = self.work/'index.cms', self.work/'index'
        cipher.write_bytes(self.raw[offset+8:offset+8+size])
        snapshot._cms(cipher, plain, self.key, False, time.monotonic()+30)
        prefix = plain.read_bytes()[:24]
        return prefix, json.loads(plain.read_bytes()[24:]), offset

    def replace_index(self, prefix, index, offset):
        plain, cipher = self.work/'changed-index', self.work/'changed-index.cms'
        plain.write_bytes(prefix + snapshot._canonical(index))
        snapshot._cms(plain, cipher, self.cert, True, time.monotonic()+30)
        data = cipher.read_bytes()
        return self.raw[:offset] + snapshot.RECORD_HEADER.pack(len(data)) + data + snapshot.FOOTER.pack(snapshot.END_MAGIC, offset, len(data))

    def test_real_multiframe_roundtrip_and_private_files(self):
        result = self.decrypt(self.raw)
        self.assertEqual(snapshot.sha256(result), snapshot.sha256(self.payload))
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.original.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.work.glob('.snapshot-*')))
        _, index, _ = self.index()
        self.assertEqual([entry['plaintext_size'] for entry in index['frames']],
                         [snapshot.FRAME_BYTES, snapshot.FRAME_BYTES, 12345])

    def test_changed_frame_refuses_even_with_replaced_transport_checksum(self):
        data = bytearray(self.raw)
        data[snapshot.FILE_HEADER.size + 100] ^= 1
        self.rejected(data)

    def test_index_gcm_tag_refuses_before_any_plaintext(self):
        _, offset, size = snapshot.FOOTER.unpack(self.raw[-snapshot.FOOTER.size:])
        data = bytearray(self.raw)
        data[offset+8+size-1] ^= 1
        self.rejected(data)

    def test_truncation_trailing_bytes_magic_and_footer_bounds(self):
        for name, data in [('truncated', self.raw[:-1]), ('tail', self.raw+b'x'),
                           ('header', b'X'+self.raw[1:]), ('footer', self.raw[:-24]+b'X'+self.raw[-23:]),
                           ('offset', self.raw[:-16]+snapshot.RECORD_HEADER.pack(2**63)+self.raw[-8:])]:
            with self.subTest(name=name):
                self.rejected(data)

    def test_authenticated_index_cannot_hide_missing_reordered_or_unbounded_frames(self):
        prefix, original, offset = self.index()
        for field, value in [('ordinal', 1), ('offset', 0), ('size', 2**63),
                             ('plaintext_size', True), ('plaintext_sha256', '0'*64)]:
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                changed['frames'][0][field] = value
                self.rejected(self.replace_index(prefix, changed, offset))
        for field, value in [('payload_bytes', True), ('payload_sha256', '0'*64),
                             ('session', '0'*32), ('version', True), ('extra', 'refuse')]:
            with self.subTest(field=field):
                changed = {**original, field: value}
                self.rejected(self.replace_index(prefix, changed, offset))
        changed = {**original, 'frames': original['frames'][:-1]}
        self.rejected(self.replace_index(prefix, changed, offset))
        changed = {**original, 'frames': list(reversed(original['frames']))}
        self.rejected(self.replace_index(prefix, changed, offset))

    def test_cross_session_splice_refuses_even_with_authentic_reencrypted_index(self):
        other = self.work/'other.cms'
        snapshot._encrypt_payload(self.payload, other, self.cert)
        prefix, index, offset = self.index()
        data = bytearray(self.raw)
        first = index['frames'][0]
        begin, end = first['offset']+8, first['offset']+8+first['size']
        replacement = other.read_bytes()[begin:end]
        self.assertEqual(len(replacement), first['size'])
        data[begin:end] = replacement
        first['sha256'] = hashlib.sha256(replacement).hexdigest()
        rewritten = self.replace_index(prefix, index, offset)
        self.rejected(bytes(data[:offset]) + rewritten[offset:])

    def test_unauthenticated_cbc_and_indefinite_der_are_refused(self):
        plain, cipher, target = self.work/'cbc-plain', self.work/'cbc', self.work/'cbc-output'
        plain.write_bytes(b'not authenticated')
        subprocess.run(['openssl','cms','-encrypt','-aes-256-cbc','-binary','-outform','DER',
                        '-in',str(plain),'-out',str(cipher),str(self.cert)],check=True,timeout=30)
        with self.assertRaisesRegex(ValueError, 'authenticated'):
            snapshot._cms(cipher,target,self.key,False,time.monotonic()+30)
        self.assertFalse(target.exists())
        cipher.write_bytes(b'\x30\x80\x00\x00')
        with self.assertRaisesRegex(ValueError, 'indefinite'):
            snapshot._cms(cipher,target,self.key,False,time.monotonic()+30)
        self.assertFalse(target.exists())

    def test_preexisting_destination_survives_and_deadline_is_aggregate(self):
        target = self.work/'existing'
        target.write_bytes(b'preserve')
        with self.assertRaises(FileExistsError):
            snapshot._encrypt_payload(self.payload, target, self.cert)
        self.assertEqual(target.read_bytes(), b'preserve')
        with self.assertRaises(FileExistsError):
            snapshot._decrypt_payload(self.original, target, self.key, snapshot.sha256(self.original))
        self.assertEqual(target.read_bytes(), b'preserve')
        with self.assertRaises(TimeoutError):
            snapshot._encrypt_payload(self.payload, self.work/'expired', self.cert, deadline=time.monotonic()-1)
        self.assertFalse((self.work/'expired').exists())
        self.assertFalse(list(self.work.glob('.snapshot-*')))

    def test_mutation_after_open_and_oversize_input_refuse(self):
        source = self.work/'source'
        source.write_bytes(b'owned source')
        original = snapshot._cms
        def mutate(*args):
            original(*args)
            source.write_bytes(b'other source')
        with patch.object(snapshot, '_cms', mutate), self.assertRaisesRegex(ValueError, 'changed'):
            snapshot._encrypt_payload(source, self.work/'result', self.cert)
        self.assertFalse((self.work/'result').exists())
        with source.open('wb') as stream:
            stream.truncate(snapshot.MAX_CIPHERTEXT+1)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            snapshot._encrypt_payload(source, self.work/'result', self.cert)

    def test_replacement_after_authenticated_index_refuses_before_exposure(self):
        cipher, destination = self.work/'changing.cms', self.work/'output'
        cipher.write_bytes(self.raw)
        original, calls = snapshot._cms, []
        def replace(*args):
            original(*args)
            calls.append(True)
            if len(calls) == 1:
                cipher.rename(self.work/'old-inode')
                cipher.write_bytes(self.raw)
        with patch.object(snapshot, '_cms', replace), self.assertRaisesRegex(ValueError, 'changed'):
            snapshot._decrypt_payload(cipher,destination,self.key,hashlib.sha256(self.raw).hexdigest())
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.work.glob('.snapshot-*')))

    def test_symlink_refusal_and_no_unauthenticated_restore(self):
        link = self.work/'link'
        link.symlink_to(self.original)
        with self.assertRaises(OSError):
            snapshot._decrypt_payload(link, self.work/'plain', self.key, snapshot.sha256(self.original))
        pair = self.work/'pair'
        pair.mkdir()
        (pair/'snapshot.cms').write_bytes(self.raw[:-10])
        (pair/'receipt.json').write_text(json.dumps({'format':2, 'ciphertext_sha256': snapshot.sha256(pair/'snapshot.cms')}))
        with patch.object(snapshot, 'restore') as restore, self.assertRaises(ValueError):
            snapshot.unseal(pair, self.key, self.work)
        restore.assert_not_called()
