#!/usr/bin/env python3
"""Record and publish the exact container images validated by the Build gate."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def references(project, revision, components, image_base):
    if not re.fullmatch(r'[a-z][a-z0-9-]*', project):
        raise ValueError('invalid project name')
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('release revision must be a full commit')
    if not components or len(set(components)) != len(components):
        raise ValueError('a release needs distinct image components')
    if any(not re.fullmatch(r'[a-z][a-z0-9-]*', component) for component in components):
        raise ValueError('invalid image component')
    if not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*', image_base):
        raise ValueError('image base must be an explicit registry repository prefix')
    return [f'{image_base}-{component}:{revision}' for component in components]



def inspect(refs):
    images = json.loads(subprocess.check_output(['docker', 'image', 'inspect', *refs], timeout=60))
    if len(images) != len(refs):
        raise ValueError('Docker did not return every release image')
    result = {}
    for ref, image in zip(refs, images, strict=True):
        if ref not in image.get('RepoTags', []) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image['Id']):
            raise ValueError(f'image identity is missing: {ref}')
        result[ref] = image['Id']
    return result


def validate(manifest, project, revision, components, archive, image_base):
    refs = references(project, revision, components, image_base)
    if manifest.get('revision') != revision or manifest.get('project') != project:
        raise ValueError('release belongs to another project or commit')
    if set(manifest.get('images', {})) != set(refs):
        raise ValueError('release image set differs from the declared components')
    if not all(re.fullmatch(r'sha256:[a-f0-9]{64}', value) for value in manifest['images'].values()):
        raise ValueError('release image ID is invalid')
    if not archive.is_file() or archive.is_symlink() or manifest.get('archive_sha256') != digest(archive):
        raise ValueError('release archive is missing or differs from the validated artifact')
    return refs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['record', 'publish'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--image-base')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--components', nargs='+', required=True)
    parser.add_argument('--directory', type=Path, default=Path('.artifacts/release'))
    args = parser.parse_args()
    if args.image_base is None:
        declaration = json.loads(Path('engineering.json').read_text())['release']
        if set(declaration) != {'project', 'image_base'} or declaration['project'] != args.project:
            raise ValueError('release project differs from the caller-owned declaration')
        args.image_base = declaration['image_base']
    refs = references(args.project, args.revision, args.components, args.image_base)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, timeout=10).strip()
    if head != args.revision:
        raise ValueError('release revision must match the checked-out commit')
    archive = args.directory/'images.tar.gz'
    manifest_file = args.directory/'release.json'
    if args.operation == 'record':
        manifest = {'project': args.project, 'revision': args.revision, 'images': inspect(refs),
                    'archive_sha256': digest(archive)}
        manifest_file.write_text(json.dumps(manifest, indent=2)+'\n')
        validate(manifest, args.project, args.revision, args.components, archive, args.image_base)
    else:
        # CD checks out the merged commit. A modified tracked source cannot publish.
        subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], check=True, timeout=30)
        manifest = json.loads(manifest_file.read_text())
        validate(manifest, args.project, args.revision, args.components, archive, args.image_base)
        subprocess.run(['docker', 'load', '--input', str(archive)], check=True, timeout=600)
        if inspect(refs) != manifest['images']:
            raise ValueError('loaded image IDs differ from the Build gate evidence')
        for ref in refs:
            subprocess.run(['docker', 'push', ref], check=True, timeout=600)
    print(f'{args.operation}: {args.project} {args.revision}: {len(refs)} validated images')


if __name__ == '__main__':
    main()
