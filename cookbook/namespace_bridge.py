"""Temporary fixed-Cookbook controller bridge; legacy execution stays in Tools."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

# Private preparation placeholder. Publication requires the actual independently
# admitted C0 main and matching literal reusable-workflow/checkout references.
COOKBOOK_TEMPLATE = 'd68b54a178d84cd206a68af3e947a6d8c223286f'
ROOT = Path(__file__).resolve().parents[1]
MAXIMUM = 4 << 20
WRAPPER = '''import json, pathlib, runpy, sys
entry=pathlib.Path(sys.argv[1])
raw=sys.stdin.buffer.read(4194305)
if len(raw)>4194304: raise ValueError("controller request exceeds its finite bound")
request=json.loads(raw)
if set(request)!={"operation","arguments"} or request["operation"] not in {"snapshot","workflow_acceptance","finish"}:
    raise ValueError("select one fixed Cookbook controller operation")
sys.argv=[str(entry)]
sys.path.insert(0,str(entry.parent))
module=runpy.run_path(str(entry),run_name="cookbook_namespace_controller")
result=module[request["operation"]](*request["arguments"])
print(json.dumps(result))
'''


def source_helper():
    # Tprep supplies this generic stdlib primitive. No product/module discovery.
    path = ROOT / 'helpers/sdk_identity.py'
    spec = importlib.util.spec_from_file_location('tools_namespace_source', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declared(source):
    """Recognize partial modern authority without interpreting or executing tasks."""
    if not isinstance(source, str) or len(source.encode()) > 262144 or '\0' in source:
        raise ValueError('candidate Mise authority requires bounded source data')
    data = tomllib.loads(source.replace('\r\n', '\n').replace('\r', '\n'))
    return bool(set(data) - {'tools', 'env'})


def legacy_source(source):
    if declared(source) or not isinstance(tomllib.loads(source.replace('\r\n', '\n').replace('\r', '\n')).get('tools'), dict):
        raise ValueError('legacy native execution requires only its original Mise SDK authority')
    return source


def protocol(record):
    if not isinstance(record, dict):
        raise ValueError('controller evidence requires its source entrypoint contract')
    contract = record.get('entrypoint_contract', 'make-v0')
    if contract == 'make-v0':
        if (record.get('cookbook_repository', 'nicodes/tools') != 'nicodes/tools'
                or any(key in record for key in ['tools_repository', 'tools_commit'])):
            raise ValueError('legacy controller evidence cannot alias a Cookbook source')
    elif contract == 'mise-v1':
        if (record.get('cookbook_repository') != 'nicodes/cookbook'
                or record.get('tools_repository') != 'nicodes/tools'
                or any(not isinstance(record.get(key), str) or not re.fullmatch('[a-f0-9]{40}', record[key])
                       for key in ['cookbook_commit', 'tools_commit'])):
            raise ValueError('Mise controller evidence requires separate immutable Cookbook and Tools identities')
    else:
        raise ValueError('controller evidence has an unknown or partial entrypoint contract')
    return contract


def prepared():
    if not re.fullmatch('[a-f0-9]{40}', COOKBOOK_TEMPLATE) or COOKBOOK_TEMPLATE == '0' * 40:
        raise ValueError('namespace bridge requires the actually admitted fixed Cookbook template')
    source = source_helper()
    cookbook = source.no_links(os.environ.get('COOKBOOK_ROOT') or Path.cwd() / '.artifacts/recipe-sdks/_cookbook')
    source.verify(cookbook, COOKBOOK_TEMPLATE, repository='nicodes/cookbook')
    dependency = source.identity(source.read_source(cookbook))
    tools = source.no_links(os.environ.get('TOOLS_ROOT') or Path.cwd() / '.artifacts/recipe-sdks/_tools')
    source.verify(tools, dependency['revision'], repository='nicodes/tools')
    if cookbook.resolve() == tools.resolve():
        raise ValueError('namespace bridge requires distinct Cookbook and Tools source roots')
    for key, expected in [('COOKBOOK_REF', COOKBOOK_TEMPLATE), ('TOOLS_REF', dependency['revision'])]:
        if os.environ.get(key, expected) != expected:
            raise ValueError('prepared namespace source differs from its immutable dependency binding')
    return cookbook, tools, dependency['revision']


def invoke(operation, *arguments):
    if operation not in {'snapshot', 'workflow_acceptance', 'finish'}:
        raise ValueError('select one fixed Cookbook controller operation')
    cookbook, tools, revision = prepared()
    raw = json.dumps({'operation': operation, 'arguments': arguments}).encode()
    if len(raw) > MAXIMUM:
        raise ValueError('controller request exceeds its finite bound')
    environment = {**os.environ, 'COOKBOOK_ROOT': str(cookbook), 'COOKBOOK_REF': COOKBOOK_TEMPLATE,
        'TOOLS_ROOT': str(tools), 'TOOLS_REF': revision, 'PYTHONDONTWRITEBYTECODE': '1',
        'MISE_AUTO_INSTALL': 'false', 'MISE_EXEC_AUTO_INSTALL': 'false', 'MISE_TASK_RUN_AUTO_INSTALL': 'false'}
    result = subprocess.run([sys.executable, '-I', '-B', '-c', WRAPPER,
        str(cookbook / 'cookbook/request_merge.py')], input=raw, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if result.returncode or len(result.stdout) > MAXIMUM:
        raise ValueError('checked Cookbook controller refused the namespace request: '
                         + result.stderr.decode(errors='replace')[-2000:])
    return json.loads(result.stdout)
