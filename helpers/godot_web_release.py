#!/usr/bin/env python3
"""Prepare and audit a Godot release; no third-party Python dependencies."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
from godot_version_release import compact_payloads, stage_version


def brotli(data, *, wasm=False, timeout=120):
    return subprocess.run(['node', str(Path(__file__).with_name('brotli.mjs')), 'compress-wasm' if wasm else 'compress'],
                          input=data, capture_output=True, check=True, timeout=timeout).stdout


def pack_paths(path):
    """Read standalone v2/v3/v4 directories, per Godot 4.7 file_access_pack.cpp."""
    with path.open("rb") as pack:
        def read(size):
            data = pack.read(size)
            if len(data) != size:
                raise ValueError("Truncated PCK directory")
            return data

        def u32():
            return struct.unpack("<I", read(4))[0]

        if read(4) != b"GDPC":
            raise ValueError("Expected a standalone Godot PCK")
        version = u32()
        if version not in (2, 3, 4):
            raise ValueError("Unsupported PCK version")
        read(12)  # engine version
        if u32() & ~2:  # Only the relative-file-base flag is permitted.
            raise ValueError("Cannot audit encrypted, sparse or unknown PCK flags")
        read(8)  # file base
        if version >= 3:
            offset = struct.unpack("<Q", read(8))[0]
            if offset < 40 or offset > path.stat().st_size - 4:
                raise ValueError("Invalid PCK directory offset")
            pack.seek(offset)
        else:
            read(16 * 4)  # reserved header
        count = u32()
        if count > 100_000:
            raise ValueError("Invalid PCK directory size")
        paths = []
        for _ in range(count):
            length = u32()
            if length > 4096:
                raise ValueError("Invalid PCK path length")
            paths.append(read(length).rstrip(b"\0").decode("utf-8"))
            read(36)  # offset, size, MD5, flags
        return paths


def prepare(root, source_commit=None, max_wasm_bytes=0, max_transfer_bytes=0, use_brotli=False, relative_urls=False, versioned_only=False, root_files=(), *, policy=None):
    policy = policy or {}
    allowed = {'payload_directories', 'forbidden_prefixes', 'forbidden_names',
               'required_pack_paths', 'deferred_payloads', 'brotli_wasm', 'brotli_timeout',
               'recovery_id', 'failure_function', 'release_root', 'strip_cache_query',
               'max_file_bytes', 'max_tree_bytes'}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ValueError('Unknown Godot payload policy')
    for key in ['payload_directories', 'forbidden_prefixes', 'forbidden_names', 'required_pack_paths']:
        values = policy.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value or '\0' in value for value in values):
            raise ValueError('Payload policy requires literal path lists')
    compression_timeout = policy.get('brotli_timeout', 120)
    if type(compression_timeout) is not int or not 1 <= compression_timeout <= 300:
        raise ValueError('Brotli deadline must stay within the reviewed five minute bound')
    for key in ['brotli_wasm', 'strip_cache_query']:
        if key in policy and type(policy[key]) is not bool:
            raise ValueError('Payload switches must be literal booleans')
    deferred = policy.get('deferred_payloads', {})
    if not isinstance(deferred, dict) or any(not isinstance(key, str) or not isinstance(value, str) or not value for key, value in deferred.items()):
        raise ValueError('Deferred payload policy requires declared filename/load phase pairs')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or any(path.is_symlink() for path in root.rglob('*')):
        raise ValueError('Release input may not contain symlinks')
    if max_wasm_bytes < 0 or max_transfer_bytes < 0:
        raise ValueError('Caller budgets must be nonnegative')
    if source_commit is not None and not re.fullmatch(r'[0-9a-f]{40}', source_commit):
        raise ValueError('Source commit must be an exact Git commit SHA')
    for name in ("index.html", "index.js", "index.wasm", "index.pck"):
        if not (root / name).is_file() or (root / name).stat().st_size == 0:
            raise ValueError(f"Missing or empty export artifact: {name}")
    paths = pack_paths(root / "index.pck")
    forbidden = [p for p in paths if (
        p.removeprefix("res://").startswith(("tests/", "tools/", "src/static/levels/templates/", *policy.get("forbidden_prefixes", [])))
        or Path(p).name in {".env.json", "gdam.link.json", *policy.get("forbidden_names", [])}
        or ("addons/" in p and "/examples/" in p)
    )]
    if forbidden:
        raise ValueError("Development resources in release:\n" + "\n".join(forbidden))

    normalized = {name.removeprefix('res://') for name in paths}
    if set(policy.get('required_pack_paths', [])) - normalized:
        raise ValueError('Release PCK omitted required native resources')
    payload_files = list(root.iterdir())
    for directory in policy.get('payload_directories', []):
        path = Path(directory)
        if (path.is_absolute() or '..' in path.parts or path.as_posix() != directory
                or ':' in directory or '\\' in directory):
            raise ValueError('Payload directories must be canonical relative paths')
        target = root / directory
        if not target.is_dir():
            raise ValueError('Declared payload directory is missing: ' + directory)
        payload_files += [entry for entry in target.rglob('*') if entry.is_file()]
    payload_files = sorted(set(payload_files))

    files = {}
    for path in payload_files:
        if not path.is_file() or path.suffix not in {".html", ".js", ".wasm", ".pck", ".png", ".json"}:
            continue
        if path.name == "release-manifest.json":
            continue
        data = path.read_bytes()
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        if path.suffix in {".html", ".js", ".wasm", ".pck", ".json"}:
            path.with_name(path.name + ".gz").write_bytes(compressed)
            encoded = brotli(data, wasm=bool(policy.get("brotli_wasm")) and path.suffix == ".wasm", timeout=compression_timeout) if use_brotli else compressed
            if use_brotli: path.with_name(path.name + ".br").write_bytes(encoded)
            transfer = len(encoded)
        else:
            transfer = len(data)
        name = path.relative_to(root).as_posix()
        files[name] = {"bytes": len(data), "transfer_bytes": transfer,
                            "sha256": hashlib.sha256(data).hexdigest()}
        if path.suffix != '.png':
            files[name]['gzip_bytes'] = len(compressed)
            files[name]['transfer_encoding'] = 'br' if use_brotli else 'gzip'
    release_id = stage_version(root, files, relative_urls=relative_urls,
        **{key: policy[key] for key in ['recovery_id', 'failure_function', 'release_root', 'strip_cache_query'] if key in policy})
    html = (root / 'index.html').read_bytes()
    compressed_html = gzip.compress(html, compresslevel=6, mtime=0)
    (root / 'index.html.gz').write_bytes(compressed_html)
    brotli_html = brotli(html, timeout=compression_timeout) if use_brotli else compressed_html
    if use_brotli: (root / 'index.html.br').write_bytes(brotli_html)
    files['index.html'] = {'bytes': len(html), 'transfer_bytes': len(brotli_html),
                           'gzip_bytes': len(compressed_html), 'transfer_encoding': 'br' if use_brotli else 'gzip',
                           'sha256': hashlib.sha256(html).hexdigest()}
    full_total = sum(f["transfer_bytes"] for f in files.values())
    if set(deferred) - files.keys():
        raise ValueError('Declared deferred payload is missing')
    deferred_total = sum(files[name]['transfer_bytes'] for name in deferred)
    total = full_total - deferred_total
    for name, phase in deferred.items():
        files[name]['load_phase'] = phase
    if max_wasm_bytes and files["index.wasm"]["transfer_bytes"] > max_wasm_bytes:
        raise ValueError("Compressed WASM exceeds the caller budget")
    if max_transfer_bytes and total > max_transfer_bytes:
        raise ValueError(f"Cold payload {total:,} bytes exceeds the caller budget")
    manifest = {"files": files, "transfer_bytes": total, "pack_entries": len(paths), "release_id": release_id}
    if deferred:
        manifest.update(full_transfer_bytes=full_total, deferred_transfer_bytes=deferred_total)
    if versioned_only:
        # The shell already requests content-addressed URLs. Keep the export's input
        # directory separate from this generated serving root; no links cross the
        # extraction boundary and every browser-facing versioned URL is unchanged.
        manifest.update(compact_payloads(root, files, release_id, root_files))
    for name in ['max_file_bytes', 'max_tree_bytes']:
        if type(policy.get(name, 0)) is not int or policy.get(name, 0) < 0:
            raise ValueError('Hosting byte limits must be nonnegative integers')
    if source_commit is not None:
        manifest['source_commit'] = source_commit
    (root / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    physical = [path for path in root.rglob('*') if path.is_file()]
    if policy.get('max_file_bytes') and any(path.stat().st_size > policy['max_file_bytes'] for path in physical):
        raise ValueError('Release file exceeds the caller hosting byte limit')
    if policy.get('max_tree_bytes') and sum(path.stat().st_size for path in physical) > policy['max_tree_bytes']:
        raise ValueError('Release tree exceeds the caller hosting byte limit')
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument('--source-commit', help='Exact verified Git revision, required by CI/CD')
    parser.add_argument('--max-wasm-bytes', type=int, default=0)
    parser.add_argument('--max-transfer-bytes', type=int, default=0)
    parser.add_argument('--brotli', action='store_true')
    parser.add_argument('--versioned-only', action='store_true', help='Keep one physical payload under the shell’s existing versioned URLs')
    parser.add_argument('--relative-urls', action='store_true', help='Keep payload URLs under the shell directory, including path previews')
    args = parser.parse_args()
    prepare(args.root, args.source_commit, args.max_wasm_bytes, args.max_transfer_bytes, args.brotli, args.relative_urls, args.versioned_only)
