#!/usr/bin/env python3
"""Freeze one database writer, archive its volume, and verify an isolated restore."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import gzip
import fcntl
import ipaddress
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import urllib.request

MAX_FILES = 100000
MAX_BYTES = 16 * 1024**3
AUXILIARY = {'auxiliary.db', 'auxiliary.db-wal', 'auxiliary.db-shm'}

# The transport limit is distinct from MAX_BYTES (expanded product files).
MAX_CIPHERTEXT = 8 * 1024**3
FRAME_BYTES = 4 * 1024**2
MAX_FRAMES = MAX_CIPHERTEXT // FRAME_BYTES
INDEX_BYTES = 2 * 1024**2
CMS_OVERHEAD = 64 * 1024
FILE_MAGIC = b'KMSNAP2\0'
END_MAGIC = b'KMSEND2\0'
PART_MAGIC = b'KMSPART2'
INDEX_MAGIC = b'KMSIDX2\0'
FILE_HEADER = struct.Struct('>8s16s')
PART_HEADER = struct.Struct('>8s16sQI')
RECORD_HEADER = struct.Struct('>Q')
FOOTER = struct.Struct('>8sQQ')
CRYPTO_SECONDS = 900


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('snapshot cryptography exceeded its aggregate deadline')
    return seconds


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate recovery metadata key')
        result[key] = value
    return result


def _metadata(raw):
    return json.loads(raw, object_pairs_hook=_unique_object)


class _StableFile:
    """Use one bounded regular inode and refuse replacement or mutation."""
    def __init__(self, path, limit):
        self.path = Path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        self.stream = os.fdopen(descriptor, 'rb')
        self.info = os.fstat(descriptor)
        if not stat.S_ISREG(self.info.st_mode) or not 0 < self.info.st_size <= limit:
            self.stream.close()
            raise ValueError('recovery input must be a bounded nonempty regular file')

    def unchanged(self):
        now, named = os.fstat(self.stream.fileno()), self.path.lstat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(value, field) != getattr(self.info, field) for value in (now, named) for field in fields):
            raise ValueError('recovery input changed during cryptography')

    def hash(self, deadline):
        self.stream.seek(0)
        result = hashlib.sha256()
        while block := self.stream.read(256 * 1024):
            _remaining(deadline)
            result.update(block)
        self.unchanged()
        return result.hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.stream.close()


def _der_item(data, start, bound):
    if start + 2 > bound:
        raise ValueError('truncated CMS DER')
    tag, length = data[start], data[start+1]
    cursor = start + 2
    if length & 0x80:
        width = length & 0x7f
        if not 1 <= width <= 4 or cursor + width > bound or data[cursor] == 0:
            raise ValueError('indefinite or noncanonical CMS DER length')
        length = int.from_bytes(data[cursor:cursor+width], 'big')
        if length < 128:
            raise ValueError('noncanonical CMS DER length')
        cursor += width
    end = cursor + length
    if end > bound:
        raise ValueError('truncated CMS DER value')
    return tag, data[cursor:end], end


def _der_fields(body, count):
    # Fixed field counts and depth, never attacker-selected recursion or count.
    result, cursor = [], 0
    for _ in range(count):
        tag, value, cursor = _der_item(body, cursor, len(body))
        result.append((tag, value))
    if cursor != len(body):
        raise ValueError('unexpected CMS DER fields')
    return result


def _der_sequence(raw, tag, count):
    actual, body, end = _der_item(raw, 0, len(raw))
    if actual != tag or end != len(raw):
        raise ValueError('unknown CMS DER shape')
    return _der_fields(body, count)


def _require_gcm(path):
    """Accept only the shallow DER AuthEnvelopedData emitted by our command."""
    # An arbitrary CMS decrypt also accepts unauthenticated CBC. Require GCM,
    # a 96-bit nonce and a 128-bit tag before invoking the native decoder.
    raw = memoryview(path.read_bytes())
    content = _der_sequence(raw, 0x30, 2)
    if content[0] != (0x06, bytes.fromhex('2a864886f70d0109100117')) or content[1][0] != 0xa0:
        raise ValueError('recovery CMS must be authenticated enveloped data')
    fields = _der_sequence(content[1][1], 0x30, 4)
    if (fields[0] != (0x02, b'\0') or fields[1][0] != 0x31 or
            not 0 < len(fields[1][1]) <= CMS_OVERHEAD or fields[2][0] != 0x30 or
            fields[3][0] != 0x04 or len(fields[3][1]) != 16):
        raise ValueError('invalid authenticated CMS envelope')
    encrypted = _der_fields(fields[2][1], 3)
    if (encrypted[0] != (0x06, bytes.fromhex('2a864886f70d010701')) or encrypted[1][0] != 0x30 or encrypted[2][0] != 0x80):
        raise ValueError('invalid CMS encrypted content')
    algorithm = _der_fields(encrypted[1][1], 2)
    if algorithm[0] != (0x06, bytes.fromhex('60864801650304012e')) or algorithm[1][0] != 0x30:
        raise ValueError('recovery CMS must use AES-256-GCM')
    parameters = _der_fields(algorithm[1][1], 2)
    if parameters[0][0] != 0x04 or len(parameters[0][1]) != 12 or parameters[1] != (0x02, b'\x10'):
        raise ValueError('invalid recovery CMS GCM parameters')


def _cms(source, target, authority, encrypt, deadline):
    """Each DER object is small; no ASN.1 decoder receives the whole backup."""
    args = (['-encrypt', '-aes-256-gcm', '-outform', 'DER'] if encrypt else
            ['-decrypt', '-inform', 'DER', '-inkey', str(authority)])
    command = ['openssl', 'cms', *args, '-binary', '-in', str(source), '-out', str(target)]
    if encrypt:
        command.append(str(authority))
    if not encrypt:
        _require_gcm(source)
    # OpenSSL can emit unauthenticated plaintext before checking a GCM tag.
    # Its output is private scratch and is never interpreted after a failure.
    try:
        subprocess.run(command, check=True, timeout=min(30, _remaining(deadline)),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        raise ValueError('recovery CMS authentication or encryption failed') from None
    target.chmod(0o600)
    _remaining(deadline)


def _publish_file(pending, destination):
    """Publish complete bytes without replacing a preexisting destination."""
    os.link(pending, destination)
    pending.unlink()


def _read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError('truncated recovery framing')
    return data


def _encrypt_payload(payload, destination, recipient, *, deadline=None):
    """Write one finite, indexed sequence of independently authenticated CMS objects."""
    deadline = deadline if deadline is not None else time.monotonic() + CRYPTO_SECONDS
    with _StableFile(payload, MAX_CIPHERTEXT) as source, tempfile.TemporaryDirectory(
            prefix='.snapshot-encrypt-', dir=destination.parent) as directory:
        scratch = Path(directory)
        plain, cipher, pending = scratch/'plain', scratch/'cipher', scratch/'pending'
        session = os.urandom(16)
        entries, whole, consumed = [], hashlib.sha256(), 0
        with pending.open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(FILE_HEADER.pack(FILE_MAGIC, session))
            for ordinal in range(MAX_FRAMES):
                _remaining(deadline)
                data = source.stream.read(FRAME_BYTES)
                if not data:
                    break
                consumed += len(data)
                whole.update(data)
                plain_hash = hashlib.sha256(data).hexdigest()
                with plain.open('wb') as stream:
                    stream.write(PART_HEADER.pack(PART_MAGIC, session, ordinal, len(data)))
                    stream.write(data)
                del data
                _cms(plain, cipher, recipient, True, deadline)
                size = cipher.stat().st_size
                if not 0 < size <= PART_HEADER.size + FRAME_BYTES + CMS_OVERHEAD:
                    raise ValueError('recovery CMS frame exceeds its bound')
                offset = output.tell()
                if offset + RECORD_HEADER.size + size + INDEX_BYTES + CMS_OVERHEAD + FOOTER.size > MAX_CIPHERTEXT:
                    raise ValueError('encrypted snapshot exceeds the 8 GiB transport bound')
                entries.append({'ordinal': ordinal, 'offset': offset, 'size': size,
                                'sha256': sha256(cipher), 'plaintext_size': consumed - ordinal * FRAME_BYTES,
                                'plaintext_sha256': plain_hash})
                output.write(RECORD_HEADER.pack(size))
                with cipher.open('rb') as stream:
                    shutil.copyfileobj(stream, output, length=256 * 1024)
            if source.stream.read(1) or consumed != source.info.st_size:
                raise ValueError('recovery payload exceeds its frame count or changed')
            source.unchanged()
            index = {'kind': 'komizo.snapshot-index', 'version': 2, 'session': session.hex(),
                     'frame_bytes': FRAME_BYTES, 'payload_bytes': consumed,
                     'payload_sha256': whole.hexdigest(), 'frames': entries}
            raw = INDEX_MAGIC + session + _canonical(index)
            if len(raw) > INDEX_BYTES:
                raise ValueError('recovery index exceeds its bound')
            plain.write_bytes(raw)
            _cms(plain, cipher, recipient, True, deadline)
            size, offset = cipher.stat().st_size, output.tell()
            if not 0 < size <= INDEX_BYTES + CMS_OVERHEAD or offset + 8 + size + FOOTER.size > MAX_CIPHERTEXT:
                raise ValueError('encrypted snapshot exceeds the 8 GiB transport bound')
            output.write(RECORD_HEADER.pack(size))
            with cipher.open('rb') as stream:
                shutil.copyfileobj(stream, output, length=256 * 1024)
            output.write(FOOTER.pack(END_MAGIC, offset, size))
            output.flush()
            os.fsync(output.fileno())
        source.unchanged()
        _remaining(deadline)
        _publish_file(pending, destination)


def _checked_index(raw, session, index_offset):
    if not raw.startswith(INDEX_MAGIC + session):
        raise ValueError('recovery index session mismatch')
    index_raw = raw[len(INDEX_MAGIC) + len(session):]
    index = _metadata(index_raw)
    if not isinstance(index, dict) or set(index) != {'kind', 'version', 'session', 'frame_bytes',
                                                       'payload_bytes', 'payload_sha256', 'frames'}:
        raise ValueError('unknown recovery index schema')
    if (index_raw != _canonical(index) or index['kind'] != 'komizo.snapshot-index' or
            type(index['version']) is not int or index['version'] != 2 or index['session'] != session.hex() or
            type(index['frame_bytes']) is not int or index['frame_bytes'] != FRAME_BYTES or
            type(index['payload_bytes']) is not int or not 0 < index['payload_bytes'] <= MAX_CIPHERTEXT or
            not isinstance(index['payload_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', index['payload_sha256']) or
            not isinstance(index['frames'], list) or not 1 <= len(index['frames']) <= MAX_FRAMES):
        raise ValueError('invalid recovery index identity or bounds')
    offset, total = FILE_HEADER.size, 0
    for ordinal, entry in enumerate(index['frames']):
        if not isinstance(entry, dict) or set(entry) != {'ordinal', 'offset', 'size', 'sha256', 'plaintext_size', 'plaintext_sha256'}:
            raise ValueError('unknown recovery frame schema')
        if (any(type(entry[key]) is not int for key in ('ordinal', 'offset', 'size', 'plaintext_size')) or
                entry['ordinal'] != ordinal or entry['offset'] != offset or
                not 0 < entry['size'] <= PART_HEADER.size + FRAME_BYTES + CMS_OVERHEAD or
                not 0 < entry['plaintext_size'] <= FRAME_BYTES or
                (ordinal < len(index['frames']) - 1 and entry['plaintext_size'] != FRAME_BYTES) or
                any(not isinstance(entry[key], str) or not re.fullmatch(r'[a-f0-9]{64}', entry[key])
                    for key in ('sha256', 'plaintext_sha256'))):
            raise ValueError('invalid recovery frame identity, order or bounds')
        offset += RECORD_HEADER.size + entry['size']
        total += entry['plaintext_size']
    if offset != index_offset or total != index['payload_bytes']:
        raise ValueError('recovery framing has gaps, overlaps or an incorrect total')
    return index


def _decrypt_payload(ciphertext, destination, key, expected, *, deadline=None):
    """Publish plaintext only after every tag and the complete closure authenticate."""
    deadline = deadline if deadline is not None else time.monotonic() + CRYPTO_SECONDS
    with _StableFile(ciphertext, MAX_CIPHERTEXT) as source, tempfile.TemporaryDirectory(
            prefix='.snapshot-decrypt-', dir=destination.parent) as directory:
        scratch = Path(directory)
        cipher, plain, pending = scratch/'cipher', scratch/'plain', scratch/'pending'
        if source.hash(deadline) != expected:
            raise ValueError('off-host ciphertext checksum mismatch')
        stream = source.stream
        stream.seek(0)
        magic, session = FILE_HEADER.unpack(_read_exact(stream, FILE_HEADER.size))
        if magic != FILE_MAGIC or source.info.st_size < FILE_HEADER.size + FOOTER.size + 8:
            raise ValueError('unknown recovery framing')
        stream.seek(-FOOTER.size, os.SEEK_END)
        magic, offset, size = FOOTER.unpack(_read_exact(stream, FOOTER.size))
        if (magic != END_MAGIC or offset < FILE_HEADER.size or not 0 < size <= INDEX_BYTES + CMS_OVERHEAD or
                offset + RECORD_HEADER.size + size + FOOTER.size != source.info.st_size):
            raise ValueError('invalid recovery footer or index bounds')
        stream.seek(offset)
        if RECORD_HEADER.unpack(_read_exact(stream, 8))[0] != size:
            raise ValueError('recovery index length mismatch')
        cipher.write_bytes(_read_exact(stream, size))
        _cms(cipher, plain, key, False, deadline)
        if plain.stat().st_size > INDEX_BYTES:
            raise ValueError('recovery index exceeds its bound')
        index = _checked_index(plain.read_bytes(), session, offset)
        whole = hashlib.sha256()
        with pending.open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            for entry in index['frames']:
                _remaining(deadline)
                stream.seek(entry['offset'])
                if RECORD_HEADER.unpack(_read_exact(stream, 8))[0] != entry['size']:
                    raise ValueError('recovery frame length mismatch')
                with cipher.open('wb') as incoming:
                    data = _read_exact(stream, entry['size'])
                    if hashlib.sha256(data).hexdigest() != entry['sha256']:
                        raise ValueError('recovery frame checksum mismatch')
                    incoming.write(data)
                del data
                _cms(cipher, plain, key, False, deadline)
                if plain.stat().st_size != PART_HEADER.size + entry['plaintext_size']:
                    raise ValueError('recovery plaintext frame length mismatch')
                with plain.open('rb') as incoming:
                    identity = PART_HEADER.unpack(_read_exact(incoming, PART_HEADER.size))
                    if identity != (PART_MAGIC, session, entry['ordinal'], entry['plaintext_size']):
                        raise ValueError('recovery plaintext frame identity mismatch')
                    part = hashlib.sha256()
                    while block := incoming.read(256 * 1024):
                        part.update(block)
                        whole.update(block)
                        output.write(block)
                    if part.hexdigest() != entry['plaintext_sha256']:
                        raise ValueError('recovery plaintext checksum mismatch')
            if output.tell() != index['payload_bytes'] or whole.hexdigest() != index['payload_sha256']:
                raise ValueError('recovery complete payload checksum mismatch')
            output.flush()
            os.fsync(output.fileno())
        source.unchanged()
        # The receipt hash is checked again on the same inode before exposure.
        if source.hash(deadline) != expected:
            raise ValueError('off-host ciphertext changed during authentication')
        _remaining(deadline)
        _publish_file(pending, destination)



def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def postgres_helper():
    spec = importlib.util.spec_from_file_location('postgres_recovery', Path(__file__).with_name('postgres-recovery.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restore(archive, target, expected=None, backend='pocketbase'):
    """Extract regular files only, bounding expansion and rejecting all links."""
    if backend not in {'pocketbase', 'postgresql'}:
        raise ValueError('unsupported recovery backend')
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('snapshot must be a regular archive')
    archive_hash = sha256(archive)
    if expected is not None and archive_hash != expected:
        raise ValueError('snapshot checksum mismatch')
    # tar can stop at its end marker without reading gzip's checksum/trailer.
    # Consume the complete compressed stream first so truncation cannot pass.
    with gzip.open(archive, 'rb') as compressed:
        total = 0
        while block := compressed.read(1024*1024):
            total += len(block)
            if total > MAX_BYTES + MAX_FILES * 4096:
                raise ValueError('compressed archive expansion exceeds its bound')
    if any(target.iterdir()):
        raise ValueError('restore target must be empty')
    files = {}
    seen = set()
    expanded = 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (not member.name or path.is_absolute() or '..' in path.parts or
                    str(path) != member.name or member.name in seen or
                    not (member.isdir() or member.isfile())):
                raise ValueError(f'unsafe or duplicate archive member: {member.name!r}')
            seen.add(member.name)
            expanded += member.size
            if len(seen) > MAX_FILES or expanded > MAX_BYTES or member.size < 0:
                raise ValueError('snapshot expansion exceeds its bound')
            destination = target / member.name
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if shutil.disk_usage(target).free < member.size + 512 * 1024**2:
                raise ValueError('insufficient disk headroom to restore snapshot member')
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, destination.open('xb') as output:
                shutil.copyfileobj(incoming, output, length=1024*1024)
            destination.chmod(0o600)
            if destination.stat().st_size != member.size:
                raise ValueError('archive member was truncated')
            files[member.name] = sha256(destination)
    if backend == 'postgresql':
        manifest = postgres_helper().validate_payload(target, files)
        return {'archive_sha256': archive_hash, 'expanded_bytes': expanded,
                'files': files, 'backend': backend, 'postgresql': manifest,
                'archive_validation': 'passed', 'database_restore': 'not_run', 'application_restore': 'not_run'}
    if 'data.db' not in files:
        raise ValueError('snapshot is missing PocketBase data.db')
    databases = {}
    for file in sorted(target.rglob('*.db')):
        # Open the COPY normally so SQLite reads/replays its WAL. immutable=1
        # would silently ignore committed data that still lives in that WAL.
        with closing(sqlite3.connect(file, timeout=10)) as database:
            database.execute('PRAGMA trusted_schema=OFF')
            result = database.execute('PRAGMA integrity_check').fetchall()
            if result != [('ok',)]:
                raise ValueError(f'SQLite integrity failure: {file.relative_to(target)}')
            tables = sorted(row[0] for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
            if file.name == 'data.db' and not {'_collections', '_migrations'} <= set(tables):
                raise ValueError('data.db is not the expected application database')
            databases[str(file.relative_to(target))] = {'integrity': 'ok', 'tables': len(tables)}
    return {'archive_sha256': archive_hash, 'expanded_bytes': expanded,
            'files': files, 'databases': databases}


def podman(*args):
    spec = importlib.util.spec_from_file_location('_tools_snapshot_runtime', Path(__file__).with_name('container_runtime.py'))
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    return runtime.output(list(args), timeout=30).strip()


def inspect(container):
    result = json.loads(podman('inspect', container))
    if len(result) != 1 or not re.fullmatch(r'[a-f0-9]{64}', result[0]['Id']):
        raise ValueError('database writer identity is ambiguous')
    return result[0]


def timestamp_ns(value):
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z', value)
    if not match:
        raise ValueError('Podman health timestamp is not recognized')
    seconds = int(datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
    return seconds * 10**9 + int((match[2] or '').ljust(9, '0'))


def resumed_health(container, boundary, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect(container)
        state = info['State']
        health = state.get('Health', {})
        if not state['Running'] or state['Paused'] or health.get('Status') not in {'starting', 'healthy', 'unhealthy'}:
            raise ValueError('resumed writer has no usable healthcheck')
        for probe in health.get('Log', []):
            started = timestamp_ns(probe['Start'])
            if (health['Status'] == 'healthy' and probe['ExitCode'] == 0 and started > boundary
                    and time.monotonic() < deadline):
                return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError('no successful health probe started after the writer resumed')


def direct_health(container, port, timeout=90):
    """Probe the inspected writer directly when an older image has no HEALTHCHECK."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect(container)
        if not info['State']['Running'] or info['State']['Paused']:
            raise ValueError('database writer did not resume')
        addresses = [value['IPAddress'] for value in info['NetworkSettings']['Networks'].values()
                     if value.get('IPAddress')]
        if len(addresses) != 1 or not ipaddress.ip_address(addresses[0]).is_private:
            raise ValueError('writer must have one private container address for a direct health probe')
        try:
            # Each request starts after unpause. Do not use a cached Podman status.
            request = urllib.request.Request(f'http://{addresses[0]}:{port}/api/health')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3) as response:
                if response.status == 200 and time.monotonic() < deadline:
                    return
        except OSError:
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError('database writer did not answer a fresh post-resume health request')


