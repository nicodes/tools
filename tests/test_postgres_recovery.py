import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch


def helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1]/'helpers'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pg = helper('postgres-recovery')
snapshot = helper('snapshot')
ENGINE = 'docker.io/library/postgres@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280'
REVISION = 'a'*40


def bind_deployment(manifest):
    manifest['images']['postgres']={'reference':manifest['engine']['reference'],'image_id':manifest['engine']['image_id']}
    artifacts={name:{'kind':'image','reference':image['reference'],'digest':image['reference'].rsplit('@',1)[1],
                     'config_digest':image['image_id'],'platform':'linux/amd64'} for name,image in manifest['images'].items()}
    release={'kind':'komizo.release','version':1,'application':manifest['project'],
        'source':{'repository':'example/'+manifest['project'],'repository_id':'123','revision':manifest['revision']},
        'artifacts':artifacts,'workload':{'services':{name:{'artifact':name} for name in artifacts}}}
    release['providers']={'cookbook_revision':'a'*40,'tools_revision':'b'*40,'core_revision':'c'*40}
    manifest['deployment']={'release':release,'release_digest':'sha256:'+__import__('hashlib').sha256(pg.canonical(release)).hexdigest(),
        'provider':{'cookbook_revision':'a'*40,'tools_revision':'b'*40,'core_revision':'c'*40}}
    return manifest


def metadata(manifest):
    return {key:manifest[key] for key in ['project','revision','engine','roles','images','deployment','role_namespace'] if key in manifest}


def payload(root, dump=b'PGDMPfixture', engine_id='sha256:'+'b'*64):
    root.mkdir(mode=0o700)
    (root/'database.dump').write_bytes(dump)
    (root/'files').mkdir()
    (root/'files'/'original.png').write_bytes(b'synthetic immutable original')
    (root/'secrets').mkdir()
    (root/'secrets'/'revocation.key').write_bytes(b'synthetic-only-recovery-key')
    manifest = {
        'format': pg.FORMAT, 'project': 'sampleapp', 'revision': REVISION, 'database': 'sampleapp',
        'engine': {'reference': ENGINE, 'image_id': engine_id, 'major': 18},
        'roles': ['sampleapp_owner', 'sampleapp_runtime'],
        'images': {name: {'reference': f'ghcr.io/nicodes/sampleapp-{name}@sha256:{"d"*64}', 'image_id': 'sha256:'+'c'*64}
                   for name in ['api', 'gate']},
        # The parser validates this declaration; only a product capture test
        # can establish that its producer actually held the stated snapshot.
        'capture': {'snapshot_id': '00000003-0000001A-1', 'reclamation_fenced': True, 'completed_at': '2026-09-09T12:00:00+00:00'},
        'files': {str(file.relative_to(root)): {'size': file.stat().st_size, 'sha256': pg.checksum(file)}
                  for file in root.rglob('*') if file.is_file()},
    }
    bind_deployment(manifest)
    (root/pg.MANIFEST).write_text(json.dumps(manifest))
    return manifest


def archive_payload(source, archive):
    with tarfile.open(archive, 'w:gz') as output:
        for file in sorted(source.rglob('*')):
            output.add(file, arcname=str(file.relative_to(source)), recursive=False)


