#!/usr/bin/env python3
"""Read caller-declared native diagnostic locations without executing caller code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib


def declaration(root, config, *, inspect=False, status=None):
    if config.get('recipe')!='godot-game':return []
    values=config.get('artifacts',{})
    registered=values.get('evidence_paths',[])
    retained=values.get('evidence_retention_days',{})
    if (not isinstance(registered,list) or len(registered)>64 or any(not isinstance(path,str) for path in registered)
            or len(set(registered))!=len(registered) or not isinstance(retained,dict) or len(retained)>64
            or set(retained)-set(registered)):
        raise ValueError('native outputs select only explicitly registered evidence locations')
    if status not in {None,'success','failure','cancelled'}:raise ValueError('native output retention requires the actual job status')
    root=Path(root).absolute()
    policies={}
    for name,policy in retained.items():
        if type(policy) is int:policy={'days':policy,'when':'always'}
        if (not isinstance(policy,dict) or set(policy)-{'days','when'} or type(policy.get('days')) is not int
                or not 0<=policy['days']<=90 or policy.get('when','always') not in {'always','success','failure'}):
            raise ValueError('native output retention requires bounded days and success/failure/always admission')
        path=Path(name)
        if (not name or len(name)>240 or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?',name)
                or any(part in {'.','..','.git'} for part in path.parts)
                or not ((path.parts[0] in {'.artifacts','tmp'} and len(path.parts)>1)
                    or re.fullmatch('[A-Za-z][A-Za-z0-9_-]*-evidence',path.parts[0]))):
            raise ValueError('native outputs require explicit generated output locations')
        target=root/path
        if any(value.is_symlink() for value in [target,*target.parents]):
            raise ValueError('native output locations may not traverse links')
        if any(path==other or path.is_relative_to(other) or other.is_relative_to(path) for other in policies):
            raise ValueError('native output locations must be distinct nonoverlapping paths')
        policies[path]=policy
        if inspect and target.exists():
            count,total=0,0
            entries=target.rglob('*') if target.is_dir() else [target]
            for value in entries:
                count+=1
                if count>20000 or value.is_symlink() or not (value.is_dir() or value.is_file()):
                    raise ValueError('native output must be a bounded unlinked regular file or tree')
                if value.is_file():
                    size=value.stat().st_size;total+=size
                    if size>2<<30 or total>16<<30:raise ValueError('native output exceeds the retained evidence size bound')
    days={value['days'] for value in policies.values()}
    if len(days)>3:raise ValueError('native output retention supports at most three native retention groups')
    groups=[]
    for retention in sorted(days):
        paths=sorted(path.as_posix() for path,policy in policies.items() if policy['days']==retention
            and (status is None or policy.get('when','always') in {'always',status}))
        if paths:groups.append({'days':retention,'paths':paths})
    return groups


def read(root, *, inspect=False, expected=None, status=None):
    root=Path(root)
    source=root/'cookbook.toml'
    if source.is_symlink() or not source.is_file() or source.stat().st_size>262144:
        raise ValueError('native diagnostic declarations require bounded regular cookbook data')
    body=source.read_bytes()
    if expected is not None and (not re.fullmatch('[a-f0-9]{64}',expected) or hashlib.sha256(body).hexdigest()!=expected):
        raise ValueError('native diagnostic declaration changed during validation')
    return declaration(root,tomllib.loads(body.decode()),inspect=inspect,status=status)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--inspect',action='store_true')
    parser.add_argument('--expected-sha256')
    parser.add_argument('--status',choices=['success','failure','cancelled'])
    args=parser.parse_args()
    groups=read(args.root,inspect=args.inspect,expected=args.expected_sha256,status=args.status)
    if os.environ.get('GITHUB_OUTPUT'):
        identity=hashlib.sha256((args.root/'cookbook.toml').read_bytes()).hexdigest()
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:
            def paths(name,values):output.write(name+'<<COOKBOOK_NATIVE_EVIDENCE_PATHS\n'+'\n'.join(values)+'\nCOOKBOOK_NATIVE_EVIDENCE_PATHS\n')
            paths('paths',[path for group in groups for path in group['paths']])
            for index in range(1,4):
                group=groups[index-1] if index<=len(groups) else {'days':0,'paths':[]}
                paths('paths-'+str(index),group['paths'])
                output.write('retention-days-'+str(index)+'='+str(group['days'])+'\n')
            output.write('declaration-sha256='+identity+'\n')
    else:print(json.dumps({'scope':'native-owned-outputs','groups':groups}))
