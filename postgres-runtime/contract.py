"""Actual minimal-runtime image, launcher and old-layout refusal contracts."""
import hashlib
import struct
import json
import os
import re
import sys
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import shlex
import stat
from contextlib import contextmanager

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = None


def native():
    if RUNTIME is None:
        raise ValueError('prepare the explicit owned native fixture first')
    return RUNTIME


def run(command, *, input=None):
    if command[:2] != ['podman', '--remote=false']:
        raise ValueError('launcher proof requires the fixed local native runtime')
    return native().run(command[2:], input=input, capture_output=True, text=True, timeout=30)


def verify(image, evidence=None):
    if not isinstance(image, str) or not re.fullmatch('sha256:[a-f0-9]{64}', image):
        raise ValueError('launcher proof requires the accepted immutable config ID')
    native().run(['info','--format=json'],check=True,capture_output=True,timeout=30)
    inspected = run(['podman', '--remote=false', 'image', 'inspect', '--format', '{{.Id}}', image])
    image_id = native().config_digest(inspected.stdout.strip())
    if inspected.returncode or image_id != image:
        raise RuntimeError('launcher artifact differs from the accepted image config')
    artifacts = Path(evidence) if evidence is not None else Path.cwd() / '.artifacts/postgres-runtime-proof'
    artifacts.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='su-exec-image-', dir=artifacts) as name:
        directory = Path(name)
        passwd = directory / 'passwd'
        group = directory / 'group'
        normal_passwd = 'root:x:0:0:root:/root:/bin/sh\nsample:x:123:456:Example:/tmp:/bin/false\n'
        normal_group = 'root:x:0:\nsample:x:456:\nother:x:789:sample\n'

        def accounts(user_text=normal_passwd, group_text=normal_group):
            passwd.write_text(user_text)
            group.write_text(group_text)
            passwd.chmod(0o644)
            group.chmod(0o644)

        ownership = uuid.uuid4().hex
        base = ['podman', '--remote=false', 'run', '--pull=never', '--network=none', '--read-only',
                '--read-only-tmpfs=false', '--label', 'io.tools.su-exec-proof='+ownership,
                '--memory=32m', '--pids-limit=32', '--timeout=30', '--user', '0:0', '--cap-drop=ALL',
                '--cap-add=SETUID', '--cap-add=SETGID', '--security-opt=no-new-privileges',
                '--entrypoint', '/usr/local/bin/su-exec',
                '-v', f'{passwd}:/etc/passwd:ro', '-v', f'{group}:/etc/group:ro']
        accounts()
        command = 'printf "%s:%s:%s:%s:%s\\n" "$(id -u)" "$(id -g)" "$HOME" "$(id -G)" "$$"'
        for case, target, expected in [
                ('named-user-and-supplementary-groups', 'sample', '123:456:/tmp:456 789:1'),
                ('explicit-group', 'sample:other', '123:789:/tmp:789:1'),
                ('numeric-user-and-group', '123:456', '123:456:/tmp:456:1'),
                ('numeric-user-without-account', '124:457', '124:457:/:457:1')]:
            result = run([*base, '--rm', image_id, target, '/bin/sh', '-c', command])
            assert result.returncode == 0 and result.stdout.strip() == expected, (case, result.stdout, result.stderr)
            results.append({'case': case, 'passed': True})
        result = run([*base, '--rm', image_id, 'sample', '/bin/sh', '-c',
                      'printf child-output; printf child-error >&2; exit 73'])
        assert (result.returncode, result.stdout, result.stderr) == (73, 'child-output', 'child-error')
        results.append({'case': 'streams-and-child-exit-status', 'passed': True})
        for case in ('oversized-passwd', 'oversized-group'):
            # OCI must resolve the original normal accounts before the actual
            # launcher guard sees oversized bytes. Mutate only our private bind
            # after PID 1 started; no second OCI exec/account parser intervenes.
            accounts()
            account = 'passwd' if case == 'oversized-passwd' else 'group'
            path = passwd if account == 'passwd' else group
            launch = list(base)
            launch[launch.index('--entrypoint') + 1] = '/bin/sh'
            launch[launch.index(f'{path}:/etc/{account}:ro')] = f'{path}:/etc/{account}:rw'
            contents = ('sample:x:123:456:' + 'a' * (128 << 10) if account == 'passwd'
                        else 'sample:x:456:' + 'a' * (2 << 20))
            script = ('printf "fixture-initial-uid=%s\\n" "$(id -u)"; cat > /etc/' + account
                      + '; exec /usr/local/bin/su-exec sample /bin/sh -c "echo unexpected-child"')
            result = run([*launch, '--interactive', '--rm', image_id, '-euc', script], input=contents)
            diagnostic = (case, result.returncode, result.stdout[-4096:], result.stderr[-4096:])
            assert result.returncode != 0 and result.stdout == 'fixture-initial-uid=0\n', diagnostic
            assert 'account record exceeds limit' in result.stderr, diagnostic
            results.append({'case': case, 'passed': True})
        accounts()
        container = 'tools-su-exec-signal-' + uuid.uuid4().hex
        container_id = None
        try:
            started = run([*base, '--detach', '--name', container, image_id, 'sample', '/bin/sh', '-c',
                           'trap "exit 37" TERM; echo ready; while :; do sleep 0.1; done'])
            assert started.returncode == 0, started.stderr
            container_id = started.stdout.strip()
            assert re.fullmatch('[a-f0-9]{64}', container_id), 'launcher fixture omitted its full native identity'
            for _ in range(50):
                if 'ready' in run(['podman', '--remote=false', 'logs', container]).stdout:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('signal fixture did not become ready')
            killed = run(['podman', '--remote=false', 'kill', '--signal', 'TERM', container])
            assert killed.returncode == 0, killed.stderr
            waited = run(['podman', '--remote=false', 'wait', container])
            assert waited.returncode == 0 and waited.stdout.strip() == '37', 'SIGTERM did not reach PID 1'
            results.append({'case': 'pid1-sigterm', 'passed': True})
        finally:
            if container_id:
                inspected = run(['podman', '--remote=false', 'container', 'inspect', container_id])
                values = json.loads(inspected.stdout)
                assert inspected.returncode == 0 and len(values) == 1 and values[0]['Id'] == container_id
                assert values[0]['Config']['Labels'].get('io.tools.su-exec-proof') == ownership, 'launcher fixture ownership changed'
                removed = run(['podman', '--remote=false', 'rm', '--force', container_id])
                assert removed.returncode == 0, 'owned launcher fixture cleanup failed'
    receipt = {'image': image_id, 'launcher': 'su-exec@0.3', 'cases': results,
               'host_accounts_changed': False, 'network': 'none'}
    (artifacts / 'su-exec-image.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('Shipped su-exec preserved user/group, HOME, PID 1, streams and signal behavior; oversized records refused.')

def wrapper_input(wrapper):
    """Admit one explicit caller-owned entrypoint without changing the runtime contract."""
    if wrapper is None:
        return '/usr/local/bin/docker-entrypoint.sh', None
    if (not isinstance(wrapper, dict) or set(wrapper) != {'path', 'source'}
            or not isinstance(wrapper['path'], str)
            or not re.fullmatch(r'/usr/local/bin/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', wrapper['path'])
            or wrapper['path'] == '/usr/local/bin/docker-entrypoint.sh'):
        raise ValueError('entrypoint wrapper requires its exact installed path and source')
    source = Path(wrapper['source'])
    if any(parent.is_symlink() for parent in (source, *source.parents)):
        raise ValueError('entrypoint wrapper source contains a link')
    before = source.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or not 0 < before.st_size <= 65536):
        raise ValueError('entrypoint wrapper source must be one bounded ordinary file')
    raw = source.read_bytes()
    after = source.lstat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                              info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if signature(before) != signature(after) or len(raw) != before.st_size or not raw.startswith(b'#!'):
        raise ValueError('entrypoint wrapper source changed or is not an executable script')
    return wrapper['path'], raw


