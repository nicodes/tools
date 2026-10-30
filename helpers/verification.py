"""Bind caller-owned commands to source, test coverage and immutable outputs."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile

CONTEXT_KEYS = ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID',
                'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'GITHUB_SHA',
                'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_EVENT_NAME')
CONFIG_FILES = ('mise.toml', '.mise.toml', 'mise.lock', '.tool-versions',
                'komizo.json', 'deploy/release.json')
NAME = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')


def git(*args):
    return subprocess.check_output(['git', *args], text=True, timeout=30).strip()


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def execution_context():
    return {key: os.environ[key] for key in CONTEXT_KEYS if os.environ.get(key)}


def checkout_binding():
    clean = subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], timeout=30).returncode
    if clean not in (0, 1):
        raise ValueError('cannot determine tracked source identity')
    files = {}
    for name in CONFIG_FILES:
        path = Path(name)
        if path.is_symlink():
            raise ValueError('verification configuration must be a regular source file')
        if path.is_file():
            files[name] = sha(path)
    return {'source_tree': git('rev-parse', 'HEAD^{tree}'), 'tracked_source_clean': clean == 0,
            'configuration_sha256': files}


def scope(artifacts=(), images=(), environment=(), revision_environment=(), directory='.artifacts/verification'):
    if any(not isinstance(values, (tuple, list)) or len(values)>128 or
           any(not isinstance(value,str) for value in values)
           for values in (artifacts, images, environment, revision_environment)):
        raise ValueError('verification inputs must be bounded string lists')
    for pattern in artifacts:
        if not pattern or Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise ValueError('artifact paths must stay within the checkout')
    names = sorted(set(environment) | set(revision_environment))
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', name) for name in names):
        raise ValueError('invalid build environment name')
    return {'artifacts': list(artifacts), 'images': list(images), 'environment': names,
            'revision_environment': sorted(set(revision_environment)), 'evidence_directory': str(directory)}


def release_definition(file=Path('deploy/release.json')):
    file=Path(file)
    if file.is_symlink() or any(parent.is_symlink() for parent in file.parents) or not file.is_file() or file.stat().st_size>1<<20:
        raise ValueError('release definition must be a bounded regular caller-owned file')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError('duplicate release definition field')
            result[key]=value
        return result
    value=json.loads(file.read_text(),object_pairs_hook=unique)
    required={'version','project','image_base','inputs','tests'}
    if (not isinstance(value,dict) or not required<=value.keys() or set(value)-required-{'stateful_contract'} or
            type(value['version']) is not int or value['version']!=1 or not isinstance(value['project'],str) or
            not NAME.fullmatch(value['project']) or not isinstance(value['image_base'],str) or
            not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*',value['image_base'])):
        raise ValueError('complete release artifact definition required')
    names=value['tests']
    if not isinstance(names,list) or not 1<=len(names)<=32 or any(not isinstance(name,str) or not NAME.fullmatch(name) for name in names) or len(set(names))!=len(names):
        raise ValueError('release publication requires distinct declared test tasks')
    inputs=value['inputs']
    if not isinstance(inputs,dict) or set(inputs)!={'artifacts','images','environment','revision_environment'}:
        raise ValueError('release definition requires explicit artifact and environment inputs')
    scope(**inputs)
    if 'stateful_contract' in value:
        path=value['stateful_contract']
        if not isinstance(path,str) or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('stateful declaration must be caller-owned source')
    try:
        relative=file.absolute().relative_to(Path(git('rev-parse','--show-toplevel')))
        git('ls-files','--error-unmatch','--',str(relative))
    except (ValueError,subprocess.CalledProcessError):
        raise ValueError('release definition must be tracked caller-owned source') from None
    return value


def identity(declaration):
    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], timeout=30)
    files = {}
    for raw in tracked.split(b'\0'):
        if not raw:
            continue
        path = Path(os.fsdecode(raw))
        if any(path.match(pattern) or str(path).startswith(pattern.rstrip('/')+'/')
               for pattern in [*declaration['artifacts'], declaration['evidence_directory']]):
            continue
        if path.is_symlink():
            files[str(path)] = {'link': os.readlink(path)}
        elif path.is_file():
            files[str(path)] = {'sha256': sha(path), 'executable': bool(path.stat().st_mode & 0o111)}
        else:
            files[str(path)] = None
    values = {name: os.environ.get(name, '') for name in declaration['environment']}
    wire = {'revision': git('rev-parse', 'HEAD'), 'files': files, 'environment': values,
            'variant': os.environ.get('BUILD_VARIANT', 'production'), 'scope': declaration}
    return hashlib.sha256(json.dumps(wire, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve_images(declaration, revision):
    def substitute(match):
        name = match[1]
        if name == 'revision':
            return revision
        if name not in declaration['environment'] or not os.environ.get(name):
            raise ValueError('image template needs an explicitly declared build input')
        return os.environ[name]
    result = []
    for reference in declaration['images']:
        value = re.sub(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', substitute, reference)
        if not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*:[a-f0-9]{40}', value):
            raise ValueError('build image must bind a registry repository and exact source revision')
        result.append(value)
    return result


def artifacts(declaration):
    found = {}
    for pattern in declaration['artifacts']:
        candidates = []
        for match in sorted(Path('.').glob(pattern)):
            candidates.extend(sorted(match.rglob('*')) if match.is_dir() else [match])
        regular = [path for path in candidates if path.is_file()]
        evidence = declaration['evidence_directory'].rstrip('/')
        regular = [path for path in regular if str(path) != evidence and not str(path).startswith(evidence+'/')]
        if not regular:
            raise ValueError('a declared build artifact is missing')
        for path in regular:
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise ValueError('build artifacts must not follow links')
            found[str(path)] = sha(path)
    refs = resolve_images(declaration, git('rev-parse', 'HEAD'))
    if refs:
        images = json.loads(subprocess.check_output(['docker', 'image', 'inspect', *refs], timeout=60))
        if len(images) != len(refs):
            raise ValueError('a declared image is missing')
        for ref, image in zip(refs, images, strict=True):
            if ref not in image.get('RepoTags', []) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image.get('Id', '')):
                raise ValueError('built image has no matching immutable identity')
            found['image:'+ref] = image['Id']
    return found


def protected_directory(path):
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('verification output must stay within the checkout')
    parent = Path('.')
    for part in path.parts:
        parent = parent/part
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('verification output directory is not protected')
    return path


def write(path, value):
    protected_directory(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('verification output is not an owned regular file')
    descriptor, name = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output, sort_keys=True)
            output.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def locked():
    path = Path(git('rev-parse', '--git-path', 'verification.lock'))
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def invoke(command, environment=None):
    if not command:
        raise ValueError('a caller-owned command is required')
    try:
        subprocess.run(command, env=environment, check=True)
    except OSError:
        raise ValueError('verification command could not start') from None


def run_command(kind, name, declaration, command, directory, source_only=False, variant='production'):
    if not NAME.fullmatch(name):
        raise ValueError('verification name must be a task slug')
    with locked():
        source = git('rev-parse', 'HEAD')
        if kind == 'test':
            for previous in (directory/'test.json', directory/(name+'.json')):
                if previous.is_symlink():
                    raise ValueError('test receipt must not be a link')
                previous.unlink(missing_ok=True)
        if kind == 'build':
            if os.environ.get('BUILD_VARIANT', 'production') != variant:
                raise ValueError('build variant differs from the caller-selected production target')
            if not source_only and not (declaration['artifacts'] or declaration['images']):
                raise ValueError('a build must declare its outputs or explicitly select source-only verification')
            for key in declaration['revision_environment']:
                if os.environ.get(key) and os.environ[key] != source:
                    raise ValueError('build revision input differs from the checked-out source')
            previous = directory/'build.json'
            if previous.exists() or previous.is_symlink():
                if previous.is_symlink():
                    raise ValueError('build receipt must not be a link')
                previous.unlink()
        before = identity(declaration)
        invoke(command)
        if identity(declaration) != before or git('rev-parse', 'HEAD') != source:
            raise ValueError('verification command changed source inputs')
        proof = {'version': 1, 'source_commit': source, 'input_identity': before,
                 'tested_commit': source if kind == 'test' else None,
                 **checkout_binding(), 'execution_context': execution_context()}
        if kind == 'build':
            write(directory/'build.json', {'identity': before, 'artifacts': artifacts(declaration), 'provenance': proof})
        else:
            session = os.environ.get('TOOLS_VERIFICATION_SESSION')
            if session:
                path = Path(session)
                if path.parent != directory/'sessions' or not re.fullmatch('[a-f0-9]{32}', path.name):
                    raise ValueError('test receipt belongs to another checkout or verification session')
                if path.is_symlink() or not path.is_dir() or (path/'session.json').is_symlink():
                    raise ValueError('test evidence session was not opened by its caller')
                protected_directory(path)
                opened = json.loads((path/'session.json').read_text())
                if opened != {'source_commit': source, 'source_tree': proof['source_tree'], 'execution_context': proof['execution_context']}:
                    raise ValueError('test evidence session belongs to another source or workflow context')
                write(path/(name+'.json'), {'proof': proof, 'scope': declaration})
            write(directory/(name+'.json'), proof)
        return proof


def run_session(tests, command, directory):
    if not tests or len(set(tests)) != len(tests) or any(not NAME.fullmatch(name) for name in tests):
        raise ValueError('declare distinct test task names for this evidence session')
    protected_directory(directory)
    previous = directory/'test.json'
    if previous.is_symlink():
        raise ValueError('test receipt must not be a link')
    previous.unlink(missing_ok=True)
    session = directory/'sessions'/secrets.token_hex(16)
    protected_directory(session)
    source = git('rev-parse', 'HEAD')
    context = execution_context()
    write(session/'session.json', {'source_commit': source, 'source_tree': git('rev-parse', 'HEAD^{tree}'), 'execution_context': context})
    invoke(command, {**os.environ, 'TOOLS_VERIFICATION_SESSION': str(session)})
    with locked():
        proofs, scopes = [], []
        for name in tests:
            file = session/(name+'.json')
            if file.is_symlink() or not file.is_file() or file.stat().st_size > 65536:
                raise ValueError('not every declared test completed in this evidence session')
            record = json.loads(file.read_text())
            proof = record['proof']
            if proof['source_commit'] != source or proof['tested_commit'] != source or proof['execution_context'] != context:
                raise ValueError('test source or execution context changed within this evidence session')
            proofs.append(proof)
            scopes.append(record['scope'])
        proof = proofs[0]
        if any(p['input_identity'] != proof['input_identity'] or p['source_tree'] != proof['source_tree'] for p in proofs):
            raise ValueError('declared tests did not verify the same source inputs')
        if any(value != scopes[0] for value in scopes) or identity(scopes[0]) != proof['input_identity']:
            raise ValueError('source inputs changed after tests completed')
        if git('rev-parse', 'HEAD') != source or any(proof[key] != value for key,value in checkout_binding().items()):
            raise ValueError('source changed after its tests completed')
        proof['test_receipts'] = {name: {'source_commit': source, 'input_identity': proof['input_identity']} for name in tests}
        write(directory/'test.json', proof)
        return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['build', 'test', 'session'])
    parser.add_argument('--name')
    parser.add_argument('--artifact', action='append', default=[])
    parser.add_argument('--image', action='append', default=[])
    parser.add_argument('--environment', action='append', default=[])
    parser.add_argument('--revision-environment', action='append', default=[])
    parser.add_argument('--test', action='append', default=[])
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--variant', default='production')
    parser.add_argument('--definition',type=Path)
    parser.add_argument('--directory', type=Path, default=Path('.artifacts/verification'))
    options, command = sys.argv[1:], []
    if '--' in options:
        index = options.index('--'); command, options = options[index+1:], options[:index]
    args = parser.parse_args(options)
    protected_directory(args.directory)
    if args.kind == 'session':
        run_session(args.test, command, args.directory)
    else:
        if args.definition:
            if args.artifact or args.image or args.environment or args.revision_environment:
                raise ValueError('select explicit verification inputs or one release definition')
            declaration=scope(**release_definition(args.definition)['inputs'],directory=args.directory)
        else:
            declaration = scope(args.artifact, args.image, args.environment, args.revision_environment, args.directory)
        run_command(args.kind, args.name or args.kind, declaration, command, args.directory, args.source_only, args.variant)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        print('Verification command failed; preserve its runner log.', file=sys.stderr)
        raise SystemExit(error.returncode if error.returncode >= 0 else 128-error.returncode)
    except (ValueError, OSError, KeyError) as error:
        print('Verification refused: '+str(error), file=sys.stderr)
        raise SystemExit(1)
