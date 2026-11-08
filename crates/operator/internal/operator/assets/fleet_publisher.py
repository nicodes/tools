"""Root-only, single-use publication of authenticated immutable OCI release packets.

The delivery receipt and policy are protected operator state, never request
fields. Product Actions must separately have Read package access. This program
does not claim that registry ACLs have been configured or that Root is isolated.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import time
import zipfile

spec = importlib.util.spec_from_file_location('protected_operator', Path(__file__).with_name('fleet_operator.py'))
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)

POLICY = Path('/etc/fleet-publisher.json')
STATE = Path('/var/lib/fleet-publisher')
DELIVERIES = Path('/var/lib/fleet-operator/deliveries')
MAX_ARCHIVE = 4 * 1024**3
MAX_EXPANDED = 16 * 1024**3
MAX_JSON = 65536
LIFETIME = 900
OWNER = 0
SHA256 = re.compile(r'[a-f0-9]{64}\Z')
IMAGE = re.compile(r'ghcr\.io/[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*\Z')


def dependency(name):
    """Load only an installed bundle member or this repository's canonical helper."""
    source = Path(__file__).resolve()
    path = source.with_name(name + '.py')
    if not path.is_file():
        # Native bundle tests execute the canonical source directly. Installed
        # executors have every dependency beside this file and never fall back
        # to a working checkout or an ambient Python import path.
        if len(source.parents) < 6:
            raise ValueError('publisher bundle dependency is missing')
        repository = source.parents[5]
        if source != repository / 'crates/operator/internal/operator/assets/fleet_publisher.py':
            raise ValueError('publisher bundle dependency is missing')
        path = repository / 'helpers' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_publisher_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry_api = dependency('oci_registry')
_prior_registry = sys.modules.get('oci_registry')
sys.modules['oci_registry'] = registry_api
try:
    packets = dependency('release_packet')
finally:
    if _prior_registry is None:
        del sys.modules['oci_registry']
    else:
        sys.modules['oci_registry'] = _prior_registry
oci = dependency('oci_archive')


def protected_directory(path):
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise ValueError('publisher path must be absolute and canonical')
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER or info.st_mode & 0o022:
            raise ValueError('publisher ancestry must be protected and root-owned')


@contextlib.contextmanager
def protected_file(path, maximum):
    protected_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER or info.st_mode & 0o077 or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
            raise ValueError('publisher input must be private, regular and bounded')
        yield stream


def private_json(path):
    with protected_file(path, MAX_JSON) as stream:
        return json.load(stream)


def digest(stream, deadline, maximum):
    value = hashlib.sha256()
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise ValueError('publisher validation exceeded its deadline')
        body = stream.read(65536)
        if not body:
            return value.hexdigest()
        total += len(body)
        if total > maximum:
            raise ValueError('publisher stream exceeds its bound')
        value.update(body)