def verify_wrapper(path, expected, read_file, command):
    if expected is not None:
        if read_file(path) != expected or command('stat -c %u:%g:%a:%h ' + path).strip() != '0:0:555:1':
            raise ValueError('installed entrypoint wrapper differs from its exact source or protected mode')


def payload_path(path):
    # GNU coreutils ships the ordinary '[' executable. Fixed argv reads and
    # shlex.quote for the shell loader check preserve its literal filename.
    return (isinstance(path, str) and len(path) <= 4096
            and re.fullmatch(r'/[A-Za-z0-9_./+\[\]-]+', path) is not None
            and '..' not in Path(path).parts)


def reader_identity(value, image, name, token, expected_id=None):
    if (not isinstance(value, dict) or not re.fullmatch('[a-f0-9]{64}', str(value.get('Id', '')))
            or (expected_id is not None and value['Id'] != expected_id)
            or value.get('Name', '').lstrip('/') != name
            or native().config_digest(value.get('Image')) != image
            or value.get('Config', {}).get('Labels', {}).get('io.tools.pg-image-contract') != token):
        raise ValueError('runtime image reader ownership or immutable image changed')
    return value['Id']


@contextmanager
def image_reader(image):
    """Use one bounded immutable reader; repeated execs avoid copying vfs layers."""
    token = uuid.uuid4().hex
    name = 'tools-pg-image-' + token
    container_id = None

    def inspect():
        result = native().run(['container', 'inspect', name], check=True, capture_output=True, timeout=30)
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError('runtime image reader inspection is ambiguous')
        reader_identity(values[0], image, name, token, container_id)
        return values[0]

    def execute(reference, arguments):
        if reference != image or inspect()['State']['Running'] is not True:
            raise ValueError('runtime image reader subject changed or stopped')
        return native().run(['exec', container_id, *arguments], check=True, capture_output=True, timeout=30)

    try:
        started = native().run(['run', '--detach', '--name', name, '--pull=never', '--network=none',
            '--read-only', '--read-only-tmpfs=false', '--image-volume=ignore', '--http-proxy=false',
            '--memory=32m', '--memory-swap=32m', '--cpus=0.5', '--pids-limit=32', '--timeout=600',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--label',
            'io.tools.pg-image-contract=' + token, '--entrypoint=/bin/sleep', image, '600'],
            check=True, capture_output=True, timeout=30)
        container_id = started.stdout.decode().strip() if isinstance(started.stdout, bytes) else started.stdout.strip()
        if not re.fullmatch('[a-f0-9]{64}', container_id):
            raise ValueError('runtime image reader has no exact native ID')
        inspect()
        yield (lambda reference, path: execute(reference, ['cat', path]).stdout,
               lambda reference, text: execute(reference, ['/bin/sh', '-euc', text]).stdout.decode())
    finally:
        present = native().run(['container', 'exists', name], capture_output=True, timeout=30)
        if present.returncode == 0:
            value = inspect()
            native().run(['rm', '--force', value['Id']], check=True, capture_output=True, timeout=30)
            if native().run(['container', 'exists', name], capture_output=True, timeout=30).returncode != 1:
                raise ValueError('owned runtime image reader survived cleanup')
        elif present.returncode != 1:
            raise ValueError('runtime image reader cleanup presence is uncertain')


