"""Regression coverage for the release directory reader and fail-closed audit."""

from pathlib import Path
import struct
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"helpers"))
from godot_web_release import pack_paths, prepare
import gzip
import json
import hashlib
import re
from urllib.parse import urljoin


class ArtifactTests(unittest.TestCase):
    def export(self,root):
        (root/'index.html').write_text('<script src="index.js"></script>\nconst GODOT_CONFIG = '+json.dumps({'executable':'index','fileSizes':{'index.wasm':7,'index.pck':8}})+';\n')
        (root/'index.js').write_bytes(b'loader')
        (root/'index.wasm').write_bytes(b'engine')
        (root/'index.pck').write_bytes(self.pack(['res://main.scn']))

    def test_frozen_shell_and_precompressed_bytes_identify_the_same_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            prepare(root,'a'*40)
            manifest=json.loads((root/'release-manifest.json').read_text())
            release=root/'releases'/manifest['release_id']
            self.assertIn('/releases/'+manifest['release_id']+'/index', (root/'index.html').read_text())
            for name in ('index.js','index.wasm','index.pck'):
                data=(release/name).read_bytes()
                self.assertEqual(gzip.decompress((release/(name+'.gz')).read_bytes()),data)
                self.assertEqual(manifest['files'][name]['sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(manifest['source_commit'],'a'*40)

    def test_caller_budgets_and_linked_output_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            with self.assertRaisesRegex(ValueError,'caller budget'): prepare(root,'a'*40,max_wasm_bytes=1)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            (root/'index.wasm').unlink();(root/'index.wasm').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError,'symlinks'): prepare(root,'a'*40)

    def test_relative_payload_urls_preserve_shell_directory_and_repack_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.export(root)
            prepare(root,'a'*40,relative_urls=True)
            manifest=json.loads((root/'release-manifest.json').read_text())
            html=(root/'index.html').read_text()
            config=json.loads(re.search(r'const GODOT_CONFIG = (\{[^\n]+\});',html)[1])
            urls=[config['executable'],*config['fileSizes'],re.search(r'src="([^"]+)"',html)[1]]
            for base in ('https://game.example/','https://game.example/74/'):
                for url in urls:
                    self.assertTrue(urljoin(base,url).startswith(base+'releases/'+manifest['release_id']+'/'))
            self.assertEqual(gzip.decompress((root/'index.html.gz').read_bytes()),html.encode())
            prepare(root,'a'*40,relative_urls=True)
            self.assertEqual((root/'index.html').read_text(),html)
            self.assertEqual(json.loads((root/'release-manifest.json').read_text())['release_id'],manifest['release_id'])

    @staticmethod
    def pack(names, flags=0, version=2):
        data = b"GDPC" + struct.pack("<5IQ", version, 4, 7, 2, flags, 0)
        data += bytes(64) if version == 2 else struct.pack("<Q", 104) + bytes(64)
        data += struct.pack("<I", len(names))
        for name in names:
            encoded = name.encode() + b"\0"
            data += struct.pack("<I", len(encoded)) + encoded + bytes(36)
        return data

    def test_directory_names_and_padding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.pck"
            names = ["res://project.binary", "res://src/main/main.gdc"]
            for version in (2, 3, 4):
                path.write_bytes(self.pack(names, version=version))
                self.assertEqual(pack_paths(path), names)

    def test_rejects_unreadable_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.pck"
            invalid_offset = bytearray(self.pack([], version=3))
            struct.pack_into("<Q", invalid_offset, 32, 2**63)
            for data in (b"bad!", self.pack([], flags=1), self.pack([], flags=4),
                         self.pack([], version=99), invalid_offset, self.pack(["test"])[:-1]):
                path.write_bytes(data)
                with self.assertRaises((ValueError, struct.error)):
                    pack_paths(path)

    def test_excludes_development_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("index.html", "index.js", "index.wasm"):
                (root / name).write_bytes(b"fixture")
            for forbidden in ("res://tests/test.gdc", "res://tools/debug.gd",
                              "res://addons/example/examples/demo.scn"):
                (root / "index.pck").write_bytes(self.pack([forbidden]))
                with self.assertRaisesRegex(ValueError, "Development resources"):
                    prepare(root)

    def test_requires_complete_export(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Missing or empty"):
                prepare(Path(folder))

    def test_rejects_nonimmutable_source_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            for source in ['main', 'a' * 40 + '\n', 'A' * 40]:
                with self.assertRaisesRegex(ValueError, 'Source commit'):
                    prepare(Path(folder), source)


if __name__ == "__main__":
    unittest.main()
