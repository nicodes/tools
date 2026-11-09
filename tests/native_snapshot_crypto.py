#!/usr/bin/env python3
"""Exercise >2 GiB framed CMS under an actual finite Linux control cgroup."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import sqlite3
import tarfile
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SIZE = 2 * 1024**3 + 4 * 1024**2 + 17
MEMORY = 128 * 1024**2
RSS_BOUND = 64 * 1024**2


def run(args, **kwargs):
    return subprocess.run(args, check=True, stdin=subprocess.DEVNULL, timeout=1800, **kwargs)


def worker(root, report):
    spec = importlib.util.spec_from_file_location('snapshot_bulk', ROOT/'helpers/snapshot.py')
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)
    group = [line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')]
    if len(group) != 1 or not group[0].startswith('/system.slice/tools-snapshot-'):
        raise ValueError('bulk control must execute inside its owned finite unit')
    kernel = Path('/sys/fs/cgroup')/group[0].lstrip('/')
    limits = {name:(kernel/name).read_text().strip() for name in ('memory.max','memory.swap.max','pids.max','cpu.max')}
    quota, period = map(int, limits['cpu.max'].split())
    if limits['memory.max'] != str(MEMORY) or limits['memory.swap.max'] != '0' or limits['pids.max'] != '32' or quota != period:
        raise ValueError('actual crypto kernel limits differ from the declared control')
    if shutil.disk_usage(root).free < SIZE*4 + 1024**3:
        raise ValueError('insufficient room for the owned synthetic bulk roundtrip')
    stopped = threading.Event()
    peaks = {'rss_bytes':0, 'anonymous_bytes':0}
    def observe():
        while not stopped.is_set():
            rss = 0
            for pid in (kernel/'cgroup.procs').read_text().split():
                try:
                    for line in Path('/proc',pid,'status').read_text().splitlines():
                        if line.startswith('VmRSS:'):
                            rss += int(line.split()[1])*1024
                except (FileNotFoundError, ProcessLookupError):
                    pass
            values = dict(line.split() for line in (kernel/'memory.stat').read_text().splitlines())
            peaks['rss_bytes'] = max(peaks['rss_bytes'],rss)
            peaks['anonymous_bytes'] = max(peaks['anonymous_bytes'],int(values['anon']))
            stopped.wait(.01)
    observer = threading.Thread(target=observe,daemon=True)
    observer.start()
    started = time.monotonic()
    try:
        key, cert = root/'key.pem', root/'cert.pem'
        run(['openssl','req','-x509','-newkey','rsa:4096','-noenc','-keyout',str(key),'-out',str(cert),
             '-days','1','-subj','/CN=disposable-bulk-control'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        package = root/'package'
        package.mkdir(mode=0o700)
        database = package/'data.db'
        with sqlite3.connect(database) as connection:
            connection.executescript('CREATE TABLE _collections (name TEXT); CREATE TABLE _migrations (name TEXT);')
        digest = hashlib.sha256()
        class SyntheticReader:
            def read(self, size):
                block = os.urandom(size)
                digest.update(block)
                return block
        archive = package/'data.tar.gz'
        # Level 0 prevents synthetic bulk from collapsing into a small archive.
        # Generate directly into the tar: no second unencrypted bulk source copy.
        with tarfile.open(archive,'w:gz',compresslevel=0,copybufsize=1024**2) as bundle:
            bundle.add(database,arcname='data.db')
            member = tarfile.TarInfo('storage/synthetic.bin')
            member.size = SIZE
            bundle.addfile(member,SyntheticReader())
        evidence = {'archive_sha256':snapshot.sha256(archive),'image_id':'sha256:'+'a'*64,
                    'image_reference':'ghcr.io/example/fixture-db@sha256:'+'b'*64,
                    'verified_at':'2026-11-09T00:00:00+00:00'}
        (package/'verification.json').write_text(json.dumps(evidence))
        pair = snapshot.seal(package,cert)
        cipher, receipt = pair/'snapshot.cms',pair/'receipt.json'
        encrypted_size = cipher.stat().st_size
        if archive.stat().st_size <= 2*1024**3 or encrypted_size <= 2*1024**3:
            raise ValueError('bulk fixture did not exercise actual multi-gigabyte payloads')
        # The ordinary unseal API needs only the two off-host files.
        archive.unlink()
        restored = root/'roundtrip'
        restored.mkdir(mode=0o700)
        accepted = snapshot.unseal(pair,key,restored)
        if accepted['archive_sha256'] != evidence['archive_sha256'] or snapshot.sha256(restored/'storage/synthetic.bin') != digest.hexdigest():
            raise ValueError('large authenticated product roundtrip changed synthetic bytes')
        shutil.rmtree(restored)
        # Recompute the transport checksum after deliberate tampering: the
        # authenticated framing itself, not merely the receipt, must refuse.
        middle = encrypted_size//2
        with cipher.open('r+b') as output:
            output.seek(middle)
            byte = output.read(1)
            output.seek(middle)
            output.write(bytes([byte[0]^1]))
        for attack in ('changed-frame','truncated'):
            if attack == 'truncated':
                with cipher.open('r+b') as output:
                    output.truncate(cipher.stat().st_size-1)
            transport = json.loads(receipt.read_text())
            transport['ciphertext_sha256'] = snapshot.sha256(cipher)
            receipt.write_text(json.dumps(transport))
            restored.mkdir(mode=0o700)
            try:
                snapshot.unseal(pair,key,restored)
            except ValueError:
                pass
            else:
                raise ValueError('large tampered ciphertext was accepted')
            if list(restored.iterdir()) or list(pair.glob('.snapshot-*')):
                raise ValueError('refused bulk authentication exposed plaintext or scratch')
            restored.rmdir()
        events = dict(line.split() for line in (kernel/'memory.events').read_text().splitlines())
        if int(events['oom']) or int(events['oom_kill']):
            raise ValueError('crypto hit its memory limit')
        self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss*1024
        if max(peaks.values()) > RSS_BOUND or self_rss > RSS_BOUND or child_rss > RSS_BOUND:
            raise ValueError(f'crypto resident memory exceeded 64 MiB: {peaks}, parent={self_rss}, child={child_rss}')
        evidence = {'kind':'komizo.snapshot-bulk-control','version':1,'plaintext_bytes':SIZE,
                    'roundtrip_sha256':digest.hexdigest(),'ciphertext_bytes':encrypted_size,
                    'frame_bytes':snapshot.FRAME_BYTES,'format':2,'kernel_limits':limits,
                    'measured':{**peaks,'parent_maxrss_bytes':self_rss,'child_maxrss_bytes':child_rss,
                                'cgroup_memory_peak_bytes':int((kernel/'memory.peak').read_text())},
                    'seconds':round(time.monotonic()-started,3),'changed_frame_refused':True,
                    'truncation_refused':True,'no_plaintext_exposed_on_failure':True}
        report.write_text(json.dumps(evidence,sort_keys=True)+'\n')
        print(json.dumps(evidence,sort_keys=True),flush=True)
    finally:
        stopped.set()
        observer.join(timeout=2)
        # Temporary fixture keys, plaintext and bulk ciphertext never leave CI.
        for path in root.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--root',type=Path)
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    if args.worker:
        os.umask(0o077)
        worker(args.root,args.report)
        return
    if os.geteuid() == 0:
        raise ValueError('launch the control as the unprivileged CI user; sudo is scoped to its unit')
    output = ROOT/'.artifacts/snapshot-bulk.json'
    output.parent.mkdir(mode=0o700,exist_ok=True)
    if output.exists():
        raise ValueError('refusing to replace previous bulk evidence')
    unit = 'tools-snapshot-'+secrets.token_hex(8)
    with tempfile.TemporaryDirectory(prefix='tools-snapshot-') as directory:
        root = Path(directory)
        try:
            run(['sudo','-n','systemd-run','--unit',unit,'--wait','--pipe','--collect',
                 '-p',f'User={os.getuid()}','-p',f'Group={os.getgid()}',
                 '-p',f'MemoryMax={MEMORY}','-p','MemorySwapMax=0','-p','CPUQuota=100%',
                 '-p','TasksMax=32','-p','RuntimeMaxSec=1500','-p','KillMode=control-group',
                 '/usr/bin/python3','-I','-B',str(Path(__file__).resolve()),'--worker','--root',str(root),'--report',str(output)])
        finally:
            subprocess.run(['sudo','-n','systemctl','stop',unit],check=False,timeout=30,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            state = subprocess.run(['systemctl','is-active',unit],check=False,timeout=10,capture_output=True,text=True)
            if state.stdout.strip() not in {'inactive','failed','unknown'}:
                raise ValueError('owned crypto unit remained active after completion')
        if list(root.iterdir()):
            raise ValueError('owned bulk plaintext or key survived control cleanup')
    if not output.is_file():
        raise ValueError('bulk control produced no resource evidence')


if __name__ == '__main__':
    main()
