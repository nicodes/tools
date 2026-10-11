#!/usr/bin/env python3
"""Generate callers from the same released implementation used by the SDK."""
import argparse
import importlib.util
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('roots',nargs='*',type=Path)
parser.add_argument('--export-sdk',action='store_true',help='regenerate flat SDK engines from this canonical library')
parser.add_argument('--check',action='store_true')
args=parser.parse_args()
if not args.roots and not args.export_sdk: parser.error('supply caller roots or --export-sdk')
if args.export_sdk:
    if args.check: parser.error('--export-sdk writes a reviewed source proposal')
    library=Path(__file__).resolve().parent
    helpers=library.parent/'helpers'
    names=('workflow_contract.py','workflow_operation.py','pins_compat.mjs')
    if any((helpers/name).is_symlink() for name in names): raise SystemExit('linked SDK engine refused')
    for name in names: (helpers/name).write_bytes((library/name).read_bytes())
source=Path(__file__).resolve().parents[1]/'helpers/sync-workflow-policy.py'
spec=importlib.util.spec_from_file_location('workflow_sync',source)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
for root in args.roots:
    module.synchronize(root,args.check)
    print(f'{root}: released workflow policy {"verified" if args.check else "regenerated"}')
