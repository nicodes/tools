import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tarfile
import tempfile
import unittest
import contextlib
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).parent/'assets/fleet_publisher.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
REVISION, TREE = 'a'*40, 'd'*40
REFERENCE = 'ghcr.io/owner/product:'+REVISION


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root/'state'
        self.state.mkdir(mode=0o700)
        (self.state/'artifacts').mkdir(mode=0o700)
        for name, value in [('STATE', self.state), ('DELIVERIES', self.root/'deliveries'), ('OWNER', os.getuid())]:
            context = patch.object(publisher, name, value)
            context.start();self.addCleanup(context.stop)
        context = patch.object(publisher.operator, 'LOCKS', self.root/'locks')
        context.start();self.addCleanup(context.stop)
        context = patch.object(publisher.operator, 'lock_owner', lambda info: info.st_uid == os.getuid())
        context.start();self.addCleanup(context.stop)
        # A CI worker's tmp parent is legitimately UID 0. Only fixture ownership
        # is substituted; the executable remains a Root-only entry point.
        def protected(path):
            for parent in (path, *path.parents):
                info = parent.lstat()
                if parent.is_symlink() or not parent.is_dir() or info.st_mode & 0o022:
                    raise ValueError('unprotected fixture ancestry')
                if parent == self.root:
                    break
        context = patch.object(publisher, 'protected_directory', protected)
        context.start();self.addCleanup(context.stop)
        self.request = {'operation': 'publish', 'repository': 'owner/product', 'revision': REVISION, 'run_id': 42, 'artifact_id': 55}
        workflow = {'workflow_id': 100, 'workflow_path': '.github/workflows/cd.yml', 'app_id': 15368,
                    'event': 'push', 'jobs': ['Build'], 'integration_job': 'build'}
        token = self.root/'token'
        token.write_bytes(b'synthetic-fixture-token')
        token.chmod(0o600)
        self.contract = {'workflow': workflow, 'artifacts': ['ghcr.io/owner/product'], 'username': 'owner', 'credential': str(token)}
        self.policy = {'version': 1, 'repositories': {'owner/product': self.contract}}
        now = datetime.now(timezone.utc)
        self.grant = {'version': 1, 'repository': 'owner/product', 'revision': REVISION, 'tree': TREE,
                      'head': 'b'*40, 'base': 'c'*40, 'kind': 'merge', 'scope': 'production-delivery',
                      'issued_at': now.isoformat(), 'expires_at': (now+timedelta(hours=1)).isoformat()}
        publisher.persist_delivery(self.grant)
        self.generation = 2
        self.producer_app = 15368
        self.main_reads = 0
        self.race_at = 0
        self.calls = []
        self.directories = []
        self.archive()

    def archive(self, *, bad_layer=False, foreign=False, revision=REVISION, linked=False):
        layer = b'synthetic uncompressed OCI layer'
        config = {'os':'linux','architecture':'amd64',
                  'config':{'Labels':{'org.opencontainers.image.revision':revision,
                                     'org.opencontainers.image.source':'https://github.com/owner/product'}},
                  'rootfs':{'type':'layers','diff_ids':['sha256:'+hashlib.sha256(layer).hexdigest()]}}
        def encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
        def descriptor(body, media): return {'mediaType':media,'digest':'sha256:'+hashlib.sha256(body).hexdigest(),'size':len(body)}
        config_body=encoded(config); configuration=descriptor(config_body,publisher.oci.CONFIG)
        self.config_sha=configuration['digest'][7:]
        layer_row=descriptor(layer,'application/vnd.oci.image.layer.v1.tar')
        manifest_body=encoded({'schemaVersion':2,'mediaType':publisher.oci.MANIFEST,'config':configuration,'layers':[layer_row]})
        manifest=descriptor(manifest_body,publisher.oci.MANIFEST)
        self.manifest_sha=manifest['digest']
        repository='ghcr.io/owner/foreign' if foreign else 'ghcr.io/owner/product'
        self.reference=repository+'@'+manifest['digest']
        rows={'oci-layout':b'{"imageLayoutVersion":"1.0.0"}',
              'index.json':encoded({'schemaVersion':2,'manifests':[{**manifest,'annotations':{'org.opencontainers.image.ref.name':repository+':'+REVISION}}]}),
              'blobs/sha256/'+manifest['digest'][7:]:manifest_body,
              'blobs/sha256/'+configuration['digest'][7:]:config_body,
              'blobs/sha256/'+layer_row['digest'][7:]:layer+b'altered' if bad_layer else layer}
        image=io.BytesIO()
        with tarfile.open(fileobj=image,mode='w') as archive:
            for name,body in rows.items():
                entry=tarfile.TarInfo(name);entry.size=len(body);archive.addfile(entry,io.BytesIO(body))
            if linked:
                entry=tarfile.TarInfo('escape');entry.type=tarfile.SYMTYPE;entry.linkname='/etc/shadow';archive.addfile(entry)
        release={'kind':'komizo.release','version':1,'application':'example','source':{
            'repository':'owner/product','repository_id':'123','revision':REVISION},
            'artifacts':{'api':{'kind':'image','reference':self.reference,'digest':manifest['digest'],
                                'config_digest':configuration['digest'],'platform':'linux/amd64'}},
            'workload':{'services':{'api':{'artifact':'api'}},'jobs':{},'assets':{}}}
        packet={'release.json':encoded(release),'images/api.oci.tar':image.getvalue()}
        inventory={'version':1,'files':{name:{'sha256':hashlib.sha256(body).hexdigest(),'size':len(body),
            'type':'release' if name=='release.json' else 'image'} for name,body in packet.items()}}
        packet['inventory.json']=encoded(inventory)
        packet['publication-source.json']=encoded({'version':1,'repository':'owner/product','revision':REVISION,
            'tree':TREE,'run_id':42,'attempt':2})
        self.path=self.state/'artifacts/55.zip'
        with zipfile.ZipFile(self.path,'w') as archive:
            for name,body in packet.items(): archive.writestr(name,body)
        self.path.chmod(0o600); self.archive_sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.remote={}

    def backend(self, args):
        url = args[-1]
        if url.endswith('/git/ref/heads/main'):
            self.main_reads += 1
            return {'object': {'sha': 'f'*40 if self.main_reads == self.race_at else REVISION}}
        if '/git/commits/' in url:
            return {'sha': REVISION, 'tree': {'sha': TREE}, 'parents': [{'sha': 'c'*40}, {'sha': 'b'*40}]}
        if url.endswith('/check-runs?per_page=100'):
            return {'total_count': 1, 'check_runs': [{'name': 'Build', 'status': 'completed', 'conclusion': 'success',
                     'details_url': 'https://github.com/owner/product/actions/runs/42/job/101'}]}
        if '/attempts/' in url:
            return {'total_count': 1, 'jobs': [{'id': 101, 'name': 'Build', 'run_attempt': self.generation, 'head_sha': REVISION,
                'status': 'completed', 'conclusion': 'success', 'check_run_url': 'https://api.github.com/repos/owner/product/check-runs/201'}]}
        if '/check-runs/201' in url:
            return {'app': {'id': self.producer_app}, 'name': 'Build', 'head_sha': REVISION, 'check_suite': {'id': 300},
                'details_url': 'https://github.com/owner/product/actions/runs/42/job/101', 'status': 'completed', 'conclusion': 'success'}
        if '/actions/runs/42' in url:
            return {'id': 42, 'workflow_id': 100, 'path': '.github/workflows/cd.yml', 'event': 'push', 'head_sha': REVISION,
                'head_branch': 'main', 'repository': {'full_name': 'owner/product'}, 'head_repository': {'full_name': 'owner/product'},
                'status': 'in_progress', 'conclusion': None, 'run_attempt': 2, 'check_suite_id': 300}
        if '/actions/artifacts/55' in url:
            return {'id': 55, 'expired': False, 'name': 'fleet-publication-42-2', 'size_in_bytes': self.path.stat().st_size,
                'digest': 'sha256:'+self.archive_sha, 'workflow_run': {'id': 42, 'head_sha': REVISION}}
        raise AssertionError('unexpected evidence request: '+str(args))

    def transport(self, reference, username, password, deadline):
        self.assertEqual((username,password),('owner','synthetic-fixture-token'))
        self.assertLess(time.monotonic(),deadline)
        outer=self
        class Registry:
            _prefix='/v2/owner/product'
            def upload_blob(self, digest, size, reader):
                body=reader.read(); outer.assertEqual(len(body),size)
                outer.assertEqual('sha256:'+hashlib.sha256(body).hexdigest(),digest)
                outer.calls.append(['upload_blob',digest,size])
            def put_manifest(self, digest, raw):
                outer.assertEqual('sha256:'+hashlib.sha256(raw).hexdigest(),digest)
                outer.calls.append(['put_manifest',digest]);outer.remote[digest]=raw
            def _request(self, method, path, headers):
                outer.calls.append(['observe',method,path])
                digest=path.rsplit('/',1)[-1]
                return 200,{'docker-content-digest':digest},outer.remote[digest]
        return Registry()

    def publish(self, transport=None):
        return publisher.execute(self.request, self.policy, self.backend, transport or self.transport)

    def test_exact_publication_retains_immutable_digest_cleans_credentials_and_refuses_replay(self):
        result = self.publish()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['published'][self.reference]['manifest_digest'], self.manifest_sha)
        self.assertFalse(list(self.state.glob('.publication-*')))
        self.assertNotIn(b'synthetic-fixture-token', b''.join(path.read_bytes() for path in self.state.glob('*.json')))
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.publish()
        self.assertEqual(len(self.calls), before)

    def test_main_race_wrong_producer_and_attempt_refuse_before_load_or_credential(self):
        for change in ('race', 'app', 'attempt'):
            with self.subTest(change=change):
                self.main_reads = 0;self.race_at = 2 if change == 'race' else 0
                self.producer_app = 99 if change == 'app' else 15368
                self.generation = 1 if change == 'attempt' else 2
                with patch.object(publisher, 'registry_login') as login, self.assertRaises(ValueError):
                    self.publish()
                login.assert_not_called();self.assertEqual(self.calls, [])

    def test_changed_artifact_layer_destination_revision_or_tar_link_has_zero_effects(self):
        for alteration in ('bytes', 'layer', 'destination', 'revision', 'link'):
            with self.subTest(alteration=alteration):
                self.archive(bad_layer=alteration == 'layer', foreign=alteration == 'destination',
                             revision='f'*40 if alteration == 'revision' else REVISION, linked=alteration == 'link')
                if alteration == 'bytes':
                    self.path.write_bytes(self.path.read_bytes()+b'changed')
                with patch.object(publisher, 'registry_login') as login, self.assertRaises(ValueError):
                    self.publish()
                login.assert_not_called();self.assertEqual(self.calls, [])

    def test_interrupted_upload_is_durable_ambiguous_and_reconciles_without_push(self):
        def interrupted(*args):
            client=self.transport(*args); put=client.put_manifest
            def lost(digest,raw):
                put(digest,raw)
                raise ValueError('synthetic response lost after upload')
            client.put_manifest=lost
            return client
        with self.assertRaises(ValueError):
            self.publish(interrupted)
        self.assertFalse(list(self.state.glob('.publication-*')))
        self.assertNotIn(b'synthetic-fixture-token', b''.join(path.read_bytes() for path in self.state.glob('*.json')))
        request = {'operation': 'inspect', 'repository': 'owner/product', 'revision': REVISION}
        observed = publisher.execute(request, self.policy)
        self.assertEqual(observed['status'], 'ambiguous')
        self.calls.clear()
        request['operation'] = 'reconcile'
        result = publisher.execute(request, self.policy, transport=self.transport)
        self.assertEqual(result['status'], 'reconciled')
        self.assertFalse(any(args[0] in ('upload_blob', 'put_manifest') for args in self.calls))
        with self.assertRaises(ValueError):
            self.publish()

    def test_delivery_is_immutable_and_unsafe_artifact_descriptor_refuses(self):
        with self.assertRaises(ValueError):
            publisher.persist_delivery({**self.grant, 'tree': 'f'*40})
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.calls, [])
        self.path.unlink();os.mkfifo(self.path, 0o600)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.calls, [])

    def test_expired_delivery_refuses_before_any_registry_operation(self):
        key = hashlib.sha256(b'owner/product').hexdigest()
        grant = {**self.grant, 'expires_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}
        publisher.atomic(publisher.DELIVERIES/key/(REVISION+'.json'), grant)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.calls, [])

    def test_actual_process_death_retains_upload_intent_and_refuses_reexecution(self):
        child = os.fork()
        if child == 0:
            def killed(*args):
                client=self.transport(*args); put=client.put_manifest
                def death(digest,raw):
                    put(digest,raw); os.kill(os.getpid(),signal.SIGKILL)
                client.put_manifest=death
                return client
            try:
                self.publish(killed)
            except BaseException:
                os._exit(2)
            os._exit(3)
        deadline = time.monotonic()+5
        try:
            while True:
                observed, status = os.waitpid(child, os.WNOHANG)
                if observed:
                    self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGKILL)
                    break
                if time.monotonic() >= deadline:
                    self.fail('synthetic publisher child exceeded deadline')
                time.sleep(.01)
        finally:
            if not observed:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
        request = {'operation': 'inspect', 'repository': 'owner/product', 'revision': REVISION}
        result = publisher.execute(request, self.policy)
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['persisted_status'], 'uploading')
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.publish()
        self.assertEqual(self.calls, [])

    def test_entrypoint_returns_no_untrusted_input_in_error(self):
        result = subprocess.run([sys.executable, '-I', publisher.__file__], input=b'{"credential":"never-echo-this"}',
                                capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'never-echo-this', result.stdout+result.stderr)

    def test_native_registry_transport_bounds_output_and_deadline(self):
        class Response:
            status=200
            def getheaders(self): return []
            def read1(self,maximum): return b'x'*maximum
        class Connection:
            sock=None
            closed=False
            def __init__(self,*args,**kwargs): pass
            def request(self,*args,**kwargs): pass
            def getresponse(self): return Response()
            def close(self): self.closed=True
        client=publisher.registry_api.Registry('ghcr.io','owner/product',timeout=1)
        with patch.object(publisher.registry_api.http.client,'HTTPSConnection',Connection):
            with self.assertRaisesRegex(ValueError,'response exceeds'):
                client._request('GET','/v2/owner/product/manifests/'+self.manifest_sha)
        client._deadline=time.monotonic()-1
        with self.assertRaisesRegex(ValueError,'deadline'):
            client._request('GET','/v2/owner/product/manifests/'+self.manifest_sha)


if __name__ == '__main__':
    unittest.main()
