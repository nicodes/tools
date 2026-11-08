import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('installed_resource', Path(__file__).parents[1] / 'helpers/installed_resource.py')
resource = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resource)


def record(service='postgres'):
    identity='sha256:'+'a'*64
    artifact={'kind':'image','reference':'ghcr.io/example/app@'+identity,'digest':identity,
        'config_digest':'sha256:'+'b'*64,'platform':'linux/amd64'}
    provider={key:'a'*40 for key in resource.PROVIDERS}
    release={'kind':'komizo.release','version':1,'application':'app',
        'source':{'repository':'example/app','repository_id':'123','revision':'a'*40},
        'providers':provider,'artifacts':{'image':artifact},
        'workload':{'services':{service:{'artifact':'image','user':'0:0'}}}}
    digest='sha256:'+hashlib.sha256(resource.canonical(release)).hexdigest()
    return {'kind':'komizo.resource','version':1,'application':'app','instance':'production','service':service,
        'release_digest':digest,'container_id':'c'*64,'container_name':'komizo_app_postgres',
        'image_ref':artifact['reference'],'image_config_digest':artifact['config_digest'],
        'volumes':{'postgres-data':{'name':'retained_pg_data','id':'sha256:'+'d'*64}},
        'deployment':{'release':release,'release_digest':digest,'provider':provider}}


class ProtectedResourceBoundary(unittest.TestCase):
    def test_query_uses_only_absolute_protected_readonly_command_and_sanitized_local_authority(self):
        value=record()
        with patch.object(resource.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(value).encode(),b'')) as run:
            with patch.dict(os.environ,{'CONTAINER_HOST':'ssh://untrusted','DOCKER_HOST':'tcp://untrusted','REGISTRY_AUTH_FILE':'/untrusted'}):
                self.assertEqual(resource.query('app','postgres'),value)
        args,kwargs=run.call_args
        self.assertEqual(args[0],['/usr/local/bin/komizo-box','resource','--application','app','--instance','production','--service','postgres'])
        self.assertEqual(kwargs['timeout'],30)
        self.assertFalse({'CONTAINER_HOST','DOCKER_HOST','REGISTRY_AUTH_FILE'} & set(kwargs['env']))

    def test_malformed_selectors_never_reach_a_process(self):
        for application,service in [('app;exec','postgres'),('app','../postgres'),('', 'postgres'),('app','--help')]:
            with self.subTest(application=application,service=service),patch.object(resource.subprocess,'run') as run:
                with self.assertRaises(ValueError):resource.query(application,service)
                run.assert_not_called()

    def test_duplicate_nonfinite_large_failed_and_timedout_queries_refuse_without_secret_errors(self):
        raw=json.dumps(record()).encode()
        for result in [subprocess.CompletedProcess([],0,b'{"kind":1,"kind":2}',b''),
                       subprocess.CompletedProcess([],0,b'{"value":NaN}',b''),
                       subprocess.CompletedProcess([],0,raw+b' '*(4<<20),b''),
                       subprocess.CompletedProcess([],7,b'',b'private-secret'),
                       subprocess.TimeoutExpired(['private-secret'],30,stderr=b'private-secret')]:
            with patch.object(resource.subprocess,'run',**({'side_effect':result} if isinstance(result,Exception) else {'return_value':result})):
                with self.assertRaises(ValueError) as raised:resource.query('app','postgres')
                self.assertNotIn('private-secret',str(raised.exception))

    def test_resource_source_platform_config_provider_and_descriptor_tampering_refuse(self):
        mutations=[lambda v:v.update(application='foreign'),lambda v:v.update(instance='preview-1'),
            lambda v:v.update(service='api'),lambda v:v.update(container_id='named-container'),
            lambda v:v.update(image_ref='ghcr.io/example/app:latest'),lambda v:v.update(image_config_digest='b'*64),
            lambda v:v['deployment']['release']['source'].update(revision='f'*40),
            lambda v:v['deployment']['provider'].update(core_revision='f'*40),
            lambda v:v['volumes']['postgres-data'].update(id='arbitrary-volume')]
        for mutate in mutations:
            value=copy.deepcopy(record());mutate(value)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):resource.validate(value,'app','postgres')

    def test_inspection_checks_exact_id_config_user_and_running_state_without_repodigest_alias(self):
        value=record();info={'Id':value['container_id'],'Image':value['image_config_digest'][7:],
            'Config':{'User':'0:0','Image':value['image_config_digest']},'State':{'Running':True}}
        self.assertEqual(resource.inspect(value,json.dumps([info])),info)
        for fields in [{'Id':'d'*64},{'Image':'sha256:'+'d'*64},{'Config':{'User':'999:999'}},
                       {'State':{'Running':False}},{'State':{'Running':True,'Paused':True}},
                       {'State':{'Running':True,'Restarting':True}}]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                resource.inspect(value,json.dumps([dict(info,**fields)]))

    def test_owned_volume_name_is_required_without_reconstructing_a_name(self):
        value=record();mount={'Destination':'/var/lib/postgresql','Type':'volume','Name':'retained_pg_data'}
        self.assertEqual(resource.volume(value,{'Mounts':[mount]},'postgres-data','/var/lib/postgresql'),'retained_pg_data')
        for mounts in [[],[mount,mount],[dict(mount,Type='bind')],[dict(mount,Name='foreign')]]:
            with self.subTest(mounts=mounts),self.assertRaises(ValueError):
                resource.volume(value,{'Mounts':mounts},'postgres-data','/var/lib/postgresql')

    def test_complete_descriptor_is_retained_and_mixed_release_refuses(self):
        value=record()
        revision,images,deployment=resource.metadata([value])
        self.assertEqual(revision,'a'*40);self.assertEqual(images,{'image':{'reference':value['image_ref'],'image_id':value['image_config_digest']}})
        self.assertEqual(deployment,value['deployment'])
        changed=copy.deepcopy(value);changed['deployment']['provider']['core_revision']='f'*40
        with self.assertRaises(ValueError):resource.metadata([value,changed])

    def test_restart_and_volume_fingerprint_change_are_detected_by_fresh_protected_query(self):
        value=record()
        with patch.object(resource,'query',return_value=copy.deepcopy(value)) as query:
            resource.unchanged([value]);query.assert_called_once_with('app','postgres')
        for key,changed in [('container_id','d'*64),('volumes',{'postgres-data':{'name':'retained_pg_data','id':'sha256:'+'e'*64}})]:
            update=copy.deepcopy(value);update[key]=changed
            with self.subTest(key=key),patch.object(resource,'query',return_value=update),self.assertRaisesRegex(ValueError,'changed during capture'):
                resource.unchanged([value])