def atomic(path, value):
    protected_directory(path.parent)
    if path.is_symlink():
        raise ValueError('linked publication state refused')
    descriptor, temporary = tempfile.mkstemp(prefix='.publisher-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(value, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def persist_delivery(value):
    """Retain the observed host grant for the separate publication boundary."""
    key = hashlib.sha256(value['repository'].encode()).hexdigest()
    directory = DELIVERIES/key
    # Never mkdir through an unchecked existing ancestor.
    missing = []
    parent = directory
    while not parent.exists():
        if parent.is_symlink():
            raise ValueError('linked delivery state refused')
        missing.append(parent)
        parent = parent.parent
    protected_directory(parent)
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
    protected_directory(directory)
    path = directory/(value['revision']+'.json')
    if path.exists() or path.is_symlink():
        if private_json(path) != value:
            raise ValueError('prior delivery differs; inspect without automatic renewal')
        return
    atomic(path, value)


def policy(path=POLICY):
    value = private_json(path)
    if set(value) != {'version', 'repositories'} or value['version'] != 1 or not isinstance(value['repositories'], dict):
        raise ValueError('unsupported publisher policy')
    for repository, contract in value['repositories'].items():
        if (not operator.REPOSITORY.fullmatch(repository)
                or not isinstance(contract, dict) or set(contract) != {'workflow', 'artifacts', 'username', 'credential'}
                or not isinstance(contract['workflow'], dict)
                or not isinstance(contract['artifacts'], list) or not 1 <= len(contract['artifacts']) <= 8
                or len(set(contract['artifacts'])) != len(contract['artifacts'])
                or any(not IMAGE.fullmatch(image) or image.split('/')[1] != repository.split('/')[0].lower()
                       for image in contract['artifacts'])
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', contract['username'])
                or not isinstance(contract['credential'], str) or not contract['credential'].startswith('/')
                or contract['workflow'].get('event') not in ('push', 'workflow_dispatch')
                or not 1 <= len(contract['workflow'].get('jobs', [])) <= 8):
            raise ValueError('publisher needs finite protected source, producer and artifact scopes')
        # Reuse the operator's complete producer contract validation.
        operator_contract = {'version': 3, 'repositories': {repository: {**contract['workflow'], 'event': 'pull_request'}}, 'hosts': []}
        operator.validate_policy(operator_contract)
    return value


def authenticate(request, contract, grant, run):
    repository, revision = request['repository'], request['revision']
    now = datetime.now(timezone.utc)
    issued, expires = (datetime.fromisoformat(grant[field]) for field in ('issued_at', 'expires_at'))
    if (grant.get('version') != 1 or grant.get('repository') != repository or grant.get('revision') != revision
            or grant.get('scope') != 'production-delivery' or grant.get('kind') not in ('merge', 'owner')
            or issued.tzinfo is None or expires.tzinfo is None or issued > now or expires <= now
            or not 0 < (expires-issued).total_seconds() <= 14400):
        raise ValueError('protected final delivery authorization missing, expired or different')
    commit = run(['gh', 'api', f'repos/{repository}/git/commits/{revision}'])
    parents = [parent['sha'] for parent in commit.get('parents', [])]
    expected = [grant['base'], grant['head']] if grant['kind'] == 'merge' else None
    if (commit.get('sha') != revision or commit.get('tree', {}).get('sha') != grant['tree']
            or expected is not None and parents != expected
            or expected is None and (not 1 <= len(parents) <= 2 or parents[0] != grant['base'] or grant['head'] != revision)):
        raise ValueError('actual final source parents or tree differ from protected delivery')
    current_main(repository, revision, run)
    checks = run(['gh', 'api', f'repos/{repository}/commits/{revision}/check-runs?per_page=100'])
    if checks.get('total_count', 0) >= 100:
        raise ValueError('publication check evidence exceeds its bound')
    rows = [{'__typename': 'CheckRun', 'name': row.get('name'), 'status': str(row.get('status', '')).upper(),
             'conclusion': str(row.get('conclusion', '')).upper(), 'detailsUrl': row.get('details_url')}
            for row in checks['check_runs']]
    if operator.authenticate_ci(repository, revision, contract['workflow'], rows, run, allow_running=True) != request['run_id']:
        raise ValueError('publication belongs to a different authenticated CI run')
    workflow = run(['gh', 'api', f"repos/{repository}/actions/runs/{request['run_id']}"])
    artifact = run(['gh', 'api', f"repos/{repository}/actions/artifacts/{request['artifact_id']}"])
    if (artifact.get('id') != request['artifact_id'] or artifact.get('expired')
            or artifact.get('name') != f"fleet-publication-{request['run_id']}-{workflow['run_attempt']}"
            or artifact.get('workflow_run', {}).get('id') != request['run_id']
            or artifact.get('workflow_run', {}).get('head_sha') != revision
            or not 0 < artifact.get('size_in_bytes', 0) <= MAX_ARCHIVE
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', artifact.get('digest', ''))):
        raise ValueError('publication artifact has no exact immutable producer identity')
    return workflow['run_attempt'], artifact['digest'].removeprefix('sha256:')


def current_main(repository, revision, run):
    if run(['gh', 'api', f'repos/{repository}/git/ref/heads/main'])['object']['sha'] != revision:
        raise ValueError('main advanced before publication')


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ValueError('publication deadline exceeded')
    return value


def prepare_artifact(stream, archive_sha, request, contract, grant, attempt, directory, deadline):
    """Admit every exact packet byte before reading registry authority."""
    if digest(stream, deadline, MAX_ARCHIVE) != archive_sha:
        raise ValueError('prepared artifact differs from authenticated GitHub digest')
    stream.seek(0)
    root = directory / 'packet'
    root.mkdir(mode=0o700)
    with zipfile.ZipFile(stream) as archive:
        entries = archive.infolist()
        names, expanded = set(), 0
        for entry in entries:
            remaining(deadline)
            name = entry.filename
            if (name in names or entry.is_dir() or entry.flag_bits & 1
                    or entry.file_size < 0 or entry.file_size > MAX_ARCHIVE
                    or stat.S_IFMT(entry.external_attr >> 16) not in {0, stat.S_IFREG}):
                raise ValueError('publication ZIP contains repeated, linked or unsupported members')
            names.add(name); expanded += entry.file_size
            if len(names) > packets.MAX_FILES + 2 or expanded > MAX_EXPANDED:
                raise ValueError('publication ZIP exceeds its member or expanded bound')
            if name not in {'publication-source.json', 'inventory.json'}:
                packets.member_type(name)
        if not {'publication-source.json','inventory.json','release.json'} <= names:
            raise ValueError('publication ZIP is missing its closed source and packet metadata')
        if not 0 < archive.getinfo('publication-source.json').file_size <= MAX_JSON:
            raise ValueError('publication source metadata exceeds its bound')
        source = json.loads(archive.read('publication-source.json'), object_pairs_hook=registry_api.unique_object)
        expected = {'version':1,'repository':request['repository'],'revision':request['revision'],
                    'tree':grant['tree'],'run_id':request['run_id'],'attempt':attempt}
        if source != expected or type(source.get('version')) is not int:
            raise ValueError('publication source differs from the authenticated final producer')
        for entry in entries:
            if entry.filename == 'publication-source.json': continue
            output = root / entry.filename
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(entry) as incoming, output.open('xb') as target:
                copied = 0
                while body := incoming.read(1 << 20):
                    remaining(deadline); copied += len(body)
                    if copied > entry.file_size:
                        raise ValueError('publication ZIP member exceeded its declared size')
                    target.write(body)
                if copied != entry.file_size:
                    raise ValueError('publication ZIP member is incomplete')
            output.chmod(0o600)
    packet = packets.Packet(root, timeout=max(1, min(600, int(remaining(deadline)))))
    descriptor = packet.descriptor
    if descriptor['source']['repository'] != request['repository'] or descriptor['source']['revision'] != request['revision']:
        raise ValueError('release descriptor differs from authenticated publication source')
    for name, artifact in descriptor['artifacts'].items():
        registry, repository, identity = registry_api.reference(artifact['reference'])
        if registry != 'ghcr.io' or registry + '/' + repository not in contract['artifacts']:
            raise ValueError('publication destination differs from protected finite policy')
        if artifact['kind'] == 'image':
            path = root / ('images/' + name + '.oci.tar')
            with oci.OCIArchive(path, artifact['reference'], artifact['config_digest'], timeout=min(600, remaining(deadline))) as image:
                metadata = image.metadata
                labels = metadata['config'].get('config', {}).get('Labels', {})
                if (metadata['manifest_digest'] != identity or metadata['platform'] != artifact['platform']
                        or labels.get('org.opencontainers.image.revision') != request['revision']
                        or labels.get('org.opencontainers.image.source') != 'https://github.com/' + request['repository']):
                    raise ValueError('OCI image differs from its accepted source, platform or manifest')
    packet.unchanged()
    return packet


def registry_login(contract):
    """Read protected authority only after source, producer and all bytes pass."""
    with protected_file(Path(contract['credential']), 4096) as stream:
        credential = stream.read(4097).strip()
    if not credential or any(value in credential for value in [b'\n',b'\r',b'\0']) or len(credential) > 4096:
        raise ValueError('registry credential has an unsupported shape')
    try:
        return contract['username'], credential.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('registry credential has an unsupported shape') from None


def registry(reference, username, password, deadline):
    host, repository, _ = registry_api.reference(reference)
    return registry_api.Registry(host, repository, username=username, password=password,
                                 timeout=max(1, min(LIFETIME, int(remaining(deadline)))))


def observe(client, artifact):
    """Reconcile by the already accepted digest; never publish or resolve a tag."""
    _, _, identity = registry_api.reference(artifact['reference'])
    status, headers, raw = client._request('GET', client._prefix + '/manifests/' + identity,
                                         {'Accept': registry_api.MANIFEST_TYPE})
    if status != 200 or registry_api.digest(raw) != identity or headers.get('docker-content-digest', identity) != identity:
        raise ValueError('registry identity remains ambiguous; no upload replayed')
    value = json.loads(raw, object_pairs_hook=registry_api.unique_object)
    if artifact['kind'] == 'image':
        if value.get('config', {}).get('digest') != artifact['config_digest']:
            raise ValueError('registry image configuration differs from the accepted release')
    elif raw != registry_api.artifact_manifest(artifact['digest'], artifact['size_bytes']):
        raise ValueError('registry static artifact differs from the accepted release')
    return {'manifest_digest':identity, 'config_digest':artifact.get('config_digest'),
            'payload_digest':artifact['digest'] if artifact['kind'] == 'archive' else None}


def execute(request, protected_policy, run=operator.command, transport=registry):
    if not isinstance(request, dict):
        raise ValueError('publisher request must be an object')
    if request.get('operation') in ('inspect', 'reconcile'):
        return inspect(request, protected_policy, transport)
    operator.fields(request, {'operation','repository','revision','run_id','artifact_id'})
    repository, revision = request['repository'], request['revision']
    if (request['operation'] != 'publish' or repository not in protected_policy['repositories']
            or not isinstance(revision,str) or not operator.SHA.fullmatch(revision)
            or any(type(request[field]) is not int or request[field] <= 0 for field in ('run_id','artifact_id'))):
        raise ValueError('publication request is outside protected scope')
    contract = protected_policy['repositories'][repository]
    key = hashlib.sha256(repository.encode()).hexdigest()
    deadline = time.monotonic() + LIFETIME
    original_run = run
    def bounded_run(arguments):
        remaining(deadline); result = original_run(arguments); remaining(deadline)
        return result
    run = bounded_run
    with operator.repository_lock(repository):
        protected_directory(STATE)
        grant = private_json(DELIVERIES/key/(revision+'.json'))
        journal = STATE/(key+'-'+revision+'.json')
        if journal.exists() or journal.is_symlink():
            raise ValueError('publication already claimed; inspect and reconcile without retry')
        attempt, archive_sha = authenticate(request,contract,grant,run)
        with tempfile.TemporaryDirectory(prefix='.publication-',dir=STATE) as directory:
            with protected_file(STATE/'artifacts'/(str(request['artifact_id'])+'.zip'),MAX_ARCHIVE) as stream:
                packet = prepare_artifact(stream,archive_sha,request,contract,grant,attempt,Path(directory),deadline)
            current_main(repository,revision,run)
            artifacts = packet.descriptor['artifacts']
            record = {'version':2,**request,'attempt':attempt,'archive_sha256':archive_sha,
                      'tree':grant['tree'],'release_digest':packet.release_digest,'artifacts':artifacts,
                      'status':'claimed','published':{}}
            atomic(journal,record)
            try:
                username,password = registry_login(contract)
                for name, artifact in artifacts.items():
                    current_main(repository,revision,run)
                    if datetime.fromisoformat(grant['expires_at']) <= datetime.now(timezone.utc):
                        raise ValueError('delivery expired before upload')
                    packet.unchanged()
                    reference = artifact['reference']
                    record['status']='uploading'; record['uploading']=reference; atomic(journal,record)
                    client = transport(reference,username,password,deadline)
                    if artifact['kind'] == 'image':
                        with oci.OCIArchive(packet.root/('images/'+name+'.oci.tar'),reference,artifact['config_digest'],
                                            timeout=min(600,remaining(deadline))) as image:
                            for blob in image.metadata['blobs']:
                                with image.open_blob(blob['digest']) as reader:
                                    client.upload_blob(blob['digest'],blob['size'],reader)
                            client.put_manifest(artifact['digest'],image.manifest_bytes)
                            image.unchanged()
                    else:
                        with packets.regular(packet.root/('static/'+name+'.tar.gz')) as (reader,_):
                            client.publish_static(artifact,reader)
                    packet.unchanged()
                    record['published'][reference]=observe(client,artifact); atomic(journal,record)
                record['status']='completed'; record.pop('uploading',None); atomic(journal,record)
                return record
            except BaseException:
                record['status']='ambiguous'; atomic(journal,record)
                raise


def inspect(request, protected_policy, transport=registry):
    operator.fields(request, {'operation','repository','revision'})
    repository, revision = request['repository'], request['revision']
    if repository not in protected_policy['repositories'] or not isinstance(revision,str) or not operator.SHA.fullmatch(revision):
        raise ValueError('publication inspection is outside protected scope')
    key = hashlib.sha256(repository.encode()).hexdigest()
    with operator.repository_lock(repository):
        journal = STATE/(key+'-'+revision+'.json')
        record = private_json(journal)
        if request['operation'] == 'inspect':
            if record['status'] not in ('completed','reconciled'):
                return {**record,'status':'ambiguous','persisted_status':record['status']}
            return record
        if record['repository'] != repository or record['revision'] != revision or record['status'] == 'completed':
            raise ValueError('only an incomplete exact publication can be reconciled')
        contract = protected_policy['repositories'][repository]
        for artifact in record['artifacts'].values():
            host, repo, _ = registry_api.reference(artifact['reference'])
            if host != 'ghcr.io' or host+'/'+repo not in contract['artifacts']:
                raise ValueError('retained destinations differ from protected policy')
        deadline = time.monotonic()+LIFETIME
        username,password = registry_login(contract)
        found = {}
        for artifact in record['artifacts'].values():
            reference=artifact['reference']
            found[reference]=observe(transport(reference,username,password,deadline),artifact)
        record['published']=found; record['status']='reconciled'; record.pop('uploading',None); atomic(journal,record)
        return record


def main():
    if os.geteuid() != 0:
        raise ValueError('publication requires the trusted root operator')
    request = json.loads(sys.stdin.buffer.read(MAX_JSON+1), object_pairs_hook=registry_api.unique_object)
    print(json.dumps(execute(request,policy()),sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError,OSError,KeyError,TypeError,zipfile.BadZipFile,tarfile.TarError,json.JSONDecodeError):
        print('publisher refused; inspect protected publication state',file=sys.stderr)
        raise SystemExit(1)
