"""Deployed scans resolve authenticated immutable descriptors, never source tags."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
                  'referenced_workflows':[{'path':'nicodes/komizo-actions/.github/workflows/cookbook-publish.yml@'+'c'*40,'sha':'c'*40}],
                  'repository':{'id':123,'full_name':'example/widget'},'head_repository':{'full_name':'example/widget'},'run_attempt':2}
        self.job={'id':101,'run_id':42,'run_attempt':2,'head_sha':self.source,'conclusion':'success'}
        self.descriptor={'kind':'komizo.release','version':1,'application':'widget',
            'source':{'repository':'example/widget','repository_id':'123','revision':self.source},
            'artifacts':{'worker':{'kind':'image','reference':'ghcr.io/example/widget-worker@sha256:'+'e'*64,
                                 'digest':'sha256:'+'e'*64,'config_digest':'sha256:'+'b'*64,'platform':'linux/amd64'}},
            'workload':{'services':{'worker':{'artifact':'worker'}},'jobs':{},'assets':{}}}
        self.context={'GITHUB_SERVER_URL':'https://github.com','GITHUB_EVENT_NAME':'push','GITHUB_JOB':'publish','GITHUB_REPOSITORY':'example/widget','GITHUB_REPOSITORY_ID':'123','GITHUB_SHA':self.source,
            'GITHUB_RUN_ID':'42','GITHUB_RUN_ATTEMPT':'2','GITHUB_WORKFLOW_REF':'example/widget/.github/workflows/cd.yml@refs/heads/main',
            'GITHUB_WORKFLOW_SHA':self.source}
        self.archive()

    def archive(self, *, change=None, extra=None):
        release=scanner.registry.canonical(self.descriptor); digest=scanner.registry.digest(release)
        source_context={**self.context,'GITHUB_JOB':'validate'}
        inventory={'version':1,'files':{'release.json':{'type':'release','sha256':hashlib.sha256(release).hexdigest(),'size':len(release)},
            'images/worker.oci.tar':{'type':'image','sha256':'c'*64,'size':10240}}}
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
                     'published':{'worker':self.descriptor['artifacts']['worker']['reference']},'execution_context':self.context,
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
        if path.endswith('/actions/runs/42/artifacts?per_page=100'): return {'total_count':1,'artifacts':[self.artifact]}
        raise AssertionError(path)

    def resolve(self):
        with patch.object(scanner,'api',side_effect=self.backend),patch.object(scanner,'download',return_value=self.raw):
            return scanner.release_for_deployment(self.config,self.deployment)

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
        self.assertEqual(self.resolve(),self.descriptor)
        self.deployment['state']='failure';self.job['conclusion']='failure'
        self.assertEqual(self.resolve(),self.descriptor)

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
