#!/usr/bin/env python3
"""Durably plan, execute and reconcile a narrowly authorized failed-workflow rerun."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
SHA = re.compile(r'[a-f0-9]{40}')
TERMINAL = {'succeeded', 'failed', 'rejected'}


def api(path, method='GET'):
    result = subprocess.run(['gh', 'api', '--method', method, path],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('GitHub operation unavailable')
    return json.loads(result.stdout) if result.stdout.strip() else None


def policy(path, expected_digest):
    with Path(path).open('rb') as stream:
        raw = stream.read(1024*1024+1)
    if len(raw) > 1024*1024 or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('operator policy differs from the independently reviewed digest')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate operation policy field')
            result[key] = item
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if set(value) != {'version', 'repositories'} or value['version'] != 1 or not value['repositories']:
        raise ValueError('unsupported operation policy')
    for repo, entry in value['repositories'].items():
        if (not REPO.fullmatch(repo) or set(entry) != {'rerun_workflows'}
                or not isinstance(entry['rerun_workflows'], list) or not entry['rerun_workflows']
                or any(not re.fullmatch(r'[a-z][a-z0-9-]*\.yml', workflow) for workflow in entry['rerun_workflows'])):
            raise ValueError('operation policy must explicitly allow repository/workflow pairs')
    return value


class Journal:
    """One protected broker database; SQLite serializes competing local workers."""

    def __init__(self, path):
        path = Path(path).absolute()
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o700)
        parent = path.parent.stat()
        if path.parent.is_symlink() or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError('journal directory must be owned by the operator with mode 0700')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('journal must be an operator-owned regular mode-0600 file')
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL,
                intent TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                worker TEXT, lease_until REAL, result TEXT);
            CREATE TABLE IF NOT EXISTS locks (scope TEXT PRIMARY KEY, operation_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                at REAL NOT NULL, state TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def event(self, identity, state, now):
        self.db.execute('INSERT INTO events(operation_id, at, state) VALUES (?, ?, ?)', (identity, now, state))

    def get(self, identity):
        row = self.db.execute('SELECT * FROM operations WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('unknown operation identity')
        result = dict(row)
        result['intent'] = json.loads(result['intent'])
        result['result'] = json.loads(result['result']) if result['result'] else None
        events = list(self.db.execute('SELECT at, state FROM events WHERE operation_id=? ORDER BY sequence', (identity,)))
        start = next((event['at'] for event in events if event['state'] == 'executing'), None)
        finished = next((event['at'] for event in reversed(events) if event['state'] in TERMINAL), None)
        result['telemetry'] = {'queue_seconds': round(start-row['created'], 3) if start is not None else None,
            'operation_seconds': round(finished-start, 3) if finished is not None and start is not None else None,
            'scope': 'Broker queue and end-to-end operation duration, not runner execution or billed time'}
        return result

    def export(self, now=None):
        now = time.time() if now is None else now
        rows = [self.get(row[0]) for row in self.db.execute('SELECT id FROM operations ORDER BY created, id')]
        return {'version': 1, 'observed_at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
                'execution_scope': 'single-broker', 'operations': rows}

    def prepare(self, intent, now=None, ttl=900):
        now = time.time() if now is None else now
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise ValueError('operation intent expires within one hour')
        identity = uuid.uuid4().hex
        scope = intent['repository']+':production'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)',
                (identity, scope, 'prepared', json.dumps(intent, sort_keys=True), now, now+ttl))
            self.event(identity, 'prepared', now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.get(identity)

    def claim(self, identity, now=None, lease_seconds=120):
        now = time.time() if now is None else now
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError('execution lease must be between one second and one hour')
        worker = uuid.uuid4().hex
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.get(identity)
            if row['state'] != 'prepared' or now >= row['expires']:
                raise ValueError('intent is expired or already claimed; inspect/reconcile it instead of retrying')
            try:
                self.db.execute('INSERT INTO locks VALUES (?, ?)', (row['scope'], identity))
            except sqlite3.IntegrityError as error:
                raise ValueError('another operation holds this production scope') from error
            self.db.execute('UPDATE operations SET state=?, worker=?, lease_until=? WHERE id=?',
                            ('executing', worker, now+lease_seconds, identity))
            self.event(identity, 'executing', now)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return worker

    def transition(self, identity, target, worker=None, result=None, now=None):
        now = time.time() if now is None else now
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.get(identity)
            permitted = {'executing': {'waiting', 'uncertain', 'rejected'},
                         'uncertain': {'waiting', 'succeeded', 'failed'},
                         'waiting': {'succeeded', 'failed', 'uncertain'}}
            if target not in permitted.get(row['state'], set()):
                raise ValueError('invalid operation transition')
            if row['state'] == 'executing' and worker != row['worker']:
                raise ValueError('operation worker does not own its execution lease')
            self.db.execute('UPDATE operations SET state=?, result=? WHERE id=?',
                            (target, json.dumps(result) if result else None, identity))
            self.event(identity, target, now)
            if target in TERMINAL:
                self.db.execute('DELETE FROM locks WHERE scope=? AND operation_id=?', (row['scope'], identity))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        return self.get(identity)


def validate_run(run, repository, workflow, revision):
    if (run.get('repository', {}).get('full_name') != repository
            or run.get('path') != '.github/workflows/'+workflow
            or run.get('head_sha') != revision or run.get('head_branch') != 'main'
            or type(run.get('id')) is not int or run['id'] <= 0
            or type(run.get('run_attempt')) is not int or run['run_attempt'] <= 0):
        raise ValueError('workflow evidence does not match the approved repository, workflow and main revision')


def prepare(journal, grant, digest, repository, run_id, revision, now=None):
    if repository not in grant['repositories'] or not SHA.fullmatch(revision):
        raise ValueError('repository or source revision is outside the operation grant')
    if type(run_id) is not int or run_id <= 0:
        raise ValueError('invalid workflow run identity')
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    workflow = str(run.get('path', '')).removeprefix('.github/workflows/')
    if workflow not in grant['repositories'][repository]['rerun_workflows']:
        raise ValueError('workflow is outside the operation grant')
    validate_run(run, repository, workflow, revision)
    if (api(f'repos/{repository}/commits/main')['sha'] != revision
            or run.get('status') != 'completed' or run.get('conclusion') != 'failure'):
        raise ValueError('rerun requires failed completed work at current main')
    return journal.prepare({'action': 'rerun-failed-jobs', 'repository': repository,
        'workflow': workflow, 'revision': revision, 'run_id': run_id,
        'run_attempt': run['run_attempt'], 'policy_sha256': digest}, now=now)


def execute(journal, grant, digest, identity, now=None):
    row = journal.get(identity)
    intent = row['intent']
    if (intent['policy_sha256'] != digest or intent['action'] != 'rerun-failed-jobs'
            or intent['workflow'] not in grant['repositories'].get(intent['repository'], {}).get('rerun_workflows', [])):
        raise ValueError('current grant does not authorize this exact prepared operation')
    worker = journal.claim(identity, now=now)
    repository, run_id = intent['repository'], intent['run_id']
    try:
        run = api(f'repos/{repository}/actions/runs/{run_id}')
        validate_run(run, repository, intent['workflow'], intent['revision'])
        if (api(f'repos/{repository}/commits/main')['sha'] != intent['revision']
                or run.get('status') != 'completed' or run.get('conclusion') != 'failure'
                or run['run_attempt'] != intent['run_attempt']):
            raise ValueError('operation preconditions changed')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        return journal.transition(identity, 'rejected', worker, {'reason': 'fresh preconditions could not be established'})
    # The durable executing state exists BEFORE the remote side effect. A crash or
    # timeout can never cause a second POST: uncertain work remains locked until read reconciliation.
    try:
        api(f'repos/{repository}/actions/runs/{run_id}/rerun-failed-jobs', 'POST')
    except (ValueError, OSError, subprocess.SubprocessError):
        return journal.transition(identity, 'uncertain', worker, {'reason': 'remote request outcome unknown; observe without retrying'})
    return journal.transition(identity, 'waiting', worker, {'run_id': run_id, 'expected_attempt': intent['run_attempt']+1})


def observe(journal, identity, now=None):
    now = time.time() if now is None else now
    row = journal.get(identity)
    if row['state'] in TERMINAL or row['state'] == 'prepared':
        return row
    if row['state'] == 'executing':
        if now < row['lease_until']:
            return row
        row = journal.transition(identity, 'uncertain', row['worker'],
                                 {'reason': 'worker lease expired; remote outcome must be reconciled'}, now)
    intent = row['intent']
    run = api(f'repos/{intent["repository"]}/actions/runs/{intent["run_id"]}')
    validate_run(run, intent['repository'], intent['workflow'], intent['revision'])
    expected = intent['run_attempt']+1
    if run['run_attempt'] != expected:
        # Original attempt or multiple externally triggered attempts cannot prove this request.
        return row if row['state'] == 'uncertain' else journal.transition(identity, 'uncertain',
            result={'reason': 'attempt identity is not the expected next attempt'}, now=now)
    if run.get('status') != 'completed':
        return row
    result = {'run_id': run['id'], 'run_attempt': run['run_attempt'], 'revision': intent['revision'],
              'conclusion': run.get('conclusion'), 'observed_at': datetime.datetime.fromtimestamp(
                  now, datetime.timezone.utc).isoformat(), 'scope': 'GitHub rerun result; live deployment identity is separate'}
    return journal.transition(identity, 'succeeded' if run.get('conclusion') == 'success' else 'failed', result=result, now=now)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--policy-sha256', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('--repository', required=True)
    plan.add_argument('--run-id', required=True, type=int)
    plan.add_argument('--expected-main', required=True)
    for command in ('execute', 'observe', 'status'):
        commands.add_parser(command).add_argument('--operation-id', required=True)
    commands.add_parser('list')
    args = parser.parse_args()
    grant = policy(args.policy, args.policy_sha256)
    journal = Journal(args.journal)
    try:
        if args.command == 'plan':
            row = prepare(journal, grant, args.policy_sha256, args.repository, args.run_id, args.expected_main)
        elif args.command == 'execute':
            row = execute(journal, grant, args.policy_sha256, args.operation_id)
        elif args.command == 'observe':
            row = observe(journal, args.operation_id)
        elif args.command == 'list':
            row = journal.export()
        else:
            row = journal.get(args.operation_id)
        print(json.dumps(row, indent=2))
        return int(row.get('state') in {'failed', 'rejected', 'uncertain'})
    finally:
        journal.close()


if __name__ == '__main__':
    raise SystemExit(main())
