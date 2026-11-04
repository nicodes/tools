#!/usr/bin/env python3
"""Bind a requested merge to an exact pull-request head, base and tested tree."""
import argparse
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

SHA = re.compile(r'[0-9a-f]{40}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')


def exact_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('expected an exact Git object identity')
    return value


def snapshot(repository, pull, candidate_commit):
    if not REPOSITORY.fullmatch(repository):
        raise ValueError('invalid repository identity')
    if pull.get('state') != 'open' or pull.get('draft'):
        raise ValueError('merge request requires an open non-draft pull request')
    if pull['base']['repo']['full_name'] != repository:
        raise ValueError('pull request belongs to another repository')
    if pull['base']['ref'] != 'main':
        raise ValueError('merge request must target main')
    if pull.get('mergeable') is not True:
        raise ValueError('pull request is conflicting or its merge candidate is not ready')
    head = exact_sha(pull['head']['sha'])
    base = exact_sha(pull['base']['sha'])
    candidate = exact_sha(pull['merge_commit_sha'])
    if candidate_commit['sha'] != candidate:
        raise ValueError('candidate commit differs from pull request merge identity')
    parents = [parent['sha'] for parent in candidate_commit['parents']]
    if parents != [base, head]:
        raise ValueError('candidate does not combine the requested base and head')
    return {'version': 1, 'repository': repository, 'pull_request': pull['number'],
            'head': head, 'base': base, 'candidate': candidate,
            'tree': exact_sha(candidate_commit['tree']['sha'])}


def current_candidate(receipt, pull, base):
    if pull.get('number') != receipt['pull_request']:
        raise ValueError('receipt names another pull request')
    if pull.get('state') != 'open' or pull.get('draft'):
        raise ValueError('pull request is no longer eligible to merge')
    if pull['base']['repo']['full_name'] != receipt['repository'] or pull['base']['ref'] != 'main':
        raise ValueError('pull request target changed')
    if pull['head']['sha'] != receipt['head']:
        raise ValueError('pull request head changed; fresh validation required')
    if base != receipt['base'] or pull['base']['sha'] != receipt['base']:
        raise ValueError('base changed; fresh validation required')
    if pull.get('mergeable') is not True or pull.get('merge_commit_sha') != receipt['candidate']:
        raise ValueError('merge candidate changed; fresh validation required')


def tested_candidate(receipt, proof):
    expected = {'repository': receipt['repository'], 'source_commit': receipt['candidate'],
                'source_tree': receipt['tree'], 'head': receipt['head'], 'base': receipt['base']}
    if any(proof.get(key) != value for key, value in expected.items()):
        raise ValueError('validation proof belongs to another candidate')
    if proof.get('status') != 'passed' or proof.get('tracked_source_clean') is not True:
        raise ValueError('validation did not pass for clean source')
    if proof.get('build_verified') is not True or proof.get('tests_verified') is not True:
        raise ValueError('candidate lacks build and test acceptance')


def api(path):
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('authenticated GitHub access required')
    request = urllib.request.Request('https://api.github.com/' + path, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'tools-merge-candidate'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError('GitHub response exceeds its bound')
        return json.loads(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['snapshot', 'verify'])
    parser.add_argument('--repository', required=True)
    parser.add_argument('--pull-request', type=int, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--proof', type=Path)
    args = parser.parse_args()
    try:
        if not REPOSITORY.fullmatch(args.repository) or not 0 < args.pull_request < 10**10:
            raise ValueError('invalid merge request identity')
        pull = api(f'repos/{args.repository}/pulls/{args.pull_request}')
        if args.operation == 'snapshot':
            candidate = exact_sha(pull.get('merge_commit_sha'))
            commit = api(f'repos/{args.repository}/git/commits/{candidate}')
            result = snapshot(args.repository, pull, commit)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(result, indent=2) + '\n')
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    for name in ['head', 'base', 'candidate', 'tree']:
                        output.write(name + '=' + result[name] + '\n')
        else:
            receipt = json.loads(args.receipt.read_text())
            if receipt['repository'] != args.repository or receipt['pull_request'] != args.pull_request:
                raise ValueError('receipt belongs to another request')
            base = api(f'repos/{args.repository}/branches/main')['commit']['sha']
            current_candidate(receipt, pull, base)
            if args.proof is None:
                raise ValueError('current candidate also requires build/test proof')
            tested_candidate(receipt, json.loads(args.proof.read_text()))
            print('Current head, base, candidate and clean build/test proof match.')
    except (OSError, ValueError, KeyError, TypeError, urllib.error.HTTPError) as error:
        # GitHub exception response bodies and authentication values are never printed.
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