class PostgreSQLArchiveBoundaries(unittest.TestCase):
    def test_completed_capture_packages_without_modifying_source_or_claiming_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); capture = root/'capture'
            manifest = payload(capture)
            (capture/pg.MANIFEST).unlink()
            report = {**manifest['capture'], 'database': manifest['database'], 'major': 18, 'files': manifest['files']}
            (capture/'capture.json').write_text(json.dumps(report))
            selected_metadata = metadata(manifest)
            output = pg.package_capture(capture, root/'package', selected_metadata)
            evidence = json.loads((output/'verification.json').read_text())
            self.assertEqual(evidence['backend'], 'postgresql')
            self.assertEqual(evidence['database_restore'], 'not_run')
            self.assertFalse((capture/pg.MANIFEST).exists())
            self.assertTrue((capture/'database.dump').is_file())
            (capture/'files'/'original.png').write_text('changed after capture')
            with self.assertRaisesRegex(ValueError, 'changed'):
                pg.package_capture(capture, root/'rejected', selected_metadata)
            self.assertFalse((root/'rejected').exists())

    def test_renamed_project_retains_only_its_explicit_role_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = payload(root/'capture')
            manifest['project'] = 'newapp'
            for name in ('api', 'gate'):
                manifest['images'][name]['reference'] = f'ghcr.io/example/newapp-{name}@sha256:{"d"*64}'
            with self.assertRaisesRegex(ValueError, 'application roles'):
                pg.validate_manifest(manifest)
            manifest['role_namespace'] = 'sampleapp'
            bind_deployment(manifest)
            self.assertEqual(pg.validate_manifest(manifest, 'newapp', REVISION), manifest)
            (root/'capture'/pg.MANIFEST).unlink()
            report = {**manifest['capture'], 'database': manifest['database'], 'major': 18, 'files': manifest['files']}
            (root/'capture/capture.json').write_text(json.dumps(report))
            selected_metadata = metadata(manifest)
            result = pg.package_capture(root/'capture', root/'package', selected_metadata)
            packed = json.loads((result/'verification.json').read_text())['postgresql']
            self.assertEqual(packed['role_namespace'], 'sampleapp')
            self.assertEqual(packed['roles'], ['sampleapp_owner', 'sampleapp_runtime'])
            with self.assertRaisesRegex(ValueError, 'project mismatch'):
                pg.validate_manifest(packed, 'sampleapp', REVISION)
            for namespace in (None, [], '../sampleapp', 'restore_owner', 'sampleapp\n'):
                with self.subTest(namespace=namespace), self.assertRaisesRegex(ValueError, 'role namespace'):
                    pg.validate_manifest({**packed, 'role_namespace': namespace})
            for roles in (['otherapp_runtime'], ['restore_owner'], ['sampleapp_runtime', 'otherapp_owner']):
                with self.subTest(roles=roles), self.assertRaisesRegex(ValueError, 'application roles'):
                    pg.validate_manifest({**packed, 'roles': roles})
            wrong_images = copy.deepcopy(packed)
            wrong_images['images']['api']['reference'] = f'ghcr.io/example/sampleapp-api@sha256:{"d"*64}'
            with self.assertRaisesRegex(ValueError, 'recovery image|recovery images'):
                pg.validate_manifest(wrong_images)

    def test_retained_namespace_admits_only_the_exact_project_backup_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = payload(Path(directory)/'payload')
            manifest.update(project='newapp', role_namespace='sampleapp')
            for component in ('api','gate'):
                manifest['images'][component]['reference']=f'ghcr.io/example/newapp-{component}@sha256:{"d"*64}'
            bind_deployment(manifest)
            manifest['roles'].append('newapp')
            self.assertEqual(pg.validate_manifest(manifest)['roles'],['sampleapp_owner','sampleapp_runtime','newapp'])
            for role in ['newapp_runtime','otherapp','otherapp_reader','postgres','restore_owner','pg_read_all_data','newapp"; DROP ROLE sampleapp_owner; --']:
                with self.subTest(role=role),self.assertRaisesRegex(ValueError,'application roles'):
                    pg.validate_manifest(dict(manifest,roles=[*manifest['roles'],role]))
            with self.assertRaisesRegex(ValueError,'application roles'):
                pg.validate_manifest(dict(manifest,role_namespace='pg',roles=['pg_read_all_data']))
            with self.assertRaisesRegex(ValueError,'application roles'):
                pg.validate_manifest(dict(manifest,roles=['newapp','newapp']))

    def test_complete_inventory_and_explicit_backend_without_fake_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'payload'
            payload(source)
            archive = root/'data.tar.gz'; archive_payload(source, archive)
            target = root/'restored'; target.mkdir()
            with self.assertRaisesRegex(ValueError, 'PocketBase'):
                snapshot.restore(archive, target)
            target = root/'postgres'; target.mkdir()
            result = snapshot.restore(archive, target, backend='postgresql')
            self.assertEqual(result['archive_validation'], 'passed')
            self.assertEqual(result['database_restore'], 'not_run')
            self.assertEqual(result['application_restore'], 'not_run')
            self.assertEqual(result['postgresql']['project'], 'sampleapp')
            (target/'files'/'extra').write_text('undeclared')
            with self.assertRaisesRegex(ValueError, 'inventory'):
                pg.validate_payload(target)

    def test_wrong_project_revision_image_backend_and_manifest_types(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = payload(Path(directory)/'payload')
            with_owner_role = copy.deepcopy(manifest)
            with_owner_role['roles'].append('sampleapp')
            pg.validate_manifest(with_owner_role)
            for mutate in [
                lambda m: m.update(project='another-product'),
                lambda m: m.update(project=[]),
                lambda m: m['engine'].update(reference='postgres:latest'),
                lambda m: m['engine'].update(reference='attacker/postgres@sha256:'+'b'*64),
                lambda m: m['engine'].update(major=True),
                lambda m: m['capture'].update(reclamation_fenced=1),
                lambda m: m['images']['api'].update(reference='ghcr.io/attacker/sampleapp-api:'+REVISION),
                lambda m: m.update(roles=['restore_owner']),
                lambda m: m['files'].update({'../escape': {'size': 0, 'sha256': 'a'*64}}),
            ]:
                value = copy.deepcopy(manifest); mutate(value)
                with self.assertRaises(ValueError): pg.validate_manifest(value)
            with self.assertRaisesRegex(ValueError, 'project mismatch'):
                pg.validate_manifest(manifest, 'samplestore', REVISION)
            with self.assertRaisesRegex(ValueError, 'revision mismatch'):
                pg.validate_manifest(manifest, 'sampleapp', 'd'*40)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                json.loads('{"format":1,"format":2}', object_pairs_hook=pg.unique_object)

    def test_image_owner_follows_project_org_and_refuses_wrong_org(self):
        with tempfile.TemporaryDirectory() as directory:
            sampleapp = payload(Path(directory)/'sampleapp')
            pg.validate_manifest(sampleapp)
            self.assertEqual(sampleapp['images']['api']['reference'], f'ghcr.io/nicodes/sampleapp-api@sha256:{"d"*64}')
            sampleaddon = copy.deepcopy(sampleapp)
            sampleaddon.update(project='sampleaddon', database='sampleaddon', roles=['sampleaddon_owner', 'sampleaddon_runtime'])
            for name in ['api', 'gate']:
                sampleaddon['images'][name]['reference'] = f'ghcr.io/example-games/sampleaddon-{name}@sha256:{"d"*64}'
            bind_deployment(sampleaddon)
            pg.validate_manifest(sampleaddon)
            wrong_org = copy.deepcopy(sampleaddon)
            wrong_org['images']['api']['reference'] = f'ghcr.io/nicodes/sampleaddon-api@sha256:{"d"*64}'
            with self.assertRaisesRegex(ValueError, 'recovery image|recovery images'):
                pg.validate_manifest(wrong_org)
            unknown = copy.deepcopy(sampleapp)
            unknown['project'] = '../unknown'
            with self.assertRaisesRegex(ValueError, 'invalid PostgreSQL recovery project'):
                pg.validate_manifest(unknown)

    def test_no_image_action_precedes_project_binding_and_no_error_discloses_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'payload'; payload(root)
            with patch.object(pg, 'podman') as podman:
                with self.assertRaisesRegex(ValueError, 'project mismatch'):
                    with pg.restored_database(root, 'samplestore', REVISION, pull=True): pass
                podman.assert_not_called()
            with patch.object(pg.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['private-password'], 1)):
                with self.assertRaises(RuntimeError) as raised: pg.podman('exec', 'private-password')
                self.assertNotIn('private-password', str(raised.exception))

    def test_authenticated_backend_cannot_be_downgraded_in_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'payload'; manifest = payload(source)
            archive = root/'data.tar.gz'; archive_payload(source, archive)
            evidence = {'archive_sha256': snapshot.sha256(archive), 'backend': 'postgresql', 'postgresql': manifest,
                        'image_id': manifest['engine']['image_id'], 'image_reference': ENGINE,
                        'verified_at': manifest['capture']['completed_at']}
            (root/'verification.json').write_text(json.dumps(evidence))
            key, cert = root/'key.pem', root/'recipient.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-noenc', '-keyout', str(key),
                            '-out', str(cert), '-days', '1', '-subj', '/CN=synthetic-postgres-recovery'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            encrypted = snapshot.seal(root, cert)
            restored = root/'restored'; restored.mkdir()
            result = snapshot.unseal(encrypted, key, restored)
            self.assertEqual(result['backend'], 'postgresql')
            self.assertEqual(result['database_restore'], 'not_run')
            receipt = json.loads((encrypted/'receipt.json').read_text())
            self.assertEqual(receipt['deployment'],snapshot.deployment_receipt(manifest))
            changed=copy.deepcopy(receipt);changed['deployment']['provider']['core_revision']='f'*40
            (encrypted/'receipt.json').write_text(json.dumps(changed))
            empty=root/'tamper-target';empty.mkdir()
            with self.assertRaisesRegex(ValueError,'deployment or provider'):
                snapshot.unseal(encrypted,key,empty)
            receipt['backend'] = 'pocketbase'
            (encrypted/'receipt.json').write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, 'backend'):
                snapshot.unseal(encrypted, key, restored)

    def test_links_truncation_and_changed_payload_remain_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'payload'; payload(source)
            (source/'files'/'original.png').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'inventory'): pg.validate_payload(source)
            (source/'files'/'alias').symlink_to(source/'database.dump')
            with self.assertRaises(ValueError): pg.validate_payload(source)
            archive = root/'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as output:
                item = tarfile.TarInfo('../escape'); item.size = 1
                output.addfile(item, io.BytesIO(b'x'))
            target = root/'restored'; target.mkdir()
            with self.assertRaises(ValueError): snapshot.restore(archive, target, backend='postgresql')

    def test_native_images_need_no_gate_and_follow_the_whole_frozen_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=payload(Path(directory)/'payload')
            del manifest['images']['gate'];bind_deployment(manifest)
            self.assertEqual(set(pg.validate_manifest(manifest)['images']),{'api','postgres'})
            for component in ['api','postgres']:
                changed=copy.deepcopy(manifest);changed['images'][component]['image_id']='sha256:'+'f'*64
                with self.assertRaisesRegex(ValueError,'differ from the exact deployment'):pg.validate_manifest(changed)
            changed=copy.deepcopy(manifest);changed['images']['foreign']={'reference':'ghcr.io/foreign/example@sha256:'+'f'*64,'image_id':'sha256:'+'e'*64}
            with self.assertRaises(ValueError):pg.validate_manifest(changed)

    def test_retired_source_tags_host_local_revisions_and_unbound_engine_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=payload(Path(directory)/'payload')
            changes=[lambda value:value.update(revision='host-local:transition'),
                lambda value:value.update(format='nicodes-postgresql-logical-v1'),
                lambda value:value['images']['api'].update(reference='ghcr.io/example/app-api:'+REVISION),
                lambda value:value['engine'].update(reference='ghcr.io/foreign/engine@sha256:'+'f'*64),
                lambda value:value.pop('deployment')]
            for change in changes:
                changed=copy.deepcopy(manifest);change(changed)
                with self.assertRaises(ValueError):pg.validate_manifest(changed)

    def test_descriptor_digest_source_and_exact_recipe_provider_revisions_are_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=payload(Path(directory)/'payload')
            for change in [lambda value:value['deployment'].update(release_digest='sha256:'+'0'*64),
                lambda value:value['deployment']['release'].update(application='foreign'),
                lambda value:value['deployment']['release']['source'].update(revision='d'*40),
                lambda value:value['deployment']['provider'].update(tools_revision='main'),
                lambda value:value['deployment']['provider'].update(tools_revision='d'*40),
                lambda value:value['deployment']['provider'].update(command='arbitrary')]:
                changed=copy.deepcopy(manifest);change(changed)
                with self.assertRaises(ValueError):pg.validate_manifest(changed)

    def test_static_archive_payload_and_immutable_manifest_identities_survive_packaging(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest=payload(root/'capture')
            release=manifest['deployment']['release']
            payload_digest='sha256:'+'e'*64;size=123
            static_manifest={'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json',
                'artifactType':'application/vnd.komizo.static.v1',
                'config':{'mediaType':'application/vnd.oci.empty.v1+json','digest':'sha256:'+hashlib.sha256(b'{}').hexdigest(),'size':2},
                'layers':[{'mediaType':'application/vnd.komizo.static.v1.tar+gzip','digest':payload_digest,'size':size}]}
            artifact={'kind':'archive','reference':'ghcr.io/example/static@sha256:'+hashlib.sha256(pg.canonical(static_manifest)).hexdigest(),
                'digest':payload_digest,'size_bytes':size,'media_type':'application/vnd.komizo.static.v1.tar+gzip'}
            release['artifacts']['web']=artifact;manifest['deployment']['release_digest']='sha256:'+hashlib.sha256(pg.canonical(release)).hexdigest()
            pg.validate_manifest(manifest)
            changed=copy.deepcopy(manifest);changed['deployment']['release']['artifacts']['web']['digest']='sha256:'+'f'*64
            changed['deployment']['release_digest']='sha256:'+hashlib.sha256(pg.canonical(changed['deployment']['release'])).hexdigest()
            with self.assertRaisesRegex(ValueError,'static reference'):pg.validate_manifest(changed)
            (root/'capture'/pg.MANIFEST).write_text(json.dumps(manifest))
            archive=root/'data.tar.gz';archive_payload(root/'capture',archive);target=root/'target';target.mkdir()
            restored=snapshot.restore(archive,target,backend='postgresql')
            self.assertEqual(restored['postgresql']['deployment'],manifest['deployment'])

    def test_generic_database_only_controls_need_no_application_or_registry_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=payload(Path(directory)/'payload');manifest['images']={};manifest.pop('deployment')
            self.assertEqual(pg.validate_manifest(manifest)['images'],{})
            manifest['engine']['reference']='ghcr.io/foreign/engine@sha256:'+'f'*64
            with self.assertRaises(ValueError):pg.validate_manifest(manifest)

@unittest.skipUnless(os.environ.get('TOOLS_TEST_POSTGRES') == '1', 'mise run check requires the real isolated PostgreSQL control')
class PostgreSQLRestoreIntegration(unittest.TestCase):
    def test_real_custom_dump_restores_rows_and_acl_and_cleans_up(self):
        self.restore_roundtrip('sampleapp')

    def test_renamed_project_restores_existing_database_and_roles(self):
        self.restore_roundtrip('newapp')

    def restore_roundtrip(self, project):
        pg.podman('pull', '--quiet', ENGINE, timeout=180)
        image = json.loads(pg.podman('image', 'inspect', ENGINE))[0]['Id']
        name = 'tools-pg-source-'+os.urandom(8).hex()
        uid = os.getuid() or 65534
        launched = pg.native_runtime().run(['run', '--pull=never', '-d', '--name', name, '--network=none', '--read-only',
                              *pg.native_runtime().bind_mount_user_namespace(), '--user', f'{uid}:{uid}',
                              '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--memory=256m',
                              '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                              '--tmpfs', '/var/run/postgresql:rw,mode=1777,size=8m',
                              '--tmpfs', '/var/lib/postgresql:rw,mode=1777,size=128m',
                              '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'POSTGRES_USER=sampleapp_owner',
                              '--env', 'POSTGRES_DB=sampleapp', '--env', 'PGDATA=/var/lib/postgresql/data', image,
                              'postgres', '-c', 'listen_addresses=127.0.0.1'], capture_output=True, timeout=60)
        # This creation uses only synthetic public fixture data and pull=never.
        # Retain the native setup error without exposing recovery credentials.
        self.assertEqual(launched.returncode, 0, launched.stderr.decode(errors='replace')[:4096])
        source_id = launched.stdout.decode().strip()
        self.addCleanup(lambda: pg.podman('rm', '-f', '-v', source_id))
        for _ in range(100):
            try:
                pg.podman('exec', source_id, 'pg_isready', '-h', '127.0.0.1', '-U', 'sampleapp_owner', '-d', 'sampleapp')
                break
            except RuntimeError: time.sleep(.1)
        else: self.fail('synthetic PostgreSQL source did not start')
        pg.podman('exec', '-i', source_id, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'sampleapp_owner', '-d', 'sampleapp', input='''
CREATE ROLE sampleapp_runtime LOGIN;
CREATE TABLE preserved(id integer PRIMARY KEY,value text NOT NULL);
INSERT INTO preserved VALUES(1,'captured');
GRANT SELECT ON preserved TO sampleapp_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sampleapp_owner IN SCHEMA public GRANT SELECT ON TABLES TO sampleapp_runtime;
CREATE FUNCTION private_count() RETURNS bigint LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS 'SELECT count(*) FROM public.preserved';
REVOKE ALL ON FUNCTION private_count() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION private_count() TO sampleapp_runtime;
''')
        if project != 'sampleapp':
            pg.podman('exec', '-i', source_id, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'sampleapp_owner', '-d', 'sampleapp', input=f'''
CREATE ROLE "{project}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT SELECT ON preserved TO "{project}";
ALTER DEFAULT PRIVILEGES FOR ROLE sampleapp_owner IN SCHEMA public GRANT SELECT ON TABLES TO "{project}";
''')
        pg.podman('exec', source_id, 'pg_dump', '-Fc', '-U', 'sampleapp_owner', '-d', 'sampleapp', '-f', '/tmp/database.dump')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'payload'; manifest = payload(source, engine_id=image)
            if project != 'sampleapp':
                manifest.update(project=project, role_namespace='sampleapp')
                manifest['roles'].append(project)
                for component in ('api', 'gate'):
                    manifest['images'][component]['reference'] = f'ghcr.io/example/{project}-{component}@sha256:{"d"*64}'
            bind_deployment(manifest)
            # Stream bytes rather than relying on Podman's archive-copy support
            # for a running read-only container with a tmpfs source file.
            with (source/'database.dump').open('wb') as output:
                copied = pg.native_runtime().run(['exec', source_id, 'cat', '/tmp/database.dump'],
                                        stdout=output, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(copied.returncode, 0, 'could not stream the synthetic custom dump')
            manifest['files']['database.dump'] = {'size': (source/'database.dump').stat().st_size,
                                                 'sha256': pg.checksum(source/'database.dump')}
            (source/pg.MANIFEST).write_text(json.dumps(manifest))
            pg.podman('exec', source_id, 'psql', '-X', '-U', 'sampleapp_owner', '-d', 'sampleapp', '-c', "UPDATE preserved SET value='after snapshot'")
            with pg.restored_database(source, project, REVISION) as clone:
                restored_id = clone['container']
                self.assertEqual(clone['database_restore'], 'passed')
                self.assertNotIn('roles', clone)
                actual = pg.podman('exec', restored_id, 'psql', '-XAt', '-U', 'restore_owner', '-d', 'sampleapp', '-c', 'SELECT value FROM preserved')
                self.assertEqual(actual, 'captured')
                acl = pg.podman('exec', restored_id, 'psql', '-XAt', '-U', 'sampleapp_runtime', '-d', 'sampleapp', '-c', 'SELECT private_count()')
                self.assertEqual(acl, '1')
                public_acl = pg.podman('exec', restored_id, 'psql', '-XAt', '-U', 'restore_owner', '-d', 'sampleapp', '-c', "SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a WHERE p.proname='private_count' AND a.grantee=0")
                self.assertEqual(public_acl, '0')
                if project != 'sampleapp':
                    reader = pg.podman('exec', restored_id, 'psql', '-XAt', '-U', project, '-d', 'sampleapp', '-c', 'SELECT value FROM preserved')
                    self.assertEqual(reader, 'captured')
                    flags = pg.podman('exec', restored_id, 'psql', '-XAt', '-U', 'restore_owner', '-d', 'sampleapp', '-c',
                        f"SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname='{project}'")
                    self.assertEqual(flags, 'f')
                    for sql in ["UPDATE preserved SET value='forbidden'", 'SELECT private_count()']:
                        with self.assertRaises(RuntimeError):
                            pg.podman('exec', restored_id, 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1', '-U', project, '-d', 'sampleapp', '-c', sql)
                    inherited = pg.podman('exec', restored_id, 'psql', '-XAt', '-U', 'restore_owner', '-d', 'sampleapp', '-c',
                        f"SELECT count(*) FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a JOIN pg_roles r ON r.oid=a.grantee WHERE r.rolname='{project}' AND a.privilege_type='SELECT'")
                    self.assertEqual(inherited, '1')
            with self.assertRaises(RuntimeError): pg.podman('inspect', restored_id)
            # A valid manifest/header cannot turn a broken pg_restore into a
            # passed database result. Owned-container cleanup still runs.
            (source/'database.dump').write_bytes((source/'database.dump').read_bytes()[:8])
            manifest['files']['database.dump'] = {'size': 8, 'sha256': pg.checksum(source/'database.dump')}
            (source/pg.MANIFEST).write_text(json.dumps(manifest))
            created = []
            real_podman = pg.podman
            def tracked(*args, **kwargs):
                if args[0] == 'run': created.append(args[args.index('--name')+1])
                return real_podman(*args, **kwargs)
            with patch.object(pg, 'podman', tracked), self.assertRaises(RuntimeError):
                with pg.restored_database(source, project, REVISION):
                    self.fail('corrupt custom dump yielded a restored database')
            self.assertEqual(len(created), 1)
            with self.assertRaises(RuntimeError): real_podman('inspect', created[0])