def image_contract(image, *, wrapper=None):
    entrypoint, wrapper_bytes = wrapper_input(wrapper)
    pin = json.loads((ROOT/'postgres-runtime/runtime/PIN.json').read_text())
    base_reference = pin['runtime_base']
    native().run(['pull', base_reference], check=True, capture_output=True, timeout=300)
    base, actual = native().inspect_images([base_reference, image])
    original = base['RootFS']['Layers']
    assert original and actual['RootFS']['Type'] == base['RootFS']['Type']
    assert actual['RootFS']['Layers'][:len(original)] == original, 'The selected immutable minimal base changed'
    assert len(actual['RootFS']['Layers']) > len(original), 'Expected explicit source-owned runtime layers'
    authored = {'Labels','Env','User','WorkingDir','Entrypoint','Cmd','StopSignal','ExposedPorts'}
    assert {k:v for k,v in base['Config'].items() if k not in authored} == {
        k:v for k,v in actual['Config'].items() if k not in authored}, 'Undeclared minimal base configuration changed'
    assert actual['Config']['User'] == '0:0'
    assert actual['Config']['WorkingDir'] == '/', 'The PostgreSQL runtime requires its accessible declared working directory'
    assert actual['Config']['Entrypoint'] == [entrypoint], 'The declared final entrypoint changed'
    assert actual['Config']['Cmd'] == ['postgres'] and actual['Config']['StopSignal'] == 'SIGINT'
    assert actual['Config']['ExposedPorts'] == {'5432/tcp':{}}
    environment = lambda config: dict(item.split('=',1) for item in config.get('Env',[]))
    assert environment(actual['Config']) == environment(base['Config']) | {
        'PG_MAJOR':'18','PG_VERSION':'18.6','PGDATA':'/var/lib/postgresql/18/docker','LANG':'en_US.utf8'}

    with image_reader(image) as (inspect_file, command):
        upstream = (ROOT/'postgres-runtime/runtime/entrypoint/docker-entrypoint.sh').read_bytes()
        assert hashlib.sha256(upstream).hexdigest()==pin['entrypoint_source']['files_sha256']['docker-entrypoint.sh']
        assert upstream.count(b'exec gosu postgres ') == 1
        assert inspect_file(image,'/usr/local/bin/docker-entrypoint.sh') == upstream.replace(
            b'exec gosu postgres ',b'exec su-exec postgres '), 'Entrypoint changed beyond the reviewed launcher replacement'
        verify_wrapper(entrypoint, wrapper_bytes, lambda path: inspect_file(image, path),
                       lambda text: command(image, text))
        runtime = json.loads(inspect_file(image,'/usr/local/share/komizo-postgres-runtime.json'))
        assert runtime['kind']=='komizo.postgres-runtime' and runtime['version']==1 and runtime['pin']==pin
        packages = {item['name']:item for item in runtime['runtime_packages']}
        assert set(packages)==set(pin['runtime_packages'])
        for name, record in packages.items():
            control=inspect_file(image,'/var/lib/dpkg/status.d/'+name).decode()
            assert 'Package: '+name+'\n' in control and 'Version: '+record['version']+'\n' in control
            assert re.fullmatch('[a-f0-9]{64}',record['sha256'])
        components={item['name']:item for item in runtime['components']}
        assert components['postgresql']=={'name':'postgresql',**pin['postgresql']}
        for name,version in pin['debian_sources'].items():
            assert components[name]['debian_source_version']==version
            assert any(path.endswith('.dsc') for path in components[name]['debian_source_archives_sha256'])
            control=inspect_file(image,'/var/lib/dpkg/status.d/'+name).decode()
            assert 'Version: '+version+'+komizo1\n' in control
        assert set(pin['postgresql']['configure']) <= set(shlex.split(command(image,'pg_config --configure')))
        sample = inspect_file(image, '/usr/local/share/postgresql/postgresql.conf.sample')
        sample_patch = next(patch for patch in pin['postgresql']['source_patches']
                            if patch['path'] == 'src/backend/utils/misc/postgresql.conf.sample')
        assert hashlib.sha256(sample).hexdigest() == sample_patch['sha256_after'], 'Installed interface default differs from the declared source patch'
        assert command(image,'postgres --version').strip()=='postgres (PostgreSQL) 18.6'
        assert command(image,'pg_config --version').strip()=='PostgreSQL 18.6'
        assert command(image,'readlink /var/run').strip()=='/run', 'The frozen entrypoint socket path lost its canonical alias'
        required={'postgres','initdb','pg_ctl','pg_isready','psql','pg_dump','pg_dumpall','pg_restore',
                  'pg_config','createdb','dropdb','createuser','dropuser','bash','ls','cp','id',
                  'sleep','env','stat','getconf','getent','locale','find','grep','awk','gzip','gunzip','xzcat','zstd'}
        assert set(runtime['executables'])==required
        for name,record in runtime['executables'].items():
            path=record['path']
            assert path in ['/usr/local/bin/'+name,'/usr/bin/'+name]
            actual_bytes=inspect_file(image,path)
            assert hashlib.sha256(actual_bytes).hexdigest()==record['sha256']
            if record['kind']=='elf':
                assert actual_bytes[:4]==b'\x7fELF'
                linked=command(image,'ldd '+path)
                assert not any(part in linked for part in ['libxml','libxslt','libsystemd','libudev','libtinfo',
                    'libncurses','libreadline','libacl','libldap','libgssapi','libicu','libsqlite','libcap','libgmp','not found'])
            else:
                assert record['kind']=='script' and actual_bytes.startswith(b'#!')
        payloads = runtime['elf_payloads']
        assert isinstance(payloads, dict) and 1 <= len(payloads) <= 1024
        for path, record in payloads.items():
            assert payload_path(path), 'Invalid runtime ELF payload inventory path: ' + repr(path)[:4096]
            assert set(record) == {'sha256', 'kind', 'needed', 'dynamic_dependencies'}
            raw = inspect_file(image, path)
            assert raw.startswith(b'\x7fELF') and hashlib.sha256(raw).hexdigest() == record['sha256']
            assert record['kind'] in {'static', 'dynamic'} and isinstance(record['needed'], list)
            if record['kind'] == 'dynamic':
                linked = command(image, 'ldd ' + shlex.quote(path))
                assert not any(part in linked for part in ['libxml','libxslt','libsystemd','libudev','libtinfo',
                    'libncurses','libreadline','libacl','libldap','libgssapi','libicu','libsqlite','libcap','libgmp','not found'])
            else:
                assert record['needed'] == [] and record['dynamic_dependencies'] == []
        assert runtime['dependency_isolation_control'] == {
            'library': 'libxxhash.so.0', 'host_builder_would_resolve': True,
            'runtime_missing_library_refused': True, 'selected_library_restored': True}
        command(image, 'test "$(id -u postgres):$(id -g postgres)" = \'999:999\'\ntest "$(getconf GNU_LIBC_VERSION)" = \'glibc 2.41\'\nlocale -a | grep -Fx \'en_US.utf8\'\ntest ! -e /usr/local/bin/gosu\ntest ! -e /etc/ssl/private/ssl-cert-snakeoil.key\ntest ! -e /usr/bin/gpg\ntest ! -e /usr/bin/perl\n! command -v mountpoint\ntest "$(readlink /usr/bin/awk)" = mawk\n! test -u /usr/local/bin/su-exec\n! test -g /usr/local/bin/su-exec\n')
        launcher = inspect_file(image,'/usr/local/bin/su-exec')
        stamp = inspect_file(image,'/usr/local/share/komizo-su-exec.sha256').decode().strip()
        assert hashlib.sha256(launcher).hexdigest() == stamp
        assert launcher[:6] == b'\x7fELF\x02\x01', 'Launcher is not a native static ELF64 executable'
        offset, entry_size, count = struct.unpack_from('<Q',launcher,32)[0], *struct.unpack_from('<HH',launcher,54)
        assert entry_size >= 56 and offset + count * entry_size <= len(launcher)
        assert all(struct.unpack_from('<I',launcher,offset+i*entry_size)[0] != 3 for i in range(count)), 'Launcher has a dynamic interpreter'
        launcher_pin = json.loads(inspect_file(image,'/usr/local/share/komizo-su-exec.json'))
        assert launcher_pin == json.loads((ROOT/'postgres-runtime/su-exec/PIN.json').read_text())
        buildinfo = inspect_file(image,'/usr/local/share/komizo-su-exec.buildinfo').decode()
        assert 'source='+launcher_pin['commit'] in buildinfo and 'static=true' in buildinfo
        assert all(digest+'  '+name in buildinfo for name,digest in launcher_pin['source_sha256'].items() if name != 'LICENSE')
        print('PostgreSQL image: exact minimal base, authenticated18.6/runtime package/source inventory, declared dependency closure, upstream entrypoint, UID999, libc2.41 locale and reviewed static launcher passed')