def snapshot(container, mount, destination, exclude_auxiliary=False, probe_port=None):
    info = inspect(container)
    state = info['State']
    if not state['Running'] or state['Paused'] or state.get('Restarting'):
        raise ValueError('snapshot requires a running, unpaused database writer')
    if probe_port is not None and not 1 <= probe_port <= 65535:
        raise ValueError('invalid direct health probe port')
    if probe_port is None and not info.get('Config', {}).get('Healthcheck', {}).get('Test'):
        raise ValueError('database writer must have a configured healthcheck')
    matches = [entry for entry in info['Mounts'] if entry['Destination'] == mount and entry['Type'] in {'volume', 'bind'}]
    if len(matches) != 1:
        raise ValueError('database data mount is missing or ambiguous')
    source = Path(matches[0]['Source']).resolve(strict=True)
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination = destination.resolve(strict=True)
    if destination == source or source in destination.parents or destination.stat().st_mode & 0o077:
        raise ValueError('backup destination must be private and outside the data volume')
    excluded = AUXILIARY if exclude_auxiliary else set()
    candidates = [path for path in source.rglob('*') if str(path.relative_to(source)) not in excluded]
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in candidates):
        raise ValueError('database volume contains links or special files')
    size = sum(path.stat().st_size for path in candidates if path.is_file())
    if len(candidates) > MAX_FILES or size > MAX_BYTES:
        raise ValueError('database snapshot exceeds its bound')
    if shutil.disk_usage(destination).free < 2 * size + 512 * 1024**2:
        raise ValueError('insufficient headroom for both the snapshot and its isolated restore')
    writer = info['Id']
    with tempfile.TemporaryDirectory(prefix='.pending-', dir=destination) as temporary:
        pending = Path(temporary)
        archive = pending / 'data.tar.gz'
        paused = False
        try:
            # A pause error is NEVER interpreted as evidence that a writer stopped.
            paused = True
            podman('pause', writer)
            if not inspect(writer)['State']['Paused']:
                raise ValueError('database writer did not pause')
            with tarfile.open(archive, 'w:gz', dereference=False) as output:
                count, copied = 0, 0
                for path in sorted(source.rglob('*')):
                    relative = str(path.relative_to(source))
                    if relative in excluded:
                        continue
                    metadata = output.gettarinfo(path, arcname=relative)
                    count += 1
                    copied += metadata.size
                    if count > MAX_FILES or copied > MAX_BYTES or copied > size + 256 * 1024**2:
                        raise ValueError('database grew beyond the reserved snapshot budget')
                    if not (metadata.isdir() or metadata.isfile()):
                        raise ValueError('database volume changed to contain a link or special file')
                    if metadata.isfile():
                        with path.open('rb') as stream:
                            output.addfile(metadata, stream)
                    else:
                        output.addfile(metadata)
        finally:
            if paused:
                podman('unpause', writer)
                boundary = time.time_ns()
                if probe_port is None:
                    resumed_health(writer, boundary)
                else:
                    direct_health(writer, probe_port)
        restored = pending / 'restored'
        restored.mkdir(mode=0o700)
        evidence = restore(archive, restored)
        evidence.update({'writer_id': writer, 'image_id': info['Image'], 'image_reference': info['Config']['Image'],
                         'excluded': sorted(excluded), 'verified_at': datetime.now(timezone.utc).isoformat()})
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + evidence['archive_sha256'][:12]
        final = destination / name
        final.mkdir(mode=0o700)
        archive.rename(final / 'data.tar.gz')
        (final / 'data.tar.gz').chmod(0o600)
        (final / 'verification.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(final)
        return final


def deployment_receipt(manifest):
    deployment = manifest.get('deployment')
    if deployment is None:
        return None
    release = deployment['release']
    return {'application': release['application'], 'source': release['source'],
            'release_digest': deployment['release_digest'], 'provider': deployment['provider']}


def seal(directory, recipient):
    """Encrypt verified product data; failed attempts leave no off-host pair."""
    output = directory / 'offhost'
    output.mkdir(mode=0o700)
    try:
        return _seal(directory, recipient, output)
    except BaseException:
        shutil.rmtree(output)
        raise


def _seal(directory, recipient, output):
    archive = directory / 'data.tar.gz'
    with _StableFile(directory / 'verification.json', 16 * 1024**2) as metadata:
        evidence_raw = metadata.stream.read()
        metadata.unchanged()
    evidence = _metadata(evidence_raw)
    backend = evidence.get('backend', 'pocketbase')
    if backend not in {'pocketbase', 'postgresql'}:
        raise ValueError('unsupported recovery backend')
    if sha256(archive) != evidence['archive_sha256']:
        raise ValueError('snapshot changed after verification')
    deployment = None
    if backend == 'postgresql':
        manifest = postgres_helper().validate_manifest(evidence.get('postgresql'))
        if manifest['engine']['reference'] != evidence['image_reference'] or manifest['engine']['image_id'] != evidence['image_id']:
            raise ValueError('sealed PostgreSQL engine differs from authenticated metadata')
        deployment = deployment_receipt(manifest)
    ciphertext = output / 'snapshot.cms'
    # Bind recovery metadata to the authenticated ciphertext too. A transport
    # receipt alone must not let a modified image reference select a restore binary.
    if shutil.disk_usage(directory).free < 2 * archive.stat().st_size + 512 * 1024**2:
        raise ValueError('insufficient headroom for authenticated off-host envelope')
    with tempfile.TemporaryDirectory(prefix='snapshot-envelope-', dir=directory) as temporary:
        envelope = Path(temporary) / 'envelope.tar'
        with _StableFile(archive, MAX_CIPHERTEXT) as original, tarfile.open(envelope, 'w', format=tarfile.USTAR_FORMAT) as bundle:
            member = tarfile.TarInfo('data.tar.gz')
            member.size, member.mode = original.info.st_size, 0o600
            bundle.addfile(member, original.stream)
            original.unchanged()
            if original.hash(time.monotonic() + CRYPTO_SECONDS) != evidence['archive_sha256']:
                raise ValueError('snapshot changed after verification')
            member = tarfile.TarInfo('verification.json')
            member.size, member.mode = len(evidence_raw), 0o600
            bundle.addfile(member, io.BytesIO(evidence_raw))
        _encrypt_payload(envelope, ciphertext, recipient)
    receipt = {key: evidence[key] for key in ['archive_sha256', 'image_id', 'image_reference', 'verified_at']}
    receipt['backend'] = backend
    if deployment is not None:
        receipt['deployment'] = deployment
    receipt['format'] = 2
    receipt['ciphertext_sha256'] = sha256(ciphertext)
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    (output / 'receipt.json').chmod(0o600)
    return output


def unseal(directory, key, target):
    """Authenticate/decrypt an off-host copy, then validate its isolated restore."""
    with _StableFile(directory / 'receipt.json', 64 * 1024) as receipt_file:
        receipt = _metadata(receipt_file.stream.read())
        receipt_file.unchanged()
    if (not isinstance(receipt, dict) or type(receipt.get('format')) is not int or receipt['format'] != 2 or
            not isinstance(receipt.get('ciphertext_sha256'), str) or
            not re.fullmatch(r'[a-f0-9]{64}', receipt['ciphertext_sha256'])):
        raise ValueError('unknown or invalid recovery envelope format')
    ciphertext = directory / 'snapshot.cms'
    with tempfile.TemporaryDirectory(prefix='snapshot-decrypt-') as temporary:
        if shutil.disk_usage(temporary).free < 2 * ciphertext.stat().st_size + 512 * 1024**2:
            raise ValueError('insufficient headroom to decrypt recovery envelope')
        envelope = Path(temporary) / 'envelope.tar'
        _decrypt_payload(ciphertext, envelope, key, receipt['ciphertext_sha256'])
        seen = set()
        with tarfile.open(envelope) as source:
            for member in source:
                if member.name not in {'data.tar.gz', 'verification.json'} or member.name in seen or not member.isfile():
                    raise ValueError('invalid encrypted recovery envelope')
                if member.size > (MAX_BYTES + 256 * 1024**2 if member.name == 'data.tar.gz' else 16 * 1024**2):
                    raise ValueError('recovery envelope entry exceeds bounds')
                seen.add(member.name)
                with source.extractfile(member) as incoming, (Path(temporary) / member.name).open('wb') as output:
                    shutil.copyfileobj(incoming, output)
        if seen != {'data.tar.gz', 'verification.json'} or receipt.get('format') != 2:
            raise ValueError('incomplete or unknown recovery envelope')
        evidence = _metadata((Path(temporary) / 'verification.json').read_bytes())
        for key in ['archive_sha256', 'image_id', 'image_reference', 'verified_at']:
            if evidence[key] != receipt[key]:
                raise ValueError('recovery metadata does not match authenticated envelope')
        backend = evidence.get('backend', 'pocketbase')
        if receipt.get('backend', 'pocketbase') != backend:
            raise ValueError('recovery backend does not match authenticated envelope')
        result = restore(Path(temporary) / 'data.tar.gz', target, evidence['archive_sha256'], backend=backend)
        if backend == 'postgresql' and (result['postgresql']['engine']['image_id'] != evidence['image_id'] or
                                       result['postgresql']['engine']['reference'] != evidence['image_reference']):
            raise ValueError('PostgreSQL engine differs from authenticated recovery metadata')
        if backend == 'postgresql' and receipt.get('deployment') != deployment_receipt(result['postgresql']):
            raise ValueError('recovery deployment or provider differs from the authenticated envelope')
        result.update({key: evidence[key] for key in ['image_id', 'image_reference', 'verified_at']})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    create = commands.add_parser('create')
    create.add_argument('--container', required=True)
    create.add_argument('--mount', required=True)
    create.add_argument('--destination', type=Path, required=True)
    create.add_argument('--exclude-auxiliary', action='store_true')
    create.add_argument('--probe-port', type=int)
    verify = commands.add_parser('verify')
    verify.add_argument('archive', type=Path)
    verify.add_argument('--sha256', required=True)
    verify.add_argument('--backend', choices=['pocketbase', 'postgresql'], default='pocketbase')
    encrypt = commands.add_parser('seal')
    encrypt.add_argument('directory', type=Path)
    encrypt.add_argument('--recipient', type=Path, required=True)
    decrypt = commands.add_parser('unseal')
    decrypt.add_argument('directory', type=Path)
    decrypt.add_argument('--key', type=Path, required=True)
    decrypt.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(number, _frame):
        raise InterruptedError(f'snapshot interrupted by signal {number}')
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    if args.operation == 'create':
        args.destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.destination / '.snapshot.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            snapshot(args.container, args.mount, args.destination, args.exclude_auxiliary, args.probe_port)
    elif args.operation == 'seal':
        print(seal(args.directory, args.recipient))
    elif args.operation == 'unseal':
        print(json.dumps(unseal(args.directory, args.key, args.target), indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix='snapshot-verify-') as temporary:
            print(json.dumps(restore(args.archive, Path(temporary), args.sha256, backend=args.backend), indent=2))


if __name__ == '__main__':
    main()
