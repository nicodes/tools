#!/usr/bin/env python3
"""Require live acceptance of earlier caller-owned cohorts before adoption."""
import base64
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import urlencode


def api(path):
    result = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('GitHub acceptance evidence unavailable: '+path)
    return json.loads(result.stdout)


def content(repository, revision, path):
    response = api(f'repos/{repository}/contents/{path}?ref={revision}')
    if response.get('encoding') != 'base64':
        raise ValueError('acceptance requires GitHub source content')
    return base64.b64decode(response['content']).decode()


def validate_plan(plan):
    if plan.get('version') != 1 or not isinstance(plan.get('cohorts'), list) or not plan['cohorts']:
        raise ValueError('rollout requires a version 1 cohort policy')
    seen = set()
    for cohort in plan['cohorts']:
        if not isinstance(cohort, list) or not cohort:
            raise ValueError('rollout cohorts must be nonempty')
        for member in cohort:
            repo = member.get('repository', '')
            if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or repo in seen:
                raise ValueError('rollout repository is invalid or repeated')
            seen.add(repo)
            if not isinstance(member.get('workflows'), list) or not member['workflows']:
                raise ValueError('each consumer requires workflow acceptance')
            for workflow in member['workflows']:
                if (not re.fullmatch(r'[\w-]+\.yml', workflow.get('file', ''))
                        or type(workflow.get('max_age_hours')) is not int or workflow['max_age_hours'] <= 0
                        or type(workflow.get('exact_main', True)) is not bool
                        or workflow.get('source', 'main') not in ('main', 'merged-pr')
                        or ('job' in workflow and not isinstance(workflow['job'], str))):
                    raise ValueError('workflow acceptance requires a filename and positive freshness bound')


def verify_consumer(member, kind, version, revision, checksum, now):
    repo = member['repository']
    head = api(f'repos/{repo}/commits/main')['sha']
    if kind == 'tools':
        pin = json.loads(content(repo, head, 'engineering-pin.json'))
        mise = tomllib.loads(content(repo, head, '.mise.toml'))['tools']['http:cicd-engineering']
        if pin.get('revision') != revision or mise.get('version') != version or mise.get('checksum') != 'sha256:'+checksum:
            raise ValueError(repo+': merged main has not adopted the target engineering release')
    else:
        pin = json.loads(content(repo, head, 'ACTION-PINS.json'))
        if (pin.get('repository') != 'https://github.com/nicodes/komizo-actions' or not pin.get('pins')
                or any(value.get('sha') != revision or value.get('tag') != 'v'+version for value in pin['pins'].values())):
            raise ValueError(repo+': merged main has not adopted the target action release')
    evidence = []
    for requirement in member['workflows']:
        expected_revision = head
        query = {'branch': 'main', 'per_page': 10}
        if requirement.get('source') == 'merged-pr':
            pulls = api(f'repos/{repo}/commits/{head}/pulls')
            candidates = [pull for pull in pulls if pull.get('merge_commit_sha') == head and pull.get('merged_at')
                          and pull.get('base', {}).get('ref') == 'main'
                          and pull.get('head', {}).get('repo', {}).get('full_name') == repo]
            if len(candidates) != 1:
                raise ValueError(repo+': cannot establish the merged canary PR identity')
            expected_revision = candidates[0]['head']['sha']
            query = {'event': 'pull_request', 'per_page': 10}
        if requirement.get('exact_main', True):
            query['head_sha'] = expected_revision
        runs = api(f'repos/{repo}/actions/workflows/{requirement["file"]}/runs?'+urlencode(query))['workflow_runs']
        if requirement.get('job'):
            selected = []
            for run in runs:
                jobs = api(f'repos/{repo}/actions/runs/{run["id"]}/jobs?per_page=100')['jobs']
                matching = [job for job in jobs if job.get('name') == requirement['job']]
                # Closing a PR runs cleanup with preview deployment skipped.
                if matching and matching[0].get('conclusion') != 'skipped':
                    selected = [run] if len(matching) == 1 and matching[0].get('conclusion') == 'success' else []
                    break
            runs = selected
        if not runs:
            raise ValueError(repo+': required acceptance workflow has no run: '+requirement['file'])
        run = runs[0]
        stamp = datetime.datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
        age = (now-stamp).total_seconds()/3600
        if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
                or not 0 <= age <= requirement['max_age_hours']
                or (requirement.get('exact_main', True) and run['head_sha'] != expected_revision)):
            raise ValueError(repo+': acceptance is failed, pending, stale or from another source: '+requirement['file'])
        evidence.append({key: run.get(key) for key in ('id', 'head_sha', 'run_attempt', 'html_url', 'updated_at')})
    return {'repository': repo, 'main_revision': head, 'workflows': evidence}


def gate(policy_path, cohort_index, repositories, kind, version, revision, checksum=None):
    raw = policy_path.read_bytes()
    plan = json.loads(raw)
    validate_plan(plan)
    if type(cohort_index) is not int or not 0 <= cohort_index < len(plan['cohorts']):
        raise ValueError('select an existing zero-based cohort')
    allowed = {member['repository'] for member in plan['cohorts'][cohort_index]}
    if set(repositories) != allowed or len(repositories) != len(allowed):
        raise ValueError('adoption must select exactly the reviewed cohort')
    now = datetime.datetime.now(datetime.timezone.utc)
    accepted = [verify_consumer(member, kind, version, revision, checksum, now)
                for cohort in plan['cohorts'][:cohort_index] for member in cohort]
    return {'version': 1, 'policy_sha256': hashlib.sha256(raw).hexdigest(),
            'checked_at': now.isoformat(), 'kind': kind, 'release_version': version,
            'release_revision': revision, 'cohort': cohort_index, 'accepted_consumers': accepted}
