#!/usr/bin/env python3
"""Record and publish the exact container images validated by the Build gate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import contract


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


# Only public execution identifiers are recorded. Build environment values are
# hashed by the engineering contract, never included in this manifest.
CONTEXT_KEYS = ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID',
                'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'GITHUB_SHA',
                'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME')
CONFIG_FILES = ('.mise.toml', 'engineering.json', 'engineering-profile.json',
                'engineering-pin.json', 'fleet-policy.json')


def source_binding():
    tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], text=True, timeout=10).strip()
    clean = subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode
    if clean not in (0, 1):
        raise ValueError('cannot determine tracked source state')
    return {'source_tree': tree, 'tracked_source_clean': clean == 0,
            'configuration_sha256': {name: digest(Path(name)) for name in CONFIG_FILES if Path(name).is_file()}}


def source_relationship(revision):
    relationship = {'source_commit': revision, 'workflow_commit': os.environ.get('GITHUB_SHA') or None,
                    'pull_request_head': None, 'pull_request_base': None, 'pull_request_merge': None}
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    if event_path:
        request = json.loads(Path(event_path).read_text()).get('pull_request')
        if request is not None:
            values = {'pull_request_head': request.get('head', {}).get('sha'),
                      'pull_request_base': request.get('base', {}).get('sha'),
                      'pull_request_merge': request.get('merge_commit_sha')}
            for key, value in values.items():
                if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}', value)):
                    raise ValueError('invalid pull-request commit relationship')
                relationship[key] = value
    return relationship


def build_provenance(revision):
    return {'version': 1, 'source_commit': revision, **source_binding(),
            'input_identity': contract.identity(contract.load(Path('engineering.json'))),
            # Build and Test may use different jobs/checkouts. Do not claim
            # head was tested merely because a workflow tests a merge commit.
            'tested_commit': None, 'source_relationship': source_relationship(revision),
            'build_context': {key: os.environ[key] for key in CONTEXT_KEYS if os.environ.get(key)}}


def validate_provenance(manifest, revision):
    if os.environ.get('GITHUB_SHA') and os.environ['GITHUB_SHA'] != revision:
        raise ValueError('publisher workflow commit differs from the release source')
    proof = manifest.get('provenance')
    if not isinstance(proof, dict) or proof.get('version') != 1 or proof.get('source_commit') != revision:
        raise ValueError('release has no matching build source provenance; rebuild with this engineering baseline')
    if proof.get('tracked_source_clean') is not True:
        raise ValueError('release was built from modified tracked source; rebuild the committed source')
    binding = source_binding()
    if any(proof.get(key) != value for key, value in binding.items()):
        raise ValueError('release source tree or pinned configuration differs from the publisher checkout')
    if not isinstance(proof.get('input_identity'), str) or not re.fullmatch(r'[a-f0-9]{64}', proof['input_identity']):
        raise ValueError('release has no valid build input identity')
    context = proof.get('build_context')
    if not isinstance(context, dict) or set(context) - set(CONTEXT_KEYS):
        raise ValueError('invalid build execution context')
    # A failed publish can be retried using the prior successful Build artifact
    # in the same run. Different runs/repositories are never interchangeable.
    for key in ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID'):
        if context.get(key) != (os.environ.get(key) or None):
            raise ValueError('release artifact belongs to another workflow execution')
    built, current = context.get('GITHUB_RUN_ATTEMPT'), os.environ.get('GITHUB_RUN_ATTEMPT')
    if built or current:
        if not built or not current or not built.isdecimal() or not current.isdecimal() or not 1 <= int(built) <= int(current):
            raise ValueError('release artifact has an invalid workflow attempt')
    return proof


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
                    'archive_sha256': digest(archive), 'provenance': build_provenance(args.revision)}
        manifest_file.write_text(json.dumps(manifest, indent=2)+'\n')
        validate(manifest, args.project, args.revision, args.components, archive, args.image_base)
    else:
        # CD checks out the merged commit. A modified tracked source cannot publish.
        subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--'], check=True, timeout=30)
        manifest = json.loads(manifest_file.read_text())
        validate(manifest, args.project, args.revision, args.components, archive, args.image_base)
        proof = validate_provenance(manifest, args.revision)
        subprocess.run(['docker', 'load', '--input', str(archive)], check=True, timeout=600)
        if inspect(refs) != manifest['images']:
            raise ValueError('loaded image IDs differ from the Build gate evidence')
        for ref in refs:
            subprocess.run(['docker', 'push', ref], check=True, timeout=600)
        publisher = {'version': 1, 'revision': args.revision, 'archive_sha256': manifest['archive_sha256'],
                     'images': manifest['images'], 'build_provenance': proof,
                     'publisher_context': {key: os.environ[key] for key in CONTEXT_KEYS if os.environ.get(key)}}
        (args.directory/'publication.json').write_text(json.dumps(publisher, indent=2)+'\n')
    print(f'{args.operation}: {args.project} {args.revision}: {len(refs)} validated images')


if __name__ == '__main__':
    main()
