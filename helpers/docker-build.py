#!/usr/bin/env python3
"""Build and load caller-selected images; cache only explicitly selected components."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-key', required=True)
    parser.add_argument('--local-base')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]*', args.cache_key):
        parser.error('cache key must be a component slug')
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    if not arguments or any(arg == '--push' or arg.startswith(('--output', '--cache-to', '--cache-from')) for arg in arguments):
        parser.error('adapter must build local images without publication or its own cache flags')
    if args.local_base:
        bases = []
        for position, argument in enumerate(arguments):
            value = (arguments[position + 1] if argument == '--build-arg' and position + 1 < len(arguments)
                     else argument[len('--build-arg='):] if argument.startswith('--build-arg=') else '')
            if value.startswith('BASE_IMAGE='):
                bases.append(value[len('BASE_IMAGE='):])
        if (not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*:[a-f0-9]{40}', args.local_base)
                or bases != [args.local_base]
                or any(arg in {'--pull', '--builder'} or arg.startswith(('--pull=', '--builder=')) for arg in arguments)):
            parser.error('local base requires one exact native source image and the embedded Docker builder')
        identity = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.local_base],
            text=True, timeout=60).strip()
        if not re.fullmatch('sha256:[a-f0-9]{64}', identity):
            raise ValueError('local base requires an actually loaded immutable Docker image')
        subprocess.run(['docker', 'buildx', 'build', '--builder', 'default', '--load', *arguments], check=True)
        return
    directory = os.environ.get('FLEET_BUILDKIT_CACHE')
    if not directory:
        subprocess.run(['docker', 'build', *arguments], check=True)
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    cache = root/args.cache_key
    if root.is_symlink() or cache.is_symlink():
        raise ValueError('build cache must not be a symlink')
    with (root/(args.cache_key+'.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix=args.cache_key+'-', dir=root) as fresh:
            flags = ['--cache-to', 'type=local,dest='+fresh+',mode=max']
            if (cache/'index.json').is_file():
                flags += ['--cache-from', 'type=local,src='+str(cache)]
            subprocess.run(['docker', 'buildx', 'build', '--load', *flags, *arguments], check=True)
            # Failed builds leave the previous cache intact. No cache is a proof
            # of artifact identity: the contract and artifact checks still run.
            if cache.exists():
                shutil.rmtree(cache)
            Path(fresh).rename(cache)


if __name__ == '__main__':
    main()
