"""Deployed scans resolve authenticated immutable descriptors, never source tags."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec=importlib.util.spec_from_file_location('scan_deployed',Path(__file__).parents[1]/'helpers/scan-deployed.py')
scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(scanner)


class DeployedRelease(unittest.TestCase):
    def setUp(self):
        self.source='a'*40
        self.config={'repository':'example/widget','application':'widget','components':['worker'],
                     'environment':'production','workflow':'.github/workflows/cd.yml'}
        self.deployment={'revision':self.source,'deployment_id':3,'run_id':42,'job_id':101,'state':'success'}
        self.run={'id':42,'head_sha':self.source,'head_branch':'main','event':'push','path':self.config['workflow'],
                  'referenced_workflows':[{'path':'nicodes/komizo-actions/.github/workflows/cookbook-'+name+'.yml@'+'c'*40,'sha':'c'*40} for name in ['publish','deliver']],
                  'repository':{'id':123,'full_name':'example/widget'},'head_repository':{'full_name':'example/widget'},'run_attempt':2}
        self.job={'id':101,'run_id':42,'run_attempt':2,'head_sha':self.source,'conclusion':'success','name':'deploy / Deliver exact release'}
        self.descriptor={'kind':'komizo.release','version':1,'application':'widget',
            'source':{'repository':'example/widget','repository_id':'123','revision':self.source},
            'artifacts':{'worker':{'kind':'image','reference':'ghcr.io/example/widget-worker@sha256:'+'e'*64,
                                 'digest':'sha256:'+'e'*64,'config_digest':'sha256:'+'b'*64,'platform':'linux/amd64'}},
            'workload':{'services':{'worker':{'artifact':'worker'}},'jobs':{},'assets':{}}}
        self.context={'GITHUB_SERVER_URL':'https://github.com','GITHUB_EVENT_NAME':'push','GITHUB_JOB':'publish','GITHUB_REPOSITORY':'example/widget','GITHUB_REPOSITORY_ID':'123','GITHUB_SHA':self.source,
            'GITHUB_RUN_ID':'42','GITHUB_RUN_ATTEMPT':'2','GITHUB_WORKFLOW_REF':'example/widget/.github/workflows/cd.yml@refs/heads/main',
            'GITHUB_WORKFLOW_SHA':self.source}
        self.archive()
        self.activation()

    def activation(self, *, change=None, extra=None):
        self.operation={'kind':'komizo.operation','version':1,'operation_id':'f'*32,'application':'widget','instance':'production',
            'release_digest':scanner.registry.digest(scanner.registry.canonical(self.descriptor)),
            'phase':'ready','ok':True,'reconciliation_required':False}
        if change:self.operation.update(change)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('komizo-operation.json',scanner.registry.canonical(self.operation)+b'\n')
            for name,body in (extra or {}).items():archive.writestr(name,body)
        self.activation_raw=output.getvalue()
        self.activation_artifact={'id':56,'expired':False,'name':'activation-'+self.source+'-42-2',
            'size_in_bytes':len(self.activation_raw),'digest':scanner.registry.digest(self.activation_raw),
            'workflow_run':{'id':42,'head_sha':self.source}}

    def archive(self, *, change=None, extra=None):
        release=scanner.registry.canonical(self.descriptor); digest=scanner.registry.digest(release)
        source_context={**self.context,'GITHUB_JOB':'validate'}
        inventory={'version':1,'files':{'release.json':{'type':'release','sha256':hashlib.sha256(release).hexdigest(),'size':len(release)}}}
        for name,artifact in self.descriptor['artifacts'].items():
            is_image=artifact['kind']=='image';path=('images/'+name+'.oci.tar') if is_image else ('static/'+name+'.tar.gz')
            inventory['files'][path]={'type':'image' if is_image else 'static',
                'sha256':'c'*64 if is_image else artifact['digest'][7:],'size':10240 if is_image else artifact['size_bytes']}
        inventory_raw=scanner.registry.canonical(inventory)
        artifacts={'.artifacts/release/'+name:record['sha256'] for name,record in inventory['files'].items()}
        artifacts['.artifacts/release/inventory.json']=hashlib.sha256(inventory_raw).hexdigest()
        validation={'version':1,'repository':'example/widget','source_commit':self.source,'source_tree':'d'*40,
                    'status':'passed','tracked_source_clean':True,'build_verified':True,'tests_verified':True,
                    'configuration_sha256':'a'*64,'test_groups':['unit','integration'],'execution_context':source_context,'artifacts':artifacts}
        provenance={'source_commit':self.source,'source_tree':'d'*40,'tracked_source_clean':True,'execution_context':source_context,
                    'input_identity':'f'*64,'configuration_sha256':{'cookbook.toml':'a'*64}}
        build={'identity':'f'*64,'artifacts':artifacts,'provenance':provenance}
        test={**provenance,'tested_commit':self.source,'test_receipts':{name:{'source_commit':self.source,'input_identity':'f'*64} for name in ['unit','integration']}}
        publication={'kind':'komizo.publication','version':1,'source':self.descriptor['source'],'release_digest':digest,
                     'published':{name:artifact['reference'] for name,artifact in self.descriptor['artifacts'].items()},'execution_context':self.context,
                     'inventory_sha256':hashlib.sha256(inventory_raw).hexdigest(),
                     'validation_sha256':hashlib.sha256(scanner.registry.canonical(validation)).hexdigest()}
        documents={'release.json':release,'inventory.json':inventory_raw,'validation.json':scanner.registry.canonical(validation),
                   'build.json':scanner.registry.canonical(build),'test.json':scanner.registry.canonical(test),
                   'publication.json':scanner.registry.canonical(publication)}
        if change: change(documents)
        if extra: documents.update(extra)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as packet:
            for name,body in documents.items(): packet.writestr(name,body)
        self.raw=output.getvalue()
        self.artifact={'id':55,'expired':False,'name':'host-release-'+self.source+'-42-2','size_in_bytes':len(self.raw),
                       'digest':scanner.registry.digest(self.raw),'workflow_run':{'id':42,'head_sha':self.source}}

    def backend(self,path,environment=None):
        if '/git/commits/' in path: return {'sha':self.source,'tree':{'sha':'d'*40}}
        if path.endswith('/actions/runs/42'): return self.run
        if path.endswith('/actions/jobs/101'): return self.job
        if path.endswith('/actions/runs/42/artifacts?per_page=100'): return {'total_count':2,'artifacts':[self.artifact,self.activation_artifact]}
        raise AssertionError(path)

    def resolve(self):
        with patch.object(scanner,'api',side_effect=self.backend),patch.object(scanner,'download',side_effect=lambda repo,identity,env:self.raw if identity==55 else self.activation_raw):
            return scanner.activation_for_deployment(self.config,self.deployment)

    def status(self,state,run=42,job=101):
        return {'state':state,'log_url':f'https://github.com/example/widget/actions/runs/{run}/job/{job}'}

    def test_failed_latest_deployment_keeps_attempt_and_prior_success_with_exact_jobs(self):
        replies=[[{'id':3,'sha':self.source},{'id':2,'sha':'b'*40}],
                 [self.status('failure'),self.status('success')],[self.status('success',40,90)]]
        with patch.object(scanner,'api',side_effect=replies):
            values=scanner.deployed_releases('example/widget','production')
        self.assertEqual([row['revision'] for row in values],[self.source,'b'*40])
        self.assertEqual([(row['run_id'],row['job_id']) for row in values],[(42,101),(40,90)])

    def test_missing_success_invalid_revision_foreign_job_and_protocol_refuse(self):
        for replies in [[[]],[[{'id':1,'sha':'main'}],[self.status('success')]],[[{'id':False}]],
                        [[{'id':1}],{}],[[{'id':1,'sha':self.source}],[{'state':'success','log_url':'https://foreign.test/run/42'}]]]:
            with patch.object(scanner,'api',side_effect=replies),self.assertRaises(ValueError): scanner.deployed_releases('example/widget','production')

    def test_components_select_only_accepted_manifest_digest_references(self):
        self.assertEqual(scanner.product_images(self.config,self.descriptor),[self.descriptor['artifacts']['worker']['reference']])
        with self.assertRaises(ValueError): scanner.product_images(self.config,self.source)
        for change in [{'kind':'archive'},{'reference':'ghcr.io/foreign/worker@sha256:'+'e'*64},{'reference':'ghcr.io/example/worker:'+self.source}]:
            descriptor=copy.deepcopy(self.descriptor);descriptor['artifacts']['worker'].update(change)
            with self.assertRaises(ValueError): scanner.product_images(self.config,descriptor)

    def test_source_declaration_checks_repository_artifacts_and_workflow_before_api(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'scan.toml';scanner.source_config.write(file,self.config)
            with patch.dict(os.environ,{'GITHUB_REPOSITORY':'example/widget'}): self.assertEqual(scanner.load_config(file),self.config)
            for change in [{'repository':'a/b/c'},{'components':['worker','worker']},{'components':['../escape']},
                           {'workflow':'../cd.yml'},{'application':'bad;exit'},{'image_base':'ghcr.io/example/widget'}]:
                scanner.source_config.write(file,{**self.config,**change})
                with self.assertRaises(ValueError): scanner.load_config(file)
            scanner.source_config.write(file,self.config)
            with patch.dict(os.environ,{'GITHUB_REPOSITORY':'example/foreign'}),self.assertRaises(ValueError): scanner.load_config(file)

    def test_environment_is_encoded_and_invalid_scope_precedes_api(self):
        with patch.object(scanner,'api',return_value=[]) as api:
            with self.assertRaises(ValueError): scanner.deployed_releases('example/widget','Preview & other')
            api.assert_not_called()
        with patch.object(scanner,'api',return_value=[]) as api:
            with self.assertRaises(ValueError): scanner.deployed_releases('example/widget','Preview Build')
            self.assertIn('environment=Preview%20Build',api.call_args.args[0])

    def test_authenticated_closed_release_evidence_preserves_immutable_refs(self):
        accepted=self.resolve()
        self.assertEqual(accepted['descriptor'],self.descriptor)
        self.assertEqual(accepted['operation'],self.operation)
        self.assertEqual(accepted['scope'],'activation_at_time');self.assertEqual(accepted['current_state'],'unobserved')
        self.assertEqual(accepted['proof']['artifact_digest'],self.activation_artifact['digest'])
        self.deployment['state']='failure';self.job['conclusion']='failure'
        with self.assertRaises(ValueError):self.resolve()

    def test_staged_failed_reconciliation_foreign_or_noncanonical_receipts_never_claim_activation(self):
        for change in [{'phase':'prepared_stopped'},{'phase':'failed','ok':False},{'reconciliation_required':True},
                {'application':'foreign'},{'instance':'pr-1'},{'release_digest':'sha256:'+'0'*64},
                {'operation_id':'main'},{'ok':1},{'previous_release_digest':'wrong'},{'extra':'untrusted'}]:
            self.activation(change=change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.resolve()
        self.activation(extra={'backup/snapshot.cms':b'not metadata'})
        with self.assertRaises(ValueError):self.resolve()

    def test_activation_artifact_is_exact_bounded_immutable_and_from_the_successful_effect_job(self):
        for field,value in [('expired',True),('name','activation-'+self.source+'-42-1'),('digest','sha256:'+'0'*64),
                ('size_in_bytes',scanner.MAX_ACTIVATION+1),('workflow_run',{'id':41,'head_sha':self.source})]:
            self.activation();self.activation_artifact[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.resolve()
        self.activation();self.job['name']='Build release'
        with self.assertRaises(ValueError):self.resolve()
        self.job['name']='deploy / Deliver exact release';self.run['referenced_workflows']=self.run['referenced_workflows'][:1]
        with self.assertRaises(ValueError):self.resolve()

    def test_complete_native_inventory_has_no_implicit_gate_or_omitted_images(self):
        self.descriptor['artifacts']['postgres']={**self.descriptor['artifacts']['worker'],'reference':'ghcr.io/example/widget-postgres@sha256:'+'e'*64}
        with self.assertRaisesRegex(ValueError,'complete'):scanner.product_images(self.config,self.descriptor)
        self.config['components'].append('postgres')
        self.assertEqual(len(scanner.product_images(self.config,self.descriptor)),2)

    def test_native_static_artifacts_remain_in_the_complete_publication_acceptance_closure(self):
        blob='sha256:'+'7'*64;size=17
        manifest=scanner.registry.digest(scanner.registry.artifact_manifest(blob,size))
        self.descriptor['artifacts']['web']={'kind':'archive','reference':'ghcr.io/example/widget-static@'+manifest,
            'digest':blob,'size_bytes':size,'media_type':scanner.registry.STATIC_LAYER}
        self.archive();self.activation()
        self.assertEqual(self.resolve()['descriptor']['artifacts']['web']['digest'],blob)
        self.assertEqual(len(scanner.product_images(self.config,self.descriptor)),1)
        def omit(documents):
            value=json.loads(documents['inventory.json']);del value['files']['static/web.tar.gz']
            documents['inventory.json']=scanner.registry.canonical(value)
        self.archive(change=omit)
        with self.assertRaises(ValueError):self.resolve()

    def test_operation_canonical_bytes_duplicates_and_linked_members_are_refused(self):
        for body in [scanner.registry.canonical(self.operation),scanner.registry.canonical(self.operation)+b'\n\n',
                b'{"kind":"komizo.operation","kind":"other"}\n']:
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:archive.writestr('komizo-operation.json',body)
            self.activation_raw=output.getvalue();self.activation_artifact.update(size_in_bytes=len(self.activation_raw),digest=scanner.registry.digest(self.activation_raw))
            with self.assertRaises(ValueError):self.resolve()
        output=io.BytesIO();entry=zipfile.ZipInfo('komizo-operation.json');entry.external_attr=(stat.S_IFLNK|0o777)<<16
        with zipfile.ZipFile(output,'w') as archive:archive.writestr(entry,scanner.registry.canonical(self.operation)+b'\n')
        self.activation_raw=output.getvalue();self.activation_artifact.update(size_in_bytes=len(self.activation_raw),digest=scanner.registry.digest(self.activation_raw))
        with self.assertRaises(ValueError):self.resolve()

    def test_wrong_source_job_workflow_attempt_or_stale_artifact_refuses(self):
        for field,value in [('head_sha','b'*40),('head_branch','feature'),('event','pull_request'),('path','.github/workflows/foreign.yml'),('run_attempt',3)]:
            original=copy.deepcopy(self.run);self.run[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): self.resolve()
            self.run=original
        for field,value in [('run_attempt',1),('run_id',40),('head_sha','b'*40),('conclusion','failure')]:
            original=self.job.copy();self.job[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): self.resolve()
            self.job=original
        for field,value in [('expired',True),('name','host-release-'+self.source),('digest','sha256:'+'0'*64)]:
            original=self.artifact.copy();self.artifact[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError): self.resolve()
            self.artifact=original

    def test_changed_bytes_foreign_members_descriptor_or_publication_refuses(self):
        self.raw+=b'changed'
        with self.assertRaises(ValueError): self.resolve()
        for name in ['../escape','foreign.json']:
            self.archive(extra={name:b'foreign'})
            with self.assertRaises(ValueError): self.resolve()
        for document,field,value in [('publication.json','execution_context',{}),('publication.json','published',{}),
                                     ('test.json','test_receipts',{}),('test.json','source_commit','0'*40)]:
            def change(documents):
                row=json.loads(documents[document]);row[field]=value;documents[document]=scanner.registry.canonical(row)
            self.archive(change=change)
            with self.subTest(document=document,field=field),self.assertRaises(ValueError): self.resolve()
        self.archive(change=lambda rows:rows.__setitem__('release.json',rows['release.json']+b'\n'))
        with self.assertRaisesRegex(ValueError,'canonical bytes'): self.resolve()
