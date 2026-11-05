#!/usr/bin/env python3
"""Retain bounded native Godot diagnostics separately from source acceptance."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

TOOLS=Path(__file__).resolve().parents[1]


def report_directory(path, *, fresh=False):
    path=Path(path).absolute()
    if any(value.is_symlink() for value in [path,*path.parents]):
        raise ValueError('native diagnostic directories must not traverse links')
    path.mkdir(mode=0o700,parents=True,exist_ok=not fresh)
    return path


def run_case(command, report, *, name, timeout=180, sentinel=None, forbidden=(), environment=None, cwd=None):
    if not command or type(timeout) not in {int,float} or not 0<timeout<=3600:
        raise ValueError('native Godot diagnostics require a command and its bounded original deadline')
    report=report_directory(report,fresh=True)
    log=report/'output.log'
    command=[sys.executable,str(TOOLS/'helpers/godot-game-runner.py'),'--timeout',str(timeout),
        *(['--sentinel',sentinel] if sentinel is not None else []),
        *[value for item in forbidden for value in ['--forbidden',item]],'--',*map(str,command)]
    started=time.monotonic()
    environment=dict(os.environ if environment is None else environment)
    environment['GODOT_TEST_TIMINGS']=str(report/'timings.jsonl')
    with log.open('wb') as output:
        os.fchmod(output.fileno(),0o600)
        process=subprocess.run(command,env=environment,cwd=cwd,
            stdout=output,stderr=subprocess.STDOUT,check=False,timeout=timeout+15)
    row={'scope':'native-diagnostics','test':name,'exit_code':process.returncode,'log':str(log),
        'seconds':round(time.monotonic()-started,3),
        'source_commit':environment.get('SOURCE_COMMIT') or environment.get('GITHUB_SHA')}
    (report/'summary.json').write_text(json.dumps(row,sort_keys=True)+'\n')
    print(log.read_text(errors='replace'),end='')
    return row


def run_one(command, reports, **options):
    reports=report_directory(reports)
    # mkdtemp reserves a unique name. run_case takes ownership of the still
    # absent directory so it can never overwrite prior diagnostic evidence.
    directory=Path(tempfile.mkdtemp(prefix='case-',dir=reports))
    directory.rmdir()
    return run_case(command,directory,**options)


def run_suite(engine, project, tests, report, jobs, *, timeout=None):
    project=Path(project).absolute()
    if any(path.is_symlink() for path in [project,*project.parents]):
        raise ValueError('native suite projects must not traverse links')
    if type(jobs) is not int or not 1<=jobs<=4:
        raise ValueError('Godot suite jobs must stay between one and four')
    if not isinstance(tests,(list,tuple)) or not tests or any(not isinstance(name,str) for name in tests) or len(set(tests))!=len(tests):
        raise ValueError('Godot suite inventory must be nonempty and unique')
    for name in tests:
        path=Path(name)
        source=project/path
        if (path.is_absolute() or '..' in path.parts or '.git' in path.parts or path.suffix!='.gd'
                or source.is_symlink() or not source.is_file() or any(parent.is_symlink() for parent in source.parents)):
            raise ValueError('Godot suite entries must be real owned native test scripts')
    timeout=float(os.environ.get('GODOT_TEST_TIMEOUT','180')) if timeout is None else timeout
    if type(timeout) not in {int,float} or not 0<timeout<=3600:
        raise ValueError('Godot suite deadlines must stay within the native bound')
    report=report_directory(report,fresh=True)
    started=time.monotonic()
    def run(item):
        index,name=item
        before=time.monotonic()
        command=[str(engine),'--headless','--path',str(project),'--script',str(TOOLS/'helpers/godot-test-entry.gd'),'--','res://'+name]
        row=run_case(command,report/f'{index:03d}-{Path(name).stem}',name=name,timeout=timeout)
        row['started_seconds']=round(before-started,3)
        return row
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results=list(pool.map(run,enumerate(tests,1)))
    passed=all(row['exit_code']==0 for row in results)
    summary={'scope':'native-diagnostics','project':str(project),
        'source_commit':os.environ.get('SOURCE_COMMIT') or os.environ.get('GITHUB_SHA'),
        'jobs':jobs,'inventory':list(tests),'results':results,'seconds':round(time.monotonic()-started,3),
        'status':'passed' if passed else 'failed'}
    (report/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return 0 if passed else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine',required=True)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--jobs',type=int,default=4)
    parser.add_argument('--timeout',type=float,default=180)
    parser.add_argument('tests',nargs='+')
    args=parser.parse_args()
    try:sys.exit(run_suite(args.engine,args.project,args.tests,args.report,args.jobs,timeout=args.timeout))
    except (OSError,ValueError,subprocess.SubprocessError) as error:parser.exit(1,str(error)+'\n')