def old_data_bind_refusal(image, evidence=None):
    artifacts=Path(evidence) if evidence is not None else Path.cwd()/'.artifacts/postgres-image-proof'
    artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='old-layout-',dir=artifacts) as directory:
        directory=Path(directory)
        current=directory/'current';current.mkdir(mode=0o700)
        legacy=directory/'legacy';legacy.mkdir(mode=0o700)
        if os.geteuid()==0:
            os.chown(current,999,999);os.chown(legacy,999,999)
        marker=directory/'unrelated-marker';marker.write_bytes(b'preserve unrelated source fixture')
        environment=directory/'private.env'
        environment.write_text('POSTGRES_USER=postgres\nPOSTGRES_DB=postgres\nPOSTGRES_PASSWORD='+uuid.uuid4().hex+'\n')
        environment.chmod(0o600)
        result=native().run(['run','--rm','--pull=never','--network=none','--read-only',
            '--read-only-tmpfs=false','--user=999:999',*native().bind_mount_user_namespace(999,999),
            '--cap-drop=ALL','--security-opt=no-new-privileges','--memory=64m','--pids-limit=64',
            '--timeout=20','--label','io.komizo.pg-layout-proof='+uuid.uuid4().hex,
            '--tmpfs','/run/postgresql:rw,nosuid,nodev,noexec,size=16777216,mode=1777',
            '--volume',str(current)+':/var/lib/postgresql:rw',
            '--volume',str(legacy)+':/var/lib/postgresql/data:ro',
            '--env-file',str(environment),image,'postgres'],capture_output=True,text=True,timeout=30)
        assert result.returncode==1 and '/var/lib/postgresql/data (unused mount/volume)' in result.stderr, (
            'The actual empty legacy bind did not trigger the upstream old-data refusal',result.returncode,result.stderr)
        assert not (current/'18/docker/PG_VERSION').exists(), 'Old-layout refusal initialized a replacement cluster'
        assert not any(legacy.iterdir()), 'Old-layout refusal changed the old bind'
        assert marker.read_bytes()==b'preserve unrelated source fixture'
        print('Native empty legacy bind refused through the mountinfo/awk guard before database initialization; old bytes and unrelated marker retained')
