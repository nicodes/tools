#!/usr/bin/env python3
"""Build and verify the shared PostgreSQL source runtime in an owned CI store."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'helpers'))
sys.path.insert(0, str(ROOT / 'tests'))
import container_runtime as runtime
import oci_archive
import image_security_report
import native_image_security_control as scanner_control
import postgres_runtime_contract as contract

LABEL = 'io.tools.postgres-native-fixture'
PROFILE = ROOT / 'tests/fixtures/apparmor-workload'
PROFILE_SHA256 = '06065f42a27508c46fef9f68f93a44351ab97555a60859f10829099880a5c799'
PROFILES = Path('/sys/kernel/security/apparmor/profiles')
APPARMOR = 'komizo-workload'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def module(path):
    spec = importlib.util.spec_from_file_location('postgres_constructor_consumer', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def source_archive(directory, revision, consumer):
    path = directory / 'tools-source.tar.gz'
    deadline = time.monotonic() + 120
    request = urllib.request.Request('https://codeload.github.com/nicodes/tools/tar.gz/' + revision)
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, path.open('xb') as output:
        os.fchmod(output.fileno(), 0o600)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('public source archive exceeded its download deadline')
            block = response.read(65536)
            if not block:
                break
            total += len(block)
            if total > 16 << 20:
                raise ValueError('public source archive exceeded its download bound')
            output.write(block)
    return consumer.verify_archive(path, revision)


class Native:
    def __init__(self, environment, token):
        self.environment = environment
        self.token = token
        self.image = None
        self.intents = {}

    def run(self, arguments, **kwargs):
        arguments = list(arguments)
        if arguments[0] in {'run', 'create'}:
            names = [arguments[i + 1] for i, arg in enumerate(arguments[:-1]) if arg == '--name']
            if len(names) > 1:
                raise ValueError('native fixture container name is ambiguous')
            name = names[0] if names else 'tools-pg-control-' + secrets.token_hex(16)
            if not names:
                arguments[1:1] = ['--name', name]
            arguments[1:1] = ['--label', LABEL + '=' + self.token,
                              '--security-opt=apparmor=' + APPARMOR]
            if name not in self.intents:
                if self.exists('container', name):
                    raise ValueError('native container intent is not fresh')
                self.intents[name] = self.image
        kwargs.setdefault('timeout', 60)
        return runtime.run(arguments, environment=self.environment, **kwargs)

    def execute(self, arguments, *, body=None, check=True, timeout=60):
        result = self.run(arguments, input=body, check=False, capture_output=True, timeout=timeout)
        if len(result.stdout) > 64 << 20 or len(result.stderr) > (64 << 20 if arguments[0] == 'build' else 1 << 20):
            raise ValueError('native PostgreSQL proof output exceeded its bound')
        if check and result.returncode:
            raise ValueError('native PostgreSQL command failed: ' + arguments[0]
                             + ' exit=' + str(result.returncode))
        return result

    def exists(self, kind, name):
        result = runtime.run([kind, 'exists', name], environment=self.environment,
                             capture_output=True, timeout=30)
        if result.returncode not in {0, 1}:
            raise ValueError('native object presence is uncertain')
        return result.returncode == 0

    def inspect_images(self, references):
        return runtime.inspect_images(references, environment=self.environment)

    def bind_mount_user_namespace(self, uid, gid):
        return runtime.bind_mount_user_namespace(uid, gid)

    def owned(self, name):
        if name not in self.intents:
            raise ValueError('container has no owned fixture intent')
        value = json.loads(self.execute(['container', 'inspect', name]).stdout)[0]
        if (value['Name'].lstrip('/') != name or value['Config']['Labels'].get(LABEL) != self.token
                or runtime.config_digest(value['Image']) != self.intents[name]
                or not re.fullmatch('[a-f0-9]{64}', value['Id'])):
            raise ValueError('native PostgreSQL fixture ownership changed')
        return value

    def cleanup(self):
        for name in reversed(self.intents):
            if self.exists('container', name):
                value = self.owned(name)
                self.execute(['rm', '--force', value['Id']], timeout=60)
                if self.exists('container', name):
                    raise ValueError('owned native PostgreSQL fixture survived cleanup')


def scan(native, directory, image, reference, revision):
    prepared = directory / 'scanner'
    scanner_control.setup(prepared)
    scanner = scanner_control.scanner_identity()
    database = prepared / 'cache/db/trivy.db'
    database_digest = digest(database)
    archive = directory / 'image.oci.tar'
    metadata = oci_archive.save_image(reference, archive, environment=native.environment)
    before = native.inspect_images([image])[0]
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with oci_archive.OCIArchive(archive, reference, image) as admitted:
        layout = admitted.materialize(directory / 'scan-layout')
        base = ['run', '--rm', '--pull=never', '--network=none', '--read-only',
                '--read-only-tmpfs=false', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--security-opt=apparmor=' + APPARMOR, '--memory=768m', '--memory-swap=768m',
                '--cpus=1', '--pids-limit=128', '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=32m',
                '-v', str(layout) + ':/inputs/image.oci:ro',
                '-v', str(prepared / 'cache') + ':/cache', '-v', str(directory) + ':/reports',
                scanner['reference'], 'image', '--quiet', '--skip-version-check',
                '--input', '/inputs/image.oci', '--cache-dir', '/cache', '--skip-db-update',
                '--skip-java-db-update', '--offline-scan', '--timeout', '120s']
        # Scanner identity is separate from the subject and has no PG cleanup intent.
        for tail in [['--scanners', 'vuln,secret', '--format', 'json', '--output', '/reports/security.json'],
                     ['--format', 'cyclonedx', '--output', '/reports/sbom.json']]:
            runtime.run([*base, *tail], environment=native.environment,
                        check=True, capture_output=True, timeout=180)
        admitted.unchanged()
    after = native.inspect_images([image])[0]
    context = {'reference': reference, 'source': revision, 'scanner': scanner,
               'started_at': started, 'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'before': before, 'after': after,
               'archive': {'sha256': metadata['archive_sha256'], 'target': '/inputs/image.oci'}}
    report = json.loads((directory / 'security.json').read_bytes())
    sbom = json.loads((directory / 'sbom.json').read_bytes())
    (directory / 'scan-context.json').write_text(json.dumps({'version': 1, 'context': context,
        'security_sha256': digest(directory / 'security.json'),
        'sbom_sha256': digest(directory / 'sbom.json')}, sort_keys=True) + '\n')
    if digest(database) != database_digest:
        raise ValueError('offline native scan changed the prepared database')
    findings = image_security_report.image_scan_findings(report, sbom, context=context, input_archive=archive)
    if findings:
        raise ValueError('native PostgreSQL image has blocking vulnerabilities or detected secrets')
    return {'config_digest': image, 'manifest_digest': metadata['manifest_digest'],
            'archive_sha256': metadata['archive_sha256'], 'scanner': scanner, 'blocking_findings': 0}


def startup(native, directory):
    name = 'tools-pg-server-' + native.token
    auth = directory / 'postgres.env'
    auth.write_text('POSTGRES_USER=tools_owner\nPOSTGRES_DB=tools_fixture\nPOSTGRES_PASSWORD='
                    + secrets.token_hex(32) + '\n')
    auth.chmod(0o600)
    native.execute(['run', '--detach', '--name', name, '--pull=never', '--network=none',
        '--read-only', '--read-only-tmpfs=false', '--image-volume=ignore', '--http-proxy=false',
        '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
        '--security-opt=no-new-privileges', '--memory=256m', '--memory-swap=256m',
        '--cpus=0.5', '--pids-limit=128', '--timeout=300', '--env-file', str(auth),
        '--tmpfs=/run/postgresql:rw,nosuid,nodev,noexec,size=16m,mode=1777',
        '--tmpfs=/var/lib/postgresql:rw,nosuid,nodev,noexec,size=96m,mode=755',
        '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777', native.image,
        'postgres', '-c', 'shared_buffers=16MB', '-c', 'work_mem=1MB',
        '-c', 'maintenance_work_mem=16MB', '-c', 'max_connections=10'])
    deadline = time.monotonic() + 90
    while native.execute(['exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'tools_owner',
                          '-d', 'tools_fixture'], check=False, timeout=5).returncode:
        if not native.owned(name)['State']['Running'] or time.monotonic() >= deadline:
            raise ValueError('bounded native PostgreSQL startup did not become ready')
        time.sleep(.1)
    value = native.owned(name)
    config = value['HostConfig']
    if (config.get('ReadonlyRootfs') is not True or config.get('NetworkMode') != 'none'
            or set(config.get('Tmpfs', {})) != {'/run/postgresql', '/var/lib/postgresql', '/tmp'}
            or any('noexec' not in options.split(',') or 'nosuid' not in options.split(',')
                   for options in config['Tmpfs'].values())):
        raise ValueError('actual PostgreSQL read-only/network/tmpfs boundary changed')
    pid = value['State']['Pid']
    status = dict(row.split(':', 1) for row in Path('/proc', str(pid), 'status').read_text().splitlines() if ':' in row)
    if (set(map(int, status['Uid'].split())) != {999} or set(map(int, status['Gid'].split())) != {999}
            or any(int(status[k].strip(), 16) for k in ['CapInh', 'CapPrm', 'CapEff', 'CapAmb'])
            or status['NoNewPrivs'].strip() != '1'):
        raise ValueError('actual PostgreSQL writer did not retain its nonroot/NNP/capability boundary')
    profile = Path('/proc', str(pid), 'attr/current').read_text().strip()
    if profile not in {'komizo-workload (enforce)', 'crun//&komizo-workload (mixed)',
                       'komizo-workload//&crun (mixed)'}:
        raise ValueError('actual PostgreSQL writer lost the enforcing reviewed profile')
    rows = Path('/proc', str(pid), 'cgroup').read_text().splitlines()
    if len(rows) != 1 or not rows[0].startswith('0::/'):
        raise ValueError('native PostgreSQL kernel group is ambiguous')
    group = Path('/sys/fs/cgroup') / rows[0][4:]
    if ((group / 'memory.max').read_text().strip() != str(256 << 20)
            or (group / 'memory.swap.max').read_text().strip() != '0'
            or (group / 'pids.max').read_text().strip() != '128'):
        raise ValueError('actual PostgreSQL memory/swap/PID limits changed')
    quota, period = map(int, (group / 'cpu.max').read_text().split())
    if quota * 1000 != period * 500:
        raise ValueError('actual PostgreSQL CPU limit changed')
    if native.execute(['exec', name, 'readlink', '-f', '/var/run/postgresql']).stdout.strip() != b'/run/postgresql':
        raise ValueError('frozen entrypoint socket alias did not reach the canonical tmpfs')

    def sql(database, query, role='tools_owner', check=True):
        return native.execute(['exec', '-i', name, 'psql', '-XAtq', '-v', 'ON_ERROR_STOP=1',
            '-U', role, '-d', database], body=query.encode(), check=check, timeout=30)

    actual = sql('tools_fixture', "SELECT current_setting('server_version_num'),datlocprovider,datcollate,datctype,datcollversion,pg_database_collation_actual_version(oid) FROM pg_database WHERE datname=current_database();").stdout.strip()
    if actual != b'180006|c|en_US.utf8|en_US.utf8|2.41|2.41':
        raise ValueError('actual engine/default client/locale contract changed')
    sql('tools_fixture', 'CREATE ROLE tools_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;')
    native.execute(['exec', name, 'createdb', '-U', 'tools_owner', '-T', 'template0', 'tools_source'])
    sql('tools_source', "CREATE TABLE rows(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,value text NOT NULL); INSERT INTO rows(value) VALUES('alpha'),('beta'),('gamma'); GRANT SELECT ON rows TO tools_reader;")
    native.execute(['exec', name, 'pg_dump', '-U', 'tools_owner', '-Fc', '-d', 'tools_source', '-f', '/tmp/native.dump'])
    native.execute(['exec', name, 'createdb', '-U', 'tools_owner', '-T', 'template0', 'tools_restored'])
    native.execute(['exec', name, 'pg_restore', '--exit-on-error', '--single-transaction', '-U', 'tools_owner', '-d', 'tools_restored', '/tmp/native.dump'])
    restored = sql('tools_restored', "SELECT (SELECT string_agg(id::text||':'||value,'|' ORDER BY id) FROM rows),nextval(pg_get_serial_sequence('rows','id')),has_table_privilege('tools_reader','rows','SELECT'),has_table_privilege('tools_reader','rows','INSERT');").stdout.strip()
    if restored != b'1:alpha|2:beta|3:gamma|4|t|f' or sql('tools_restored', 'SELECT count(*) FROM rows;', 'tools_reader').stdout.strip() != b'3':
        raise ValueError('native dump/restore changed rows, identity sequence or read-only ACL')
    denied = sql('tools_restored', "INSERT INTO rows(value) VALUES('forbidden');", 'tools_reader', check=False)
    if denied.returncode != 3 or b'permission denied' not in denied.stderr:
        raise ValueError('restricted restored role unexpectedly gained write authority')
    native.owned(name)
    return {'server_version_num': 180006, 'uid': 999, 'gid': 999, 'locale': 'en_US.utf8',
            'collation_version': '2.41', 'default_socket_clients': True, 'restored_rows': 3,
            'next_identity': 4, 'reader_select': True, 'reader_insert_refused': True,
            'apparmor': profile, 'memory_bytes': 256 << 20, 'swap_bytes': 0, 'cpu_millis': 500, 'pids': 128}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', args.revision):
        raise ValueError('native proof requires an owned privileged disposable CI scope and exact source')
    expected = subprocess.check_output(['git', '--no-replace-objects', '-c', 'safe.directory=' + str(ROOT),
        '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if expected != args.revision:
        raise ValueError('native proof source differs from the actual checkout')
    consumer = module(ROOT / 'postgres-runtime/consumer.py')
    consumer.verify()
    directory = args.directory.absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    storage = directory / 'storage.conf'
    storage.write_text('[storage]\ndriver="vfs"\nrunroot="' + str(directory / 'runroot')
                       + '"\ngraphroot="' + str(directory / 'graphroot') + '"\n')
    storage.chmod(0o600)
    containers = directory / 'containers.conf'
    containers.write_text('[containers]\napparmor_profile="komizo-workload"\n')
    containers.chmod(0o600)
    environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': '/root', 'LANG': 'C.UTF-8', 'CONTAINERS_STORAGE_CONF': str(storage),
        'CONTAINERS_CONF': str(containers)}
    os.environ.clear()
    os.environ.update(environment)
    token = secrets.token_hex(16)
    native = Native(environment, token)
    loaded = False
    foreign = None
    output = ROOT / '.artifacts/postgres-runtime-proof'
    output.mkdir(parents=True, exist_ok=True)
    profile_input = directory / 'apparmor-workload'
    try:
        archive_sha256 = source_archive(directory, args.revision, consumer)
        wrapper = directory / 'Containerfile'
        wrapper.write_text(consumer.render(args.revision, archive_sha256))
        if (digest(PROFILE) != PROFILE_SHA256 or any(row.startswith(APPARMOR + ' ')
                for row in PROFILES.read_text().splitlines())):
            raise ValueError('native proof refuses changed or already installed profile state')
        profile_input.write_bytes(PROFILE.read_bytes())
        profile_input.chmod(0o600)
        if digest(profile_input) != PROFILE_SHA256:
            raise ValueError('reviewed profile changed while entering the private fixture')
        subprocess.run(['/usr/sbin/apparmor_parser', '-r', str(profile_input)], check=True, timeout=30,
                       capture_output=True, env=environment)
        loaded = True
        if APPARMOR + ' (enforce)' not in PROFILES.read_text().splitlines():
            raise ValueError('reviewed native profile is not enforcing')
        reference = 'localhost/tools-postgres-runtime:' + token
        native.execute(['build', '--force-rm', '--label', LABEL + '=' + token,
            '--build-arg', 'SOURCE_REVISION=' + args.revision, '-f', str(wrapper),
            '-t', reference, str(ROOT)], timeout=1800)
        inspected = native.inspect_images([reference])[0]
        if inspected['Config'].get('Labels', {}).get(LABEL) != token:
            raise ValueError('constructor omitted its exact fixture ownership')
        labels = inspected['Config']['Labels']
        if (labels.get('io.nicodes.tools.postgres.build_revision') != args.revision
                or labels.get('io.nicodes.tools.postgres.source_archive_sha256') != 'sha256:' + archive_sha256):
            raise ValueError('consumer omitted the immutable source/archive identities')
        native.image = inspected['Id']
        subject = native.image
        foreign_name = 'tools-pg-preservation-' + token
        foreign_token = secrets.token_hex(16)
        runtime.run(['create', '--name', foreign_name, '--pull=never', '--network=none',
            '--label', LABEL + '=' + foreign_token, '--image-volume=ignore',
            '--entrypoint=/bin/sleep', subject, '1'], environment=environment,
            check=True, capture_output=True, timeout=30)
        foreign = json.loads(native.execute(['container', 'inspect', foreign_name]).stdout)[0]
        native.intents[foreign_name] = subject
        try:
            try:
                native.owned(foreign_name)
            except ValueError:
                pass
            else:
                raise ValueError('foreign-label container was accepted for native cleanup')
        finally:
            native.intents.pop(foreign_name)
        # Keep source-reference evidence while using immutable configs for all execution.
        scan_result = scan(native, directory, subject, reference, args.revision)
        contract.RUNTIME = native
        contract.image_contract(subject)
        contract.old_data_bind_refusal(subject)
        contract.verify(subject)
        behavior = startup(native, directory)
        native.cleanup()
        after = json.loads(native.execute(['container', 'inspect', foreign_name]).stdout)[0]
        if any(after[key] != foreign[key] for key in ['Id', 'Image', 'Name', 'State']):
            raise ValueError('native cleanup changed an unrelated stopped container')
        native.execute(['rm', foreign['Id']])
        foreign = None
        native.execute(['rmi', reference], timeout=60)
        result = {'kind': 'tools.postgres-runtime-proof', 'version': 1, 'source_revision': args.revision,
                  'constructor_inventory_sha256': digest(ROOT / 'postgres-runtime/bundle.json'),
                  'build_source_archive_sha256': 'sha256:' + archive_sha256,
                  'downloaded_constructor_matches_tested_checkout': True,
                  'scan': scan_result, 'runtime': behavior, 'owned_container_cleanup': True,
                  'foreign_label_refused': True, 'unrelated_stopped_container_preserved': True,
                  'production_data': False}
        (output / 'proof.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
        print('Native source-built PostgreSQL: strict scanner clean, exact provenance/ELF/launcher/old-bind contracts, default clients, rows/identity/ACL restore and owned cleanup passed.')
    finally:
        for name in ['security.json', 'sbom.json', 'scan-context.json']:
            if (directory / name).is_file():
                shutil.copyfile(directory / name, output / name)
        native.cleanup()
        if foreign is not None:
            current = json.loads(native.execute(['container', 'inspect', foreign['Id']]).stdout)[0]
            if (current['Id'] != foreign['Id'] or current['Image'] != foreign['Image']
                    or current['Config']['Labels'].get(LABEL) != foreign['Config']['Labels'][LABEL]
                    or current['State']['Running']):
                raise ValueError('preservation control changed before explicit owned cleanup')
            native.execute(['rm', current['Id']])
        remaining = native.execute(['ps', '--all', '--format=json']).stdout
        if json.loads(remaining):
            raise ValueError('native proof retained unexpected container state')
        if loaded:
            if digest(profile_input) != PROFILE_SHA256:
                raise ValueError('owned fixture profile bytes changed before removal')
            subprocess.run(['/usr/sbin/apparmor_parser', '-R', str(profile_input)], check=True, timeout=30,
                           capture_output=True, env=environment)


if __name__ == '__main__':
    main()
