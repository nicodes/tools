#!/usr/bin/env python3
"""Audit historically activated artifacts without claiming current host state."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile

MAX_METADATA = 4 << 20
MAX_EVIDENCE = 16 << 20
MAX_ACTIVATION = 1 << 20
SHA = re.compile(r'[a-f0-9]{40}\Z')
NAME = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')


def helper(name):
    spec = importlib.util.spec_from_file_location('_deployed_' + name, Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


source_config = helper('source_config')
runtime = helper('container_runtime')
registry = helper('oci_registry')
_prior = sys.modules.get('oci_registry')
sys.modules['oci_registry'] = registry
try:
    packets = helper('release_packet')
finally:
    if _prior is None: del sys.modules['oci_registry']
    else: sys.modules['oci_registry'] = _prior


def load_config(file):
    config = source_config.read(file, maximum=262144)
    if set(config) != {'repository','application','components','environment','workflow'}:
        raise ValueError('scan config requires repository, application, components, environment and workflow')
    repository = config['repository']
    if (not isinstance(repository,str) or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',repository)
            or repository.split('/')[1] in {'.','..'}
            or os.environ.get('GITHUB_REPOSITORY',repository) != repository):
        raise ValueError('repository does not match the caller-owned scan declaration')
    if not isinstance(config['application'],str) or not NAME.fullmatch(config['application']):
        raise ValueError('scan requires its source-declared application')
    components=config['components']
    if (not isinstance(components,list) or not 1 <= len(components) <= 32
            or any(not isinstance(value,str) or not NAME.fullmatch(value) for value in components)
            or len(set(components)) != len(components)):
        raise ValueError('scan requires distinct declared image artifact names')
    if (not isinstance(config['workflow'],str)
            or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.yml',config['workflow'])):
        raise ValueError('scan requires its exact source-owned delivery workflow path')
    deployment_environment(config['environment'])
    return config


def deployment_environment(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,99}',value) or value != value.strip():
        raise ValueError('deployment environment must be an explicit bounded name')
    return urllib.parse.quote(value,safe='')


def api(path, environment=None):
    result=subprocess.check_output(['gh','api','--hostname','github.com',path],text=True,timeout=30,env=environment)
    if len(result.encode()) > MAX_METADATA:
        raise ValueError('deployment metadata exceeds its bound')
    return json.loads(result,object_pairs_hook=registry.unique_object)


def deployed_releases(repository, environment):
    selected=[]; encoded=deployment_environment(environment)
    for page in range(1,11):
        deployments=api(f'repos/{repository}/deployments?environment={encoded}&per_page=100&page={page}')
        if not isinstance(deployments,list) or len(deployments)>100:
            raise ValueError('invalid bounded deployment list')
        for deployment in deployments:
            identity=deployment.get('id') if isinstance(deployment,dict) else None
            if type(identity) is not int or identity<=0:
                raise ValueError('invalid deployment identity')
            statuses=api(f'repos/{repository}/deployments/{identity}/statuses?per_page=100')
            if not isinstance(statuses,list) or len(statuses)>100:
                raise ValueError('invalid bounded deployment status list')
            if not statuses: continue
            status=statuses[0]
            state=status.get('state') if isinstance(status,dict) else None
            revision=deployment.get('sha','')
            match=re.fullmatch(r'https://github\.com/'+re.escape(repository)+r'/actions/runs/([1-9][0-9]{0,19})/job/([1-9][0-9]{0,19})',status.get('log_url',''))
            if (state not in {'success','failure','error','pending','queued','in_progress','inactive'}
                    or not isinstance(revision,str) or not SHA.fullmatch(revision) or match is None):
                raise ValueError('deployment has no exact source and workflow job identity')
            record={'revision':revision,'deployment_id':identity,'run_id':int(match[1]),'job_id':int(match[2]),'state':state}
            if not selected: selected.append(record)
            if state=='success':
                if record != selected[0]: selected.append(record)
                return selected
        if len(deployments)<100: break
    raise ValueError('no successful deployment found within the lookup bound')


def download(repository, identity, environment=None):
    with tempfile.TemporaryFile() as stream:
        subprocess.run(['gh','api','--hostname','github.com',f'repos/{repository}/actions/artifacts/{identity}/zip'],
                       stdout=stream,env=environment,check=True,timeout=60)
        if not 0 < stream.tell() <= MAX_EVIDENCE:
            raise ValueError('release evidence archive exceeds its bound')
        stream.seek(0); return stream.read(MAX_EVIDENCE+1)


def _publication_for_deployment(config, deployment, environment=None):
    repository,revision=config['repository'],deployment['revision']
    run=api(f'repos/{repository}/actions/runs/{deployment["run_id"]}',environment)
    job=api(f'repos/{repository}/actions/jobs/{deployment["job_id"]}',environment)
    if (not isinstance(run,dict) or not isinstance(job,dict)
            or run.get('id') != deployment['run_id'] or run.get('head_sha') != revision
            or run.get('head_branch') != 'main' or run.get('event') not in {'push','workflow_dispatch'}
            or run.get('path') != config['workflow'] or not isinstance(run.get('repository'),dict)
            or not isinstance(run.get('head_repository'),dict) or run['repository'].get('full_name') != repository
            or run.get('head_repository',{}).get('full_name') != repository
            or type(run.get('run_attempt')) is not int or run['run_attempt']<1
            or job.get('id') != deployment['job_id'] or job.get('run_id') != run['id']
            or job.get('run_attempt') != run['run_attempt'] or job.get('head_sha') != revision
            or (deployment['state']=='success' and job.get('conclusion') != 'success')):
        raise ValueError('deployment producer differs from its exact main workflow run and attempt')
    artifacts=api(f'repos/{repository}/actions/runs/{run["id"]}/artifacts?per_page=100',environment)
    if (not isinstance(artifacts,dict) or type(artifacts.get('total_count')) is not int
            or not 0<=artifacts['total_count']<100 or not isinstance(artifacts.get('artifacts'),list)
            or artifacts['total_count'] != len(artifacts['artifacts'])):
        raise ValueError('release evidence artifact list is incomplete or oversized')
    wanted=f'host-release-{revision}-{run["id"]}-{run["run_attempt"]}'
    candidates=[row for row in artifacts['artifacts'] if isinstance(row,dict) and row.get('name')==wanted]
    if len(candidates)!=1: raise ValueError('deployment requires one exact release evidence artifact')
    artifact=candidates[0]
    if (type(artifact.get('id')) is not int or artifact['id']<=0 or artifact.get('expired') is not False
            or type(artifact.get('size_in_bytes')) is not int or not 0<artifact['size_in_bytes']<=MAX_EVIDENCE
            or not isinstance(artifact.get('digest'),str) or not registry.DIGEST.fullmatch(artifact['digest'])
            or artifact.get('workflow_run',{}).get('id') != run['id']
            or artifact.get('workflow_run',{}).get('head_sha') != revision):
        raise ValueError('release artifact has no immutable source producer identity')
    raw=download(repository,artifact['id'],environment)
    if len(raw)!=artifact['size_in_bytes'] or registry.digest(raw)!=artifact['digest']:
        raise ValueError('release evidence differs from the authenticated artifact digest')
    documents={}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries=archive.infolist()
        if len(entries)!=6 or {entry.filename for entry in entries}!={'release.json','inventory.json','validation.json','build.json','test.json','publication.json'}:
            raise ValueError('release evidence archive has an unexpected member set')
        for entry in entries:
            if (not 0<entry.file_size<=MAX_METADATA or entry.flag_bits&1 or entry.is_dir()
                    or stat.S_IFMT(entry.external_attr>>16) not in {0,stat.S_IFREG}):
                raise ValueError('release evidence contains linked or oversized metadata')
            body=archive.read(entry)
            documents[entry.filename]=(body,json.loads(body,object_pairs_hook=registry.unique_object))
    release_raw,descriptor=documents['release.json']
    canonical=packets.release(descriptor,application=config['application'])
    if canonical!=release_raw:
        raise ValueError('deployed release descriptor differs from its immutable canonical bytes')
    source=descriptor['source']; release_digest=registry.digest(release_raw)
    if (source['repository']!=repository or source['revision']!=revision
            or str(run.get('repository',{}).get('id'))!=source['repository_id']):
        raise ValueError('deployed descriptor belongs to another source')
    publication=documents['publication.json'][1]
    context={'GITHUB_SERVER_URL':'https://github.com','GITHUB_REPOSITORY':repository,'GITHUB_REPOSITORY_ID':source['repository_id'],
             'GITHUB_SHA':revision,'GITHUB_RUN_ID':str(run['id']),'GITHUB_RUN_ATTEMPT':str(run['run_attempt']),
             'GITHUB_WORKFLOW_REF':repository+'/'+config['workflow']+'@refs/heads/main',
             'GITHUB_WORKFLOW_SHA':revision,'GITHUB_EVENT_NAME':run['event']}
    expected_published={name:row['reference'] for name,row in descriptor['artifacts'].items()}
    if (not isinstance(publication,dict) or publication.get('kind')!='komizo.publication'
            or type(publication.get('version')) is not int or publication['version']!=1
            or publication.get('source')!=source or publication.get('release_digest')!=release_digest
            or publication.get('published')!=expected_published):
        raise ValueError('publication receipt differs from its frozen descriptor')
    validate_context(publication.get('execution_context'),context)
    producer=publication['execution_context']['GITHUB_JOB']
    if producer not in {'publish','deliver'}:
        raise ValueError('release evidence has an unknown protected publication producer')
    provider='nicodes/komizo-actions/.github/workflows/cookbook-'+producer+'.yml@'
    references=run.get('referenced_workflows')
    if (not isinstance(references,list) or not 1<=len(references)<=100
            or not any(isinstance(row,dict) and isinstance(row.get('sha'),str) and SHA.fullmatch(row['sha'])
                       and row.get('path')==provider+row['sha'] for row in references)):
        raise ValueError('release producer did not execute its immutable protected adapter')
    if producer=='deliver' and (not isinstance(job.get('name'),str)
            or job['name'].split(' / ')[-1]!='Deliver exact release'):
        raise ValueError('combined publication must belong to the exact deployed delivery job')
    validate_acceptance(documents,descriptor,publication,context,release_raw)
    commit=api(f'repos/{repository}/git/commits/{revision}',environment)
    if (not isinstance(commit,dict) or commit.get('sha')!=revision
            or commit.get('tree',{}).get('sha')!=documents['validation.json'][1]['source_tree']):
        raise ValueError('deployment acceptance source tree differs from its exact Git commit')
    product_images(config,descriptor)
    return descriptor,run,job,artifacts['artifacts']


def activation_for_deployment(config, deployment, environment=None):
    """Bind accepted publication to an actual ready operation at that time.

    The owner can later stop, recover or replace the instance independently.
    GitHub evidence does not observe the current installed or running state.
    """
    descriptor,run,job,artifacts=_publication_for_deployment(config,deployment,environment)
    if (deployment['state']!='success' or job.get('conclusion')!='success'
            or not isinstance(job.get('name'),str) or job['name'].split(' / ')[-1]!='Deliver exact release'):
        raise ValueError('historical activation requires its successful exact delivery job')
    provider='nicodes/komizo-actions/.github/workflows/cookbook-deliver.yml@'
    if not any(isinstance(row,dict) and isinstance(row.get('sha'),str) and SHA.fullmatch(row['sha'])
            and row.get('path')==provider+row['sha'] for row in run['referenced_workflows']):
        raise ValueError('historical activation did not execute its immutable delivery adapter')
    wanted=f'activation-{deployment["revision"]}-{run["id"]}-{run["run_attempt"]}'
    candidates=[row for row in artifacts if isinstance(row,dict) and row.get('name')==wanted]
    if len(candidates)!=1:raise ValueError('historical activation requires one exact operation artifact')
    artifact=candidates[0]
    if (type(artifact.get('id')) is not int or artifact['id']<=0 or artifact.get('expired') is not False
            or type(artifact.get('size_in_bytes')) is not int or not 0<artifact['size_in_bytes']<=MAX_ACTIVATION
            or not isinstance(artifact.get('digest'),str) or not registry.DIGEST.fullmatch(artifact['digest'])
            or artifact.get('workflow_run',{}).get('id')!=run['id']
            or artifact.get('workflow_run',{}).get('head_sha')!=deployment['revision']):
        raise ValueError('activation artifact lacks its immutable source producer identity')
    raw=download(config['repository'],artifact['id'],environment)
    if len(raw)!=artifact['size_in_bytes'] or registry.digest(raw)!=artifact['digest']:
        raise ValueError('activation artifact differs from its authenticated digest')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries=archive.infolist()
        if len(entries)!=1:raise ValueError('activation artifact requires only its operation receipt')
        entry=entries[0]
        if (entry.filename!='komizo-operation.json' or not 0<entry.file_size<=65536
                or entry.flag_bits&1 or entry.is_dir() or stat.S_IFMT(entry.external_attr>>16) not in {0,stat.S_IFREG}):
            raise ValueError('activation operation receipt is linked, unexpected or oversized')
        body=archive.read(entry);operation=json.loads(body,object_pairs_hook=registry.unique_object)
    required={'kind','version','operation_id','application','instance','release_digest','phase','ok','reconciliation_required'}
    if (not isinstance(operation,dict) or not required<=set(operation)<=required|{'previous_release_digest'}
            or operation.get('kind')!='komizo.operation' or type(operation.get('version')) is not int or operation['version']!=1
            or not isinstance(operation.get('operation_id'),str) or not re.fullmatch('[a-f0-9]{32}',operation['operation_id'])
            or operation.get('application')!=config['application'] or operation.get('instance')!='production'
            or operation.get('release_digest')!=registry.digest(registry.canonical(descriptor))
            or operation.get('phase')!='ready' or operation.get('ok') is not True or operation.get('reconciliation_required') is not False
            or ('previous_release_digest' in operation and operation['previous_release_digest'] is not None
                and (not isinstance(operation['previous_release_digest'],str) or not registry.DIGEST.fullmatch(operation['previous_release_digest'])))
            or body!=registry.canonical(operation)+b'\n'):
        raise ValueError('publication is not bound to an exact successful ready operation')
    return {'scope':'activation_at_time','current_state':'unobserved','descriptor':descriptor,'operation':operation,
        'proof':{'deployment_id':deployment['deployment_id'],'run_id':run['id'],'run_attempt':run['run_attempt'],'job_id':job['id'],
                 'artifact_id':artifact['id'],'artifact_digest':artifact['digest']}}


def validate_context(value, expected, *, repository_id=True):
    if (not isinstance(value,dict) or any(value.get(key)!=wanted for key,wanted in expected.items()
            if repository_id or key!='GITHUB_REPOSITORY_ID')
            or not isinstance(value.get('GITHUB_JOB'),str)
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,127}',value['GITHUB_JOB'])
            or ('GITHUB_REPOSITORY_ID' in value and value['GITHUB_REPOSITORY_ID']!=expected['GITHUB_REPOSITORY_ID'])):
        raise ValueError('publication acceptance belongs to another workflow execution')


def validate_acceptance(documents, descriptor, publication, context, release_raw):
    validation=documents['validation.json'][1]; build=documents['build.json'][1]; test=documents['test.json'][1]
    inventory_raw,inventory=documents['inventory.json']
    if (publication.get('inventory_sha256')!=hashlib.sha256(inventory_raw).hexdigest()
            or publication.get('validation_sha256')!=hashlib.sha256(registry.canonical(validation)).hexdigest()
            or not all(isinstance(value,dict) for value in [validation,build,test,inventory])):
        raise ValueError('publication acceptance metadata hashes differ')
    accepted_context=validation.get('execution_context')
    validate_context(accepted_context,context,repository_id=False)
    groups=validation.get('test_groups')
    source=descriptor['source']['revision']
    tree=validation.get('source_tree')
    if (validation.get('source_commit')!=source or not isinstance(tree,str) or not SHA.fullmatch(tree)
            or validation.get('repository')!=descriptor['source']['repository']
            or validation.get('status')!='passed' or validation.get('tracked_source_clean') is not True
            or validation.get('build_verified') is not True or validation.get('tests_verified') is not True
            or not isinstance(groups,list) or not 1<=len(groups)<=32
            or any(not isinstance(group,str) or not NAME.fullmatch(group) for group in groups)
            or len(set(groups))!=len(groups)):
        raise ValueError('deployment requires complete same-source build and test acceptance')
    identity=build.get('identity'); provenance=build.get('provenance',{})
    configurations=provenance.get('configuration_sha256') if isinstance(provenance,dict) else None
    if (not isinstance(configurations,dict) or not configurations
            or any(not isinstance(key,str) or not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value) for key,value in configurations.items())
            or configurations.get('cookbook.toml')!=validation.get('configuration_sha256')
            or not isinstance(configurations.get('cookbook.toml'),str)):
        raise ValueError('deployment source configuration hashes differ from complete acceptance')
    if (not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{64}',identity)
            or not isinstance(provenance,dict) or provenance.get('input_identity')!=identity
            or test.get('input_identity')!=identity or test.get('tested_commit')!=source
            or test.get('execution_context')!=accepted_context or provenance.get('execution_context')!=accepted_context
            or test.get('configuration_sha256')!=provenance.get('configuration_sha256')
            or not isinstance(test.get('test_receipts'),dict) or set(test['test_receipts'])!=set(groups)
            or any(value!={'input_identity':identity,'source_commit':source} for value in test['test_receipts'].values())):
        raise ValueError('deployment test receipts did not accept the same build inputs')
    for record in [provenance,test]:
        if record.get('source_commit')!=source or record.get('source_tree')!=tree or record.get('tracked_source_clean') is not True:
            raise ValueError('deployment build/test source differs from complete acceptance')
    accepted=validation.get('artifacts')
    if (not isinstance(accepted,dict) or build.get('artifacts')!=accepted
            or not isinstance(inventory,dict) or set(inventory)!={'version','files'}
            or type(inventory['version']) is not int or inventory['version']!=1
            or not isinstance(inventory['files'],dict) or not 1<=len(inventory['files'])<=packets.MAX_FILES):
        raise ValueError('deployment has no closed accepted artifact inventory')
    required={'release.json'}
    for name,artifact in descriptor['artifacts'].items():
        required.add(('images/'+name+'.oci.tar') if artifact['kind']=='image' else ('static/'+name+'.tar.gz'))
    files=inventory['files']
    if {name for name in files if not name.startswith('metadata/')}!=required:
        raise ValueError('deployment inventory differs from its actual workload artifacts')
    for name,record in files.items():
        kind=packets.member_type(name)
        if (not isinstance(record,dict) or set(record)!={'sha256','size','type'} or record['type']!=kind
                or not isinstance(record['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',record['sha256'])
                or type(record['size']) is not int or not 0<=record['size']<=packets.MAX_FILE
                or accepted.get('.artifacts/release/'+name)!=record['sha256']):
            raise ValueError('deployed artifact differs from its complete acceptance')
    if (files['release.json']['sha256']!=hashlib.sha256(release_raw).hexdigest() or files['release.json']['size']!=len(release_raw)
            or accepted.get('.artifacts/release/inventory.json')!=hashlib.sha256(inventory_raw).hexdigest()):
        raise ValueError('immutable release descriptor or inventory differs from complete acceptance')


def product_images(config, descriptor):
    if not isinstance(descriptor,dict) or descriptor.get('application')!=config['application']:
        raise ValueError('image selection requires its accepted release descriptor')
    actual={name for name,artifact in descriptor.get('artifacts',{}).items() if isinstance(artifact,dict) and artifact.get('kind')=='image'}
    if set(config['components'])!=actual:
        raise ValueError('image scan selection differs from the complete accepted image inventory')
    result=[]
    for component in config['components']:
        artifact=descriptor.get('artifacts',{}).get(component,{})
        if artifact.get('kind')!='image': raise ValueError('declared deployed image artifact is missing')
        server,repository,identity=registry.reference(artifact.get('reference'))
        if server!='ghcr.io' or not repository.startswith(config['repository'].split('/')[0].lower()+'/') or artifact.get('digest')!=identity:
            raise ValueError('deployed image is outside its immutable source registry scope')
        result.append(artifact['reference'])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('deploy/scan.toml'))
    args=parser.parse_args(); config=load_config(args.config)
    releases=deployed_releases(config['repository'],config['environment']); scanner=helper('scan-image')
    results,failures=[],[]
    for deployed in releases:
        activation=activation_for_deployment(config,deployed);descriptor=activation['descriptor']
        for image in product_images(config,descriptor):
            try:
                runtime.run(['pull',image],check=True,timeout=300); results.append(scanner.scan(image))
            except (ValueError,subprocess.SubprocessError) as error: failures.append(f'{image}: {error}')
    print(json.dumps({'scope':'activation_at_time','current_state':'unobserved','deployments':releases,'images':results,'failures':failures},indent=2))
    if failures: raise SystemExit(1)
