#!/usr/bin/env python3
"""Build local OCI images with native Podman layers and owned cache records."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile

_spec = importlib.util.spec_from_file_location('_tools_build_runtime', Path(__file__).with_name('container_runtime.py'))
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)


def selected_tags(arguments):
    tags = []
    for i, argument in enumerate(arguments):
        if argument in {'-t', '--tag'}:
            if i + 1 >= len(arguments):
                raise ValueError('image tag requires a reference')
            tags.append(arguments[i + 1])
        elif argument.startswith('--tag='):
            tags.append(argument.split('=', 1)[1])
    if (not 1 <= len(tags) <= 16 or len(set(tags)) != len(tags)
            or any(not re.fullmatch(r'[a-z0-9][a-z0-9.:/_-]*', tag) or '/' not in tag for tag in tags)):
        raise ValueError('builds require distinct explicit registry image tags')
    return tags


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-key', required=True)
    parser.add_argument('--local-base')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]*', args.cache_key):
        parser.error('cache key must be a component slug')
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    forbidden = {'--push', '--load', '--builder', '--output', '--cache-to', '--cache-from', '--format', '--layers', '--authfile'}
    if not arguments or any(value.split('=', 1)[0] in forbidden for value in arguments):
        parser.error('builds produce local OCI images without publication or caller-selected engine/cache adapters')
    tags = selected_tags(arguments)
    if args.local_base:
        bases = []
        for i, argument in enumerate(arguments):
            value = (arguments[i + 1] if argument == '--build-arg' and i + 1 < len(arguments)
                     else argument[len('--build-arg='):] if argument.startswith('--build-arg=') else '')
            if value.startswith('BASE_IMAGE='):
                bases.append(value[len('BASE_IMAGE='):])
        if (not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*:[a-f0-9]{40}', args.local_base)
                or bases != [args.local_base]
                or any(value.split('=', 1)[0] == '--pull' for value in arguments)):
            parser.error('local base requires one exact loaded source image without a pull override')
        runtime.inspect_images([args.local_base])
    root = Path(os.environ.get('TOOLS_IMAGE_CACHE', '.artifacts/image-cache')).absolute()
    if any(parent.is_symlink() for parent in [root, *root.parents]):
        raise ValueError('image cache must not traverse links')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = root / (args.cache_key + '.lock')
    if lock_path.is_symlink():
        raise ValueError('image cache lock must not be a link')
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runtime.run(['build', '--format', 'oci', '--layers', *(['--pull=never'] if args.local_base else []), *arguments], timeout=3600, check=True)
        images = runtime.inspect_images(tags)
        record = {'version': 1, 'inputs_sha256': hashlib.sha256('\0'.join(arguments).encode()).hexdigest(),
                  'images': {tag: image['Id'] for tag, image in zip(tags, images, strict=True)}}
        # This record describes the successful local build. Native layer cache
        # remains engine-owned and never grants artifact or source authority.
        with tempfile.NamedTemporaryFile(mode='w', prefix=args.cache_key + '-', dir=root, delete=False) as output:
            fresh = Path(output.name)
            json.dump(record, output, sort_keys=True)
            output.write('\n')
        fresh.replace(root / (args.cache_key + '.json'))


if __name__ == '__main__':
    main()
