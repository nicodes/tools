"""Assemble the source-owned PostgreSQL runtime from authenticated inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
PIN = json.loads((HERE / 'PIN.json').read_text())
ROOT = Path('/out/rootfs')
WORK = Path('/build/work')
JOBS = str(min(4, os.cpu_count() or 1))
ENV = dict(os.environ, LC_ALL='C.UTF-8',
           CFLAGS='-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2',
           LDFLAGS='-Wl,-z,relro,-z,now', FORCE_UNSAFE_CONFIGURE='1')
COMPONENTS = []
PACKAGES = []
CONFIGURE_LOGS = {}
EXECUTABLES = ['postgres', 'initdb', 'pg_ctl', 'pg_isready', 'psql', 'pg_dump', 'pg_dumpall',
               'pg_restore', 'pg_config', 'createdb', 'dropdb', 'createuser', 'dropuser',
               'bash', 'ls', 'cp', 'id', 'sleep', 'env', 'stat', 'getconf', 'getent',
               'locale', 'find', 'grep', 'awk', 'gzip', 'gunzip', 'xzcat', 'zstd']


def validate_pin(value):
    fields = {'version', 'builder', 'runtime_base', 'entrypoint_base', 'entrypoint_source', 'postgresql',
              'debian_sources', 'runtime_packages', 'database_uid', 'database_gid',
              'locale', 'collation_version'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('runtime constructor requires its closed versioned source pin')
    image = re.compile(r'[a-z0-9./:_-]+@sha256:[a-f0-9]{64}\Z')
    if any(not isinstance(value[name], str) or not image.fullmatch(value[name])
           for name in ['builder', 'runtime_base', 'entrypoint_base']):
        raise ValueError('runtime constructor requires immutable base identities')
    entrypoint = value['entrypoint_source']
    if (not isinstance(entrypoint, dict) or set(entrypoint) != {'repository', 'revision', 'files_sha256'}
            or entrypoint['repository'] != 'docker-library/postgres'
            or not isinstance(entrypoint['revision'], str) or not re.fullmatch('[a-f0-9]{40}', entrypoint['revision'])
            or not isinstance(entrypoint['files_sha256'], dict)
            or set(entrypoint['files_sha256']) != {'docker-entrypoint.sh', 'LICENSE', 'AUTHORS'}
            or any(not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                   for digest in entrypoint['files_sha256'].values())):
        raise ValueError('runtime constructor requires the closed upstream entrypoint source')
    postgres = value['postgresql']
    if (not isinstance(postgres, dict) or set(postgres) != {'version', 'url', 'sha256', 'configure', 'source_patches'}
            or any(not isinstance(postgres[name], str) for name in ['version', 'url', 'sha256'])
            or not re.fullmatch(r'18\.\d{1,2}', postgres.get('version', ''))
            or postgres['url'] != 'https://ftp.postgresql.org/pub/source/v' + postgres['version']
                + '/postgresql-' + postgres['version'] + '.tar.bz2'
            or not re.fullmatch('[a-f0-9]{64}', postgres.get('sha256', ''))):
        raise ValueError('runtime constructor requires exact official PostgreSQL source bytes')
    patches = postgres['source_patches']
    if (not isinstance(patches, list) or len(patches) != 1 or not isinstance(patches[0], dict)
            or set(patches[0]) != {'path', 'sha256_before', 'sha256_after', 'old', 'new'}
            or patches[0]['path'] != 'src/include/pg_config_manual.h'
            or patches[0]['old'] != '#define DEFAULT_PGSOCKET_DIR  "/tmp"'
            or patches[0]['new'] != '#define DEFAULT_PGSOCKET_DIR  "/run/postgresql"'
            or any(not isinstance(patches[0][name], str) or not re.fullmatch('[a-f0-9]{64}', patches[0][name])
                   for name in ['sha256_before', 'sha256_after'])):
        raise ValueError('runtime constructor requires its exact canonical socket-default patch')
    allowed = {'--prefix=/usr/local', '--with-openssl', '--with-lz4', '--with-zstd',
               '--with-system-tzdata=/usr/share/zoneinfo', '--without-readline', '--without-icu',
               '--without-libxml', '--without-libxslt', '--without-gssapi', '--without-ldap',
               '--without-pam', '--without-systemd', '--without-llvm', '--without-perl',
               '--without-python', '--without-tcl'}
    if (not isinstance(postgres['configure'], list) or any(not isinstance(flag, str) for flag in postgres['configure'])
            or len(postgres['configure']) != len(allowed)
            or set(postgres['configure']) != allowed):
        raise ValueError('runtime constructor requires its reviewed server and client feature set')
    sources = value['debian_sources']
    if (not isinstance(sources, dict) or set(sources) != {'bash', 'coreutils'}
            or any(not isinstance(version, str) or not re.fullmatch('[A-Za-z0-9.+:~_-]{1,80}', version)
                   for version in sources.values())):
        raise ValueError('runtime constructor requires exact stable source package versions')
    packages = value['runtime_packages']
    if (not isinstance(packages, list) or not 1 <= len(packages) <= 64
            or any(not isinstance(package, str) or not re.fullmatch('[a-z][a-z0-9+.-]{0,63}', package)
                   for package in packages) or len(packages) != len(set(packages))):
        raise ValueError('runtime constructor requires literal distinct runtime packages')
    if (any(type(value[name]) is not int or not 1 <= value[name] <= 65534
            for name in ['database_uid', 'database_gid'])
            or any(value[name] in {65532, 65534} for name in ['database_uid', 'database_gid'])
            or not isinstance(value['locale'], str) or not re.fullmatch(r'[a-z]{2}_[A-Z]{2}\.utf8', value['locale'])
            or value['collation_version'] != '2.41'):
        raise ValueError('runtime constructor requires its explicit retained data identity and locale')
    return value


def run(arguments, *, cwd=WORK, capture=False, timeout=600, environment=ENV):
    return subprocess.run(arguments, cwd=cwd, env=environment, check=True,
                          text=True, stdout=subprocess.PIPE if capture else None,
                          timeout=timeout).stdout


def configure(directory, arguments, name):
    # A successful exit alone does not establish that configure understood every
    # requested feature. Keep finite diagnostics and refuse ignored options.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(['./configure', *arguments], cwd=directory, env=ENV,
                                stdout=output, stderr=subprocess.STDOUT, timeout=600)
        output.seek(0)
        contents = output.read((4 << 20) + 1)
    if len(contents) > 4 << 20:
        raise ValueError('configure diagnostics exceed their finite bound')
    text = contents.decode('utf-8', errors='replace')
    print(text, end='', flush=True)
    if re.search(r'configure:.*unrecognized options?', text, re.IGNORECASE):
        raise ValueError('configure ignored an unrecognized requested feature')
    result.check_returncode()
    CONFIGURE_LOGS[name] = {'sha256': hashlib.sha256(contents).hexdigest(),
                            'arguments': arguments}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            value.update(block)
    return value.hexdigest()


def entrypoint_source():
    result = {}
    for name, expected in PIN['entrypoint_source']['files_sha256'].items():
        path = HERE / 'entrypoint' / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise ValueError('upstream entrypoint requires bounded unlinked source files')
        value = path.read_bytes()
        if hashlib.sha256(value).hexdigest() != expected:
            raise ValueError('upstream entrypoint source differs from its frozen identity')
        result[name] = value
    return result


def put(path, value, mode=0o644):
    destination = ROOT / path.lstrip('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value)
    destination.chmod(mode)


def source(package, version):
    directory = WORK / package
    directory.mkdir()
    run(['apt-get', 'source', package + '=' + version], cwd=directory, timeout=180)
    directories = [path for path in directory.iterdir() if path.is_dir()]
    if len(directories) != 1:
        raise ValueError('authenticated source package has an ambiguous tree')
    actual = run(['dpkg-parsechangelog', '--file', str(directories[0] / 'debian/changelog'),
                  '--show-field', 'Version'], capture=True).strip()
    if actual != version:
        raise ValueError('authenticated Debian source differs from its claimed source version')
    archives = {path.name: digest(path) for path in directory.iterdir() if path.is_file()}
    if not any(name.endswith('.dsc') for name in archives):
        raise ValueError('Debian source package descriptor is missing')
    return directories[0], archives


def package_record(name, source_name, version, description):
    architecture = run(['dpkg', '--print-architecture'], capture=True).strip()
    put('/var/lib/dpkg/status.d/' + name,
        'Package: ' + name + '\nStatus: install ok installed\n'
        'Architecture: ' + architecture + '\nVersion: ' + version + '\n'
        'Source: ' + source_name + '\nMaintainer: Komizo runtime maintainers\n'
        'Description: ' + description + '\n')


def build_debian(package, flags):
    version = PIN['debian_sources'][package]
    directory, archives = source(package, version)
    configure(directory, ['--prefix=/usr/local', *flags], package)
    run(['make', '-j' + JOBS], cwd=directory)
    run(['make', 'DESTDIR=' + str(ROOT), 'install'], cwd=directory)
    for name in ['COPYING', 'debian/copyright']:
        original = directory / name
        if original.is_file():
            target = ROOT / 'usr/share/doc' / ('komizo-' + package) / original.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
    COMPONENTS.append({'name': package, 'debian_source_version': version,
                       'debian_source_archives_sha256': archives,
                       'configure': ['--prefix=/usr/local', *flags]})
    package_record(package, package, version + '+komizo1',
                   'Source-owned build of authenticated Debian ' + package)


def build_postgres():
    record = PIN['postgresql']
    archive = WORK / 'postgresql.tar.bz2'
    run(['curl', '--fail', '--location', '--proto', '=https', '--tlsv1.2',
         '--proto-redir', '=https', '--max-time', '120', '--retry', '2', '--retry-max-time', '180',
         '--output', str(archive), record['url']], timeout=200)
    if digest(archive) != record['sha256']:
        raise ValueError('PostgreSQL source differs from its published immutable hash')
    with tarfile.open(archive, 'r:bz2') as source_archive:
        members = source_archive.getmembers()
        if len(members) > 50000 or sum(max(0, member.size) for member in members) > 512 << 20:
            raise ValueError('PostgreSQL source archive exceeds its finite bounds')
        source_archive.extractall(WORK, filter='data')
    directory = WORK / ('postgresql-' + record['version'])
    for patch in record['source_patches']:
        path = directory / patch['path']
        before = path.read_bytes()
        old, new = patch['old'].encode(), patch['new'].encode()
        if hashlib.sha256(before).hexdigest() != patch['sha256_before'] or before.count(old) != 1:
            raise ValueError('PostgreSQL source patch does not match the authenticated original')
        after = before.replace(old, new)
        if hashlib.sha256(after).hexdigest() != patch['sha256_after']:
            raise ValueError('PostgreSQL source patch differs from its declared result')
        path.write_bytes(after)
    configure(directory, record['configure'], 'postgresql')
    run(['make', '-j' + JOBS], cwd=directory)
    run(['make', 'DESTDIR=' + str(ROOT), 'install'], cwd=directory)
    target = ROOT / 'usr/share/doc/komizo-postgresql/COPYRIGHT'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(directory / 'COPYRIGHT', target)
    COMPONENTS.append({'name': 'postgresql', **record})
    package_record('postgresql-18', 'postgresql-18', record['version'] + '-0komizo1',
                   'PostgreSQL ' + record['version'] + ' built from the published upstream source')


def runtime_packages():
    directory = WORK / 'runtime-packages'
    directory.mkdir()
    run(['apt-get', 'download', *PIN['runtime_packages']], cwd=directory, timeout=180)
    for path in sorted(directory.glob('*.deb')):
        fields = run(['dpkg-deb', '--field', str(path)], capture=True)
        values = {}
        for line in fields.splitlines():
            if line and not line[0].isspace() and ':' in line:
                key, value = line.split(':', 1)
                values[key] = value.strip()
        name = values.get('Package', '')
        if name not in PIN['runtime_packages'] or not re.fullmatch('[a-z0-9+.-]+', name):
            raise ValueError('downloaded runtime package was not selected')
        run(['dpkg-deb', '--extract', str(path), str(ROOT)])
        put('/var/lib/dpkg/status.d/' + name, fields)
        PACKAGES.append({'name': name, 'version': values['Version'],
                         'source': values.get('Source', name),
                         'architecture': values['Architecture'], 'sha256': digest(path)})
    if {item['name'] for item in PACKAGES} != set(PIN['runtime_packages']):
        raise ValueError('authenticated runtime package set is incomplete')


def symlink(path, target):
    path = ROOT / path.lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_file():
        path.unlink()
    path.symlink_to(target)


def identity_and_entrypoint():
    launcher = Path('/build/launcher')
    for name in ['su-exec', 'su-exec.buildinfo', 'su-exec.sha256']:
        target = ROOT / ('usr/local/bin/su-exec' if name == 'su-exec'
                         else 'usr/local/share/komizo-' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(launcher / name, target)
        target.chmod(0o755 if name == 'su-exec' else 0o644)
    launcher_pin = json.loads(Path('/build/su-exec/PIN.json').read_text())
    put('/usr/local/share/komizo-su-exec.json', json.dumps(launcher_pin, sort_keys=True) + '\n')
    target = ROOT / 'usr/share/licenses/su-exec/LICENSE'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile('/build/su-exec/source/LICENSE', target)
    if digest(ROOT / 'usr/local/bin/su-exec') != (launcher / 'su-exec.sha256').read_text().strip():
        raise ValueError('reviewed launcher binary differs from its actual build stamp')
    COMPONENTS.append({'name': 'su-exec', 'pin': launcher_pin,
                       'binary_sha256': digest(ROOT / 'usr/local/bin/su-exec'),
                       'buildinfo': (launcher / 'su-exec.buildinfo').read_text().splitlines()})
    uid, gid = str(PIN['database_uid']), str(PIN['database_gid'])
    put('/etc/passwd', 'root:x:0:0:root:/root:/bin/sh\n'
        'postgres:x:' + uid + ':' + gid + ':PostgreSQL:/var/lib/postgresql:/bin/sh\n'
        'nonroot:x:65532:65532:nonroot:/home/nonroot:/sbin/nologin\n'
        'nobody:x:65534:65534:nobody:/nonexistent:/sbin/nologin\n')
    put('/etc/group', 'root:x:0:\nssl-cert:x:101:postgres\npostgres:x:' + gid + ':\n'
        'nonroot:x:65532:\nnogroup:x:65534:\n')
    put('/etc/nsswitch.conf', 'passwd: files\ngroup: files\nshadow: files\nhosts: files dns\n')
    for directory, mode in [('var/lib/postgresql', 0o755), ('run/postgresql', 0o3775),
                            ('docker-entrypoint-initdb.d', 0o755), ('usr/lib/locale', 0o755)]:
        path = ROOT / directory
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
        if directory in {'var/lib/postgresql', 'run/postgresql'}:
            os.chown(path, PIN['database_uid'], PIN['database_gid'])
    for directory in ['bin', 'sbin', 'lib', 'lib64']:
        symlink('/' + directory, 'usr/' + directory)
    symlink('/var/run', '/run')
    symlink('/usr/bin/sh', 'dash')
    symlink('/usr/bin/awk', 'mawk')
    for path in (ROOT / 'usr/local/bin').iterdir():
        if path.is_file() and os.access(path, os.X_OK):
            symlink('/usr/bin/' + path.name, '../local/bin/' + path.name)
    source = entrypoint_source()
    original = source['docker-entrypoint.sh']
    if original.count(b'exec gosu postgres ') != 1:
        raise ValueError('frozen upstream PostgreSQL entrypoint changed')
    target = ROOT / 'usr/local/bin/docker-entrypoint.sh'
    target.write_bytes(original.replace(b'exec gosu postgres ', b'exec su-exec postgres '))
    target.chmod(0o755)
    COMPONENTS.append({'name': 'postgresql-entrypoint', 'base': PIN['entrypoint_base'],
                       'source': PIN['entrypoint_source'],
                       'upstream_sha256': hashlib.sha256(original).hexdigest(),
                       'sha256': digest(target), 'change': 'exec gosu postgres -> exec su-exec postgres'})
    for name in ['LICENSE', 'AUTHORS']:
        put('/usr/share/licenses/postgresql-entrypoint/' + name, source[name].decode())
    # Generate exactly the libc locale required by the retained databases.
    run(['localedef', '--no-archive', '--prefix=' + str(ROOT), '-i', PIN['locale'].split('.')[0],
         '-f', 'UTF-8', PIN['locale']], timeout=60)
    shutil.copytree('/etc/ssl/certs', ROOT / 'etc/ssl/certs', dirs_exist_ok=True, symlinks=True)
    multiarch = run(['dpkg-architecture', '-qDEB_HOST_MULTIARCH'], capture=True).strip()
    if not re.fullmatch('[a-z0-9_-]+', multiarch):
        raise ValueError('runtime library architecture is invalid')
    put('/etc/ld.so.conf', 'include /etc/ld.so.conf.d/*.conf\n')
    put('/etc/ld.so.conf.d/komizo-postgres.conf', '/usr/local/lib\n/usr/lib/' + multiarch + '\n')
    run(['ldconfig', '-r', str(ROOT)])


def validate_dependencies():
    forbidden = ['libxml', 'libxslt', 'libsystemd', 'libudev', 'libtinfo', 'libncurses',
                 'libreadline', 'libacl', 'libldap', 'libgssapi', 'libicu', 'libsqlite', 'libcap', 'libgmp']
    proof = {}
    multiarch = run(['dpkg-architecture', '-qDEB_HOST_MULTIARCH'], capture=True).strip()
    for name in EXECUTABLES:
        paths = [ROOT / directory / name for directory in ['usr/local/bin', 'usr/bin']]
        path = next((path for path in paths if path.is_file()), None)
        if path is None:
            raise ValueError('required runtime executable is missing: ' + name)
        with path.open('rb') as stream:
            elf = stream.read(4) == b'\x7fELF'
        linked = run(['ldd', str(path)], capture=True,
                     environment=ENV | {'LD_LIBRARY_PATH': ':'.join([
                         str(ROOT / 'usr/local/lib'), str(ROOT / 'usr/lib' / multiarch)])}) if elf else ''
        if any(item in linked for item in forbidden) or 'not found' in linked:
            raise ValueError('runtime executable retained an excluded or missing dependency: ' + name)
        proof[name] = {'path': '/' + path.relative_to(ROOT).as_posix(), 'sha256': digest(path),
                       'kind': 'elf' if elf else 'script',
                       'dynamic_dependencies': [re.sub(r' \(0x[a-f0-9]+\)', '', line.strip())
                                                for line in linked.splitlines()]}
    return proof


def main():
    validate_pin(PIN)
    entrypoint_source()
    ROOT.mkdir(parents=True)
    WORK.mkdir(parents=True)
    # Source compilation uses the distribution's authenticated, patched sources.
    build_debian('bash', ['--without-bash-malloc', '--disable-readline', '--disable-history',
                          '--disable-nls', '--enable-net-redirections'])
    build_debian('coreutils', ['--disable-acl', '--without-selinux',
                               '--without-openssl', '--disable-nls'])
    build_postgres()
    runtime_packages()
    # Development headers/static archives are not part of this runtime package.
    shutil.rmtree(ROOT / 'usr/local/include', ignore_errors=True)
    for path in (ROOT / 'usr/local/lib').glob('*.a'):
        path.unlink()
    identity_and_entrypoint()
    proof = validate_dependencies()
    record = {'kind': 'komizo.postgres-runtime', 'version': 1, 'pin': PIN,
              'components': COMPONENTS, 'runtime_packages': PACKAGES,
              'compiler': run(['cc', '--version'], capture=True).splitlines()[0],
              'cflags': ENV['CFLAGS'], 'ldflags': ENV['LDFLAGS'], 'executables': proof,
              'configure_logs': CONFIGURE_LOGS}
    put('/usr/local/share/komizo-postgres-runtime.json', json.dumps(record, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
