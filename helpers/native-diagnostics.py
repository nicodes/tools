#!/usr/bin/env python3
"""Read caller-declared native diagnostic locations without executing caller code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib


def declaration(root, config, *, inspect=False):
    if config.get('recipe')!='godot-game':return [],7
    values=config.get('artifacts',{})
    registered=values.get('evidence_paths',[])
    retained=values.get('evidence_retention_days',{})
    if (not isinstance(registered,list) or len(registered)>64 or any(not isinstance(path,str) for path in registered)
            or len(set(registered))!=len(registered) or not isinstance(retained,dict) or len(retained)>64
            or set(retained)-set(registered)):
        raise ValueError('native diagnostics select only explicitly registered evidence locations')
    if any(type(days) is not int or not 1<=days<=90 for days in retained.values()) or len(set(retained.values()))>1:
        raise ValueError('one native diagnostic packet requires one bounded common retention')
    root=Path(root).absolute()
    for name in retained:
        path=Path(name)
        if (not name or len(name)>240 or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?',name)
                or any(part in {'.','..','.git'} for part in path.parts)
                or not ((path.parts[0] in {'.artifacts','tmp'} and len(path.parts)>1)
                    or re.fullmatch('[A-Za-z][A-Za-z0-9_-]*-evidence',path.parts[0]))):
            raise ValueError('native diagnostics require explicit generated output locations')
        target=root/path
        if any(value.is_symlink() for value in [target,*target.parents]):
            raise ValueError('native diagnostic locations may not traverse links')
        if inspect and target.exists():
            if not target.is_dir():raise ValueError('native diagnostic locations must be output directories')
            count=0
            for value in target.rglob('*'):
                count+=1
                if count>20000 or value.is_symlink() or not (value.is_dir() or value.is_file()):
                    raise ValueError('native diagnostic output must be a bounded unlinked regular tree')
    return sorted(retained),next(iter(retained.values()),7)


def read(root, *, inspect=False, expected=None):
    root=Path(root)
    source=root/'cookbook.toml'
    if source.is_symlink() or not source.is_file() or source.stat().st_size>262144:
        raise ValueError('native diagnostic declarations require bounded regular cookbook data')
    body=source.read_bytes()
    if expected is not None and (not re.fullmatch('[a-f0-9]{64}',expected) or hashlib.sha256(body).hexdigest()!=expected):
        raise ValueError('native diagnostic declaration changed during validation')
    return declaration(root,tomllib.loads(body.decode()),inspect=inspect)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--inspect',action='store_true')
    parser.add_argument('--expected-sha256')
    args=parser.parse_args()
    paths,days=read(args.root,inspect=args.inspect,expected=args.expected_sha256)
    if os.environ.get('GITHUB_OUTPUT'):
        identity=hashlib.sha256((args.root/'cookbook.toml').read_bytes()).hexdigest()
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:
            output.write('paths<<COOKBOOK_NATIVE_EVIDENCE_PATHS\n'+'\n'.join(paths)+'\nCOOKBOOK_NATIVE_EVIDENCE_PATHS\nretention-days='+str(days)+'\ndeclaration-sha256='+identity+'\n')
    else:print(json.dumps({'scope':'native-diagnostics','paths':paths,'retention_days':days}))
