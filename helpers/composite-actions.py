#!/usr/bin/env python3
"""Validate composite step fields using the explicitly prepared YAML SDK."""
from pathlib import Path
import subprocess
import sys

FIELDS={'id','if','name','run','shell','working-directory','env','uses','with','continue-on-error'}


def validate(document, path):
    runs=document.get('runs',{})
    if runs.get('using')!='composite':return
    steps=runs.get('steps')
    if not isinstance(steps,list) or not steps:raise ValueError(f'{path}: composite steps must be nonempty')
    for index,step in enumerate(steps,1):
        if not isinstance(step,dict) or set(step)-FIELDS:
            invalid=sorted(set(step)-FIELDS) if isinstance(step,dict) else ['invalid step']
            raise ValueError(f'{path}: composite step {index} has unsupported fields: {invalid}')
        if ('run' in step)==('uses' in step) or ('run' in step and not step.get('shell')):
            raise ValueError(f'{path}: composite step {index} requires one declared action or explicit shell program')


def main():
    import yaml
    root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
    paths=subprocess.check_output(['git','ls-files','-z'],cwd=root,timeout=10).decode().split('\0')
    for name in paths:
        if Path(name).name not in {'action.yml','action.yaml'}:continue
        path=root/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1048576:
            raise ValueError('action manifests must be bounded regular source files')
        document=yaml.safe_load(path.read_text())
        if not isinstance(document,dict):raise ValueError(f'{name}: action must be a mapping')
        validate(document,name)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,subprocess.SubprocessError) as error:raise SystemExit(str(error)) from None
