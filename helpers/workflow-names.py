#!/usr/bin/env python3
"""Keep active workflow filenames and references tied to their display names."""
from pathlib import Path
import re
import sys


def title(value, location):
    if (not isinstance(value, str) or not value or value != value.strip()
            or re.search(r' {2,}|[^\S ]|&', value)):
        raise ValueError(f'{location}: use a nonempty name with ordinary spaces and "and"')
    return value


def filename(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') + '.yml'


def validate(documents):
    names = {}
    for path, document in documents.items():
        if not isinstance(document, dict):
            raise ValueError(f'{path}: workflow must be a mapping')
        name = title(document.get('name'), path)
        expected = filename(name)
        if Path(path).name != expected:
            raise ValueError(f'{path}: filename must match workflow name: {expected}')
        if name in names:
            raise ValueError(f'{path}: duplicate workflow name: {name}')
        names[name] = path
        jobs = document.get('jobs')
        if not isinstance(jobs, dict) or not jobs:
            raise ValueError(f'{path}: workflow jobs must be a nonempty mapping')
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                raise ValueError(f'{path}: job {job_id} must be a mapping')
            title(job.get('name'), f'{path}: job {job_id}')
            if 'steps' in job:
                if not isinstance(job['steps'], list) or not job['steps']:
                    raise ValueError(f'{path}: job {job_id} steps must be a nonempty list')
                for index, step in enumerate(job['steps'], 1):
                    if not isinstance(step, dict):
                        raise ValueError(f'{path}: job {job_id} step {index} must be a mapping')
                    title(step.get('name'), f'{path}: job {job_id} step {index}')
    for path, document in documents.items():
        # The pinned PyYAML SDK follows YAML 1.1 and interprets `on` as True.
        triggers = document.get('on', document.get(True, {}))
        if not isinstance(triggers, dict) or 'workflow_run' not in triggers:
            continue
        callback = triggers.get('workflow_run', {})
        if not isinstance(callback, dict):
            continue
        workflows = callback.get('workflows', [])
        if not isinstance(workflows, list) or not workflows:
            raise ValueError(f'{path}: workflow_run requires explicit workflow names')
        for name in workflows:
            if not isinstance(name, str) or name not in names:
                raise ValueError(f'{path}: workflow_run references an unknown workflow: {name}')


def main():
    import yaml
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve()
    documents = {}
    for path in sorted((root / '.github/workflows').iterdir()):
        if path.suffix not in {'.yml', '.yaml'}:
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1048576:
            raise ValueError('workflows must be bounded regular source files')
        documents[path.relative_to(root).as_posix()] = yaml.safe_load(path.read_text())
    if not documents:
        raise ValueError('no active workflows found')
    validate(documents)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
