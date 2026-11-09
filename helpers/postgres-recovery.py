#!/usr/bin/env python3
"""Bounded PostgreSQL logical archives and disposable, local-only DB restores.

Application capture/verification stays product-owned. A manifest/checksum check
is not reported as a successful database or application restore.
"""
from contextlib import contextmanager
from datetime import datetime
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import tempfile
import tarfile
import time

FORMAT = 'postgresql-logical-v2'
MANIFEST = 'postgresql.json'
MAX_MANIFEST = 16 * 1024**2
MAX_FILES = 100000
MAX_BYTES = 16 * 1024**3
HEX_REVISION = re.compile(r'[a-f0-9]{40}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
COMPONENT = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate PostgreSQL manifest field')
        value[key] = item
    return value


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and '..' not in path.parts and str(path) == name


def immutable_reference(value):
    if not isinstance(value,str) or len(value)>512:
        return False
    match=re.fullmatch(r'([a-z0-9][a-z0-9.-]*(?::[1-9][0-9]{0,4})?)/(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[a-f0-9]{64}',value)
    if not match or '..' in match[1]:
        return False
    return ':' not in match[1] or int(match[1].rsplit(':',1)[1])<=65535


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def validate_deployment(value, project, revision, images, engine):
    """Bind generic data recovery to exact source/descriptor/provider bytes.

    Workload semantics and application probes remain owned by the descriptor's
    pinned native compiler and product adapter. This reader never executes them.
    """
    if (not isinstance(value,dict) or set(value)!={'release','release_digest','provider'}
            or not isinstance(value['release_digest'],str) or not DIGEST.fullmatch(value['release_digest'])
            or not isinstance(value['provider'],dict)
            or set(value['provider'])!={'cookbook_revision','tools_revision','core_revision'}
            or any(not isinstance(item,str) or not HEX_REVISION.fullmatch(item) for item in value['provider'].values())):
        raise ValueError('recovery deployment requires exact descriptor and provider identities')
    release=value['release']
    required={'kind','version','application','source','artifacts','workload'}
    if (not isinstance(release,dict) or not required<=set(release)<=required|{'stateful_contract','providers'}
            or release['kind']!='komizo.release' or type(release['version']) is not int or release['version']!=1
            or release['application']!=project or not isinstance(release['workload'],dict)
            or not isinstance(release['source'],dict) or set(release['source'])!={'repository','repository_id','revision'}
            or release['source']['revision']!=revision
            or not isinstance(release['source']['repository'],str)
            or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',release['source']['repository'])
            or release['source']['repository'].split('/')[1] in {'.','..'}
            or not isinstance(release['source']['repository_id'],str)
            or not re.fullmatch('[1-9][0-9]{0,19}',release['source']['repository_id'])):
        raise ValueError('recovery deployment is not bound to the project and exact source')
    if release.get('providers')!=value['provider']:
        raise ValueError('recovery provider differs from its descriptor-bound source')
    raw=canonical(release)
    if len(raw)>4<<20 or 'sha256:'+hashlib.sha256(raw).hexdigest()!=value['release_digest']:
        raise ValueError('recovery deployment differs from its immutable descriptor digest')
    artifacts=release['artifacts'];expected={};engines=[]
    if not isinstance(artifacts,dict) or not 1<=len(artifacts)<=32:
        raise ValueError('recovery deployment has no closed bounded artifact set')
    for name,artifact in artifacts.items():
        if (not isinstance(name,str) or not COMPONENT.fullmatch(name) or not isinstance(artifact,dict)
                or not immutable_reference(artifact.get('reference'))):
            raise ValueError('recovery artifact requires a logical name and immutable reference')
        if artifact.get('kind')=='image':
            if (set(artifact)!={'kind','reference','digest','config_digest','platform'}
                    or artifact['reference'].rsplit('@',1)[1]!=artifact['digest']
                    or not isinstance(artifact['config_digest'],str) or not DIGEST.fullmatch(artifact['config_digest'])
                    or artifact['platform'] not in {'linux/amd64','linux/arm64'}):
                raise ValueError('recovery image differs from its exact OCI identity')
            expected[name]={'reference':artifact['reference'],'image_id':artifact['config_digest']}
            engines.append(expected[name])
        elif artifact.get('kind')=='archive':
            if (set(artifact)!={'kind','reference','digest','size_bytes','media_type'}
                    or not isinstance(artifact['digest'],str) or not DIGEST.fullmatch(artifact['digest'])
                    or type(artifact['size_bytes']) is not int or not 0<artifact['size_bytes']<=MAX_BYTES
                    or artifact['media_type']!='application/vnd.komizo.static.v1.tar+gzip'):
                raise ValueError('recovery static archive has no immutable payload identity')
            manifest={'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json',
                'artifactType':'application/vnd.komizo.static.v1',
                'config':{'mediaType':'application/vnd.oci.empty.v1+json','digest':'sha256:'+hashlib.sha256(b'{}').hexdigest(),'size':2},
                'layers':[{'mediaType':artifact['media_type'],'digest':artifact['digest'],'size':artifact['size_bytes']}]}
            if artifact['reference'].rsplit('@',1)[1]!='sha256:'+hashlib.sha256(canonical(manifest)).hexdigest():
                raise ValueError('recovery static reference differs from its exact payload')
        else:
            raise ValueError('recovery deployment contains an unsupported artifact kind')
    if images!=expected or {'reference':engine['reference'],'image_id':engine['image_id']} not in engines:
        raise ValueError('recovery images or database engine differ from the exact deployment')
    return value


def validate_manifest(value, expected_project=None, expected_revision=None):
    required = {'format', 'project', 'revision', 'database', 'engine', 'roles', 'images', 'capture', 'files'}
    if not isinstance(value, dict) or not required<=set(value)<=required|{'role_namespace','deployment'} or value['format'] != FORMAT:
        raise ValueError('unknown or incomplete PostgreSQL recovery manifest')
    project, revision = value['project'], value['revision']
    if (not isinstance(project,str) or not COMPONENT.fullmatch(project)
            or not isinstance(revision,str) or not HEX_REVISION.fullmatch(revision)):
        raise ValueError('invalid PostgreSQL recovery project or exact source revision')
    if expected_project is not None and project != expected_project:
        raise ValueError('PostgreSQL recovery project mismatch')
    if expected_revision is not None and revision != expected_revision:
        raise ValueError('PostgreSQL recovery revision mismatch')
    if not isinstance(value['database'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value['database']):
        raise ValueError('invalid PostgreSQL recovery database name')
    engine = value['engine']
    if not isinstance(engine, dict) or set(engine) != {'reference', 'image_id', 'major'}:
        raise ValueError('invalid PostgreSQL engine declaration')
    if not isinstance(engine['reference'], str) or not immutable_reference(engine['reference']):
        raise ValueError('PostgreSQL recovery requires an immutable engine reference')
    if not isinstance(engine['image_id'], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', engine['image_id']):
        raise ValueError('invalid PostgreSQL engine image identity')
    if type(engine['major']) is not int or engine['major'] != 18:
        raise ValueError('unsupported PostgreSQL recovery major version')
    # A renamed application may retain its database roles. The authenticated
    # manifest declares that namespace explicitly; omission preserves the
    # original project-scoped contract. Images remain bound to project.
    role_namespace = value.get('role_namespace', project)
    if (not isinstance(role_namespace, str)
            or not re.fullmatch(r'[a-z][a-z0-9-]*', role_namespace)):
        raise ValueError('invalid PostgreSQL role namespace')
    roles = value['roles']
    if not isinstance(roles, list) or not 1 <= len(roles) <= 8 or any(
            not isinstance(role, str) or not re.fullmatch(re.escape(role_namespace) + r'(?:_[a-z][a-z0-9_]{0,40})?', role)
            for role in roles) or len(set(roles)) != len(roles):
        raise ValueError('invalid PostgreSQL application roles')
    images = value['images']
    if (not isinstance(images,dict) or len(images)>32
            or any(not isinstance(name,str) or not COMPONENT.fullmatch(name) or not isinstance(image,dict)
                   or set(image)!={'reference','image_id'} or not immutable_reference(image['reference'])
                   or not isinstance(image['image_id'],str) or not DIGEST.fullmatch(image['image_id'])
                   for name,image in images.items())):
        raise ValueError('invalid immutable recovery image set')
    if 'deployment' in value:
        validate_deployment(value['deployment'],project,revision,images,engine)
    elif images or not re.fullmatch(r'docker\.io/library/postgres@sha256:[a-f0-9]{64}',engine['reference']):
        raise ValueError('application recovery requires its exact deployment descriptor and providers')
    capture = value['capture']
    if not isinstance(capture, dict) or set(capture) != {'snapshot_id', 'reclamation_fenced', 'completed_at'}:
        raise ValueError('incomplete PostgreSQL capture declaration')
    if not isinstance(capture['snapshot_id'], str) or not re.fullmatch(r'[A-Fa-f0-9]+-[A-Fa-f0-9]+-[0-9]+', capture['snapshot_id']) or capture['reclamation_fenced'] is not True:
        raise ValueError('capture must declare one exported snapshot and fenced file reclamation')
    try:
        stamp = datetime.fromisoformat(capture['completed_at'])
        if stamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('invalid PostgreSQL capture completion timestamp') from None
    files = value['files']
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES or 'database.dump' not in files:
        raise ValueError('incomplete PostgreSQL file inventory')
    total = 0
    for name, entry in files.items():
        if not safe_path(name) or (name != 'database.dump' and not name.startswith(('files/', 'secrets/'))):
            raise ValueError('invalid PostgreSQL archive path')
        if not isinstance(entry, dict) or set(entry) != {'size', 'sha256'} or type(entry['size']) is not int or entry['size'] < 0:
            raise ValueError('invalid PostgreSQL file size declaration')
        if not isinstance(entry['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', entry['sha256']):
            raise ValueError('invalid PostgreSQL file digest')
        total += entry['size']
    if total > MAX_BYTES or files['database.dump']['size'] < 5:
        raise ValueError('PostgreSQL payload exceeds bounds or lacks a dump')
    return value


def validate_payload(root, extracted_files=None, expected_project=None, expected_revision=None):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('PostgreSQL payload must be a real directory')
    manifest_path = root / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST:
        raise ValueError('missing or oversized PostgreSQL manifest')
    manifest = validate_manifest(json.loads(manifest_path.read_text(), object_pairs_hook=unique_object), expected_project, expected_revision)
    actual = {}
    total = 0
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('PostgreSQL payload contains links or special files')
        if path.is_file() and path != manifest_path:
            name = str(path.relative_to(root))
            size = path.stat().st_size
            total += size
            if len(actual) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError('PostgreSQL payload exceeds inventory bounds')
            actual[name] = {'size': size, 'sha256': checksum(path)}
    if actual != manifest['files']:
        raise ValueError('PostgreSQL payload differs from its complete file inventory')
    if extracted_files is not None and set(extracted_files) != set(actual) | {MANIFEST}:
        raise ValueError('PostgreSQL archive contains undeclared files')
    with (root / 'database.dump').open('rb') as dump:
        if dump.read(5) != b'PGDMP':
            raise ValueError('PostgreSQL archive is not a custom-format logical dump')
    return manifest


_runtime = None


def native_runtime():
    global _runtime
    if _runtime is None:
        spec = importlib.util.spec_from_file_location('_tools_pg_runtime', Path(__file__).with_name('container_runtime.py'))
        _runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_runtime)
    return _runtime


def podman(*args, input=None, timeout=60):
    runtime = native_runtime()
    try:
        result = runtime.run(list(args), input=input, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'isolated PostgreSQL {args[0]} timed out') from None
    if result.returncode:
        # Never disclose SQL output, dump contents, arguments or clone passwords.
        raise RuntimeError(f'isolated PostgreSQL {args[0]} failed')
    if args[:2] == ('image', 'inspect'):
        values = json.loads(result.stdout)
        for value in values:
            value['Id'] = runtime.config_digest(value.get('Id'))
        return json.dumps(values)
    return result.stdout.strip()


def package_capture(capture, destination, metadata):
    """Package private completed staging, not a live application volume."""
    if capture.is_symlink() or not capture.is_dir():
        raise ValueError('capture staging must be a real directory')
    report_path = capture/'capture.json'
    if report_path.is_symlink() or not report_path.is_file() or report_path.stat().st_size > MAX_MANIFEST:
        raise ValueError('missing or oversized completed capture report')
    report = json.loads(report_path.read_text(), object_pairs_hook=unique_object)
    if not isinstance(report, dict) or set(report) != {'snapshot_id', 'reclamation_fenced', 'completed_at', 'database', 'major', 'files'}:
        raise ValueError('unknown completed capture report')
    if not isinstance(metadata, dict) or not {'project','revision','engine','roles','images'}<=set(metadata)<={'project','revision','engine','roles','images','role_namespace','deployment'}:
        raise ValueError('invalid recovery image metadata')
    manifest = validate_manifest({**metadata, 'format': FORMAT, 'database': report['database'],
                                  'capture': {key: report[key] for key in ['snapshot_id', 'reclamation_fenced', 'completed_at']},
                                  'files': report['files']})
    if manifest['engine']['major'] != report['major']:
        raise ValueError('capture and recovery engine major differ')
    source = capture.resolve(strict=True)
    target = destination.parent.resolve(strict=True)/destination.name
    if target == source or source in target.parents or target in source.parents:
        raise ValueError('recovery package must be outside capture staging')
    actual = set()
    for path in source.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('capture staging contains links or special files')
        if path.is_file(): actual.add(str(path.relative_to(source)))
        if len(actual) > MAX_FILES + 1:
            raise ValueError('capture staging exceeds its file bound')
    if actual != set(manifest['files']) | {'capture.json'}:
        raise ValueError('capture staging contains missing or undeclared files')
    size = sum(entry['size'] for entry in manifest['files'].values())
    if shutil.disk_usage(target.parent).free < 2 * size + 512 * 1024**2:
        raise ValueError('insufficient headroom to package and verify capture')
    target.mkdir(mode=0o700)
    complete = False
    try:
        encoded = json.dumps(manifest, sort_keys=True).encode()
        archive = target/'data.tar.gz'
        with tarfile.open(archive, 'x:gz') as output:
            header = tarfile.TarInfo(MANIFEST); header.size = len(encoded); header.mode = 0o600
            output.addfile(header, io.BytesIO(encoded))
            for name, entry in sorted(manifest['files'].items()):
                path = source/name
                if path.stat().st_size != entry['size'] or checksum(path) != entry['sha256']:
                    raise ValueError('capture file changed before packaging')
                header = output.gettarinfo(path, arcname=name)
                if not header.isfile(): raise ValueError('capture file became unsafe')
                with path.open('rb') as stream: output.addfile(header, stream)
        archive.chmod(0o600)
        spec = importlib.util.spec_from_file_location('snapshot', Path(__file__).with_name('snapshot.py'))
        snapshot = importlib.util.module_from_spec(spec); spec.loader.exec_module(snapshot)
        with tempfile.TemporaryDirectory(prefix='package-check-', dir=target) as temporary:
            evidence = snapshot.restore(archive, Path(temporary), backend='postgresql')
        evidence.update({'image_id': manifest['engine']['image_id'], 'image_reference': manifest['engine']['reference'],
                         'verified_at': manifest['capture']['completed_at']})
        verification = target/'verification.json'
        verification.write_text(json.dumps(evidence, indent=2)+'\n'); verification.chmod(0o600)
        complete = True
        return target
    finally:
        if not complete: shutil.rmtree(target)


@contextmanager
def restored_database(payload, project, revision, pull=False, timeout=300):
    """Restore into a newly owned local container, yielding only after SQL restore.

    Product code performs application-specific checks inside this context. A
    successful DB restore is not reported as application or worker acceptance.
    """
    if timeout <= 0:
        raise ValueError('restore timeout must be positive')
    manifest = validate_payload(payload, expected_project=project, expected_revision=revision)
    engine = manifest['engine']
    if pull:
        podman('pull', '--quiet', engine['reference'], timeout=180)
    inspected = json.loads(podman('image', 'inspect', engine['image_id']))
    if len(inspected) != 1 or inspected[0]['Id'] != engine['image_id']:
        raise ValueError('restored PostgreSQL engine differs from authenticated metadata')
    uid = os.getuid() or 65534
    name = project + '-pg-restore-' + secrets.token_hex(8)
    with tempfile.TemporaryDirectory(prefix=project+'-pg-restore-') as directory:
        work = Path(directory)
        if any(character in str(work) for character in ',\r\n'):
            raise ValueError('restore temporary path is not safe for Podman mounts')
        if shutil.disk_usage(work).free < 2 * manifest['files']['database.dump']['size'] + 512 * 1024**2:
            raise ValueError('insufficient disk headroom for PostgreSQL restore')
        dump = work/'database.dump'
        shutil.copyfile(payload/'database.dump', dump)
        dump.chmod(0o400)
        if checksum(dump) != manifest['files']['database.dump']['sha256']:
            raise ValueError('PostgreSQL dump changed while staging restore')
        data = work/'database'; data.mkdir(mode=0o700)
        if os.getuid() == 0:
            os.chown(data, uid, uid)
            os.chown(dump, uid, uid)
        password = secrets.token_hex(32)
        environment = work/'postgres.env'
        environment.write_text(f'POSTGRES_USER=restore_owner\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB={manifest["database"]}\nPGDATA=/var/lib/postgresql/data\n')
        environment.chmod(0o600)
        started = False
        try:
            started = True
            podman('run', '--pull=never', '-d', '--name', name, '--label', 'nicodes.restore='+name,
                   '--network=none', '--read-only', *native_runtime().bind_mount_user_namespace(), '--user', f'{uid}:{uid}', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges:true', '--memory=256m', '--cpus=1', '--pids-limit=128',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                   '--tmpfs', '/var/run/postgresql:rw,noexec,nosuid,mode=1777,size=8m',
                   '--mount', f'type=bind,src={data},dst=/var/lib/postgresql/data',
                   '--mount', f'type=bind,src={dump},dst=/recovery/database.dump,readonly',
                   '--env-file', str(environment), engine['image_id'],
                   'postgres', '-c', 'listen_addresses=127.0.0.1')
            deadline = time.monotonic()+min(timeout, 90)
            while True:
                if not json.loads(podman('inspect', name))[0]['State']['Running']:
                    raise RuntimeError('isolated PostgreSQL exited before readiness')
                try:
                    podman('exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'restore_owner', '-d', manifest['database'])
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('isolated PostgreSQL did not become ready') from None
                    time.sleep(.1)
            passwords = {role: secrets.token_hex(32) for role in manifest['roles']}
            sql = '\n'.join(f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD \'{secret}\';'
                            for role, secret in passwords.items())
            podman('exec', '-i', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'restore_owner', '-d', manifest['database'], input=sql)
            podman('exec', name, 'pg_restore', '--exit-on-error', '--single-transaction', '--no-owner',
                   '--no-tablespaces', '--username=restore_owner', '--dbname='+manifest['database'],
                   '/recovery/database.dump', timeout=timeout)
            major = podman('exec', name, 'psql', '-XAt', '-U', 'restore_owner', '-d', manifest['database'],
                           '-c', 'SHOW server_version_num')
            if not major.isdigit() or int(major)//10000 != engine['major']:
                raise ValueError('restored PostgreSQL major differs from manifest')
            credentials = work/'credentials'; credentials.mkdir(mode=0o700)
            role_files = {}
            for role, secret in passwords.items():
                file = credentials/(role+'.url')
                file.write_text(f'postgresql://{role}:{secret}@127.0.0.1:5432/{manifest["database"]}?sslmode=disable\n')
                file.chmod(0o600)
                if os.getuid() == 0:
                    os.chown(file, uid, uid)
                role_files[role] = str(file)
            yield {'container': name, 'database': manifest['database'], 'credentials': role_files,
                   'engine': engine['image_id'], 'manifest': manifest, 'database_restore': 'passed'}
        finally:
            if started:
                owned = json.loads(podman('inspect', name))
                if len(owned) != 1 or owned[0].get('Config', {}).get('Labels', {}).get('nicodes.restore') != name or not re.fullmatch(r'[a-f0-9]{64}', owned[0].get('Id', '')):
                    raise RuntimeError('cannot establish ownership for PostgreSQL restore cleanup')
                podman('rm', '-f', '-v', owned[0]['Id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    package = commands.add_parser('package')
    package.add_argument('--capture', type=Path, required=True)
    package.add_argument('--destination', type=Path, required=True)
    package.add_argument('--metadata', type=Path, required=True)
    verify = commands.add_parser('verify-database')
    verify.add_argument('--payload', type=Path, required=True)
    verify.add_argument('--project', required=True)
    verify.add_argument('--revision', required=True)
    verify.add_argument('--pull', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.operation == 'package':
        if args.metadata.stat().st_size > MAX_MANIFEST:
            raise ValueError('image metadata exceeds its bound')
        metadata = json.loads(args.metadata.read_text(), object_pairs_hook=unique_object)
        print(package_capture(args.capture, args.destination, metadata))
    else:
        with restored_database(args.payload, args.project, args.revision, pull=args.pull) as restored:
            report = {'project': args.project, 'revision': args.revision, 'engine': restored['engine'],
                      'database_restore': 'passed', 'application_restore': 'not_run'}
        report['cleanup'] = 'passed'
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
