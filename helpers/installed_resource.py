"""Read protected installed resources without names, tags, or ambient authority."""
import argparse
import hashlib
import json
import os
import re
import subprocess

DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
IDENTITY = re.compile(r'[a-f0-9]{64}\Z')
REVISION = re.compile(r'[a-f0-9]{40}\Z')
NAME = re.compile(r'[a-z][a-z0-9-]{0,62}\Z')
REFERENCE = re.compile(r'[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}\Z')
PROVIDERS = {'cookbook_revision', 'tools_revision', 'core_revision'}
FIELDS = {'kind', 'version', 'application', 'instance', 'service', 'release_digest',
    'container_id', 'container_name', 'image_ref', 'image_config_digest', 'volumes', 'deployment'}
MAX_DOCUMENT = 4 << 20


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('installed resource contains repeated fields')
        result[name] = value
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()


def environment():
    result = dict(os.environ)
    for name in ('CONTAINER_HOST', 'CONTAINER_CONNECTION', 'CONTAINER_SSHKEY', 'CONTAINER_PROXY',
                 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH',
                 'REGISTRY_AUTH_FILE', 'DOCKER_CONFIG', 'CONTAINERS_REGISTRIES_CONF',
                 'CONTAINERS_CONF', 'CONTAINERS_STORAGE_CONF'):
        result.pop(name, None)
    return result


def config_digest(value):
    if not isinstance(value, str):
        raise ValueError('installed resource image config identity is invalid')
    value = value if value.startswith('sha256:') else 'sha256:' + value
    if not DIGEST.fullmatch(value):
        raise ValueError('installed resource image config identity is invalid')
    return value


def validate(value, application, service):
    if (not isinstance(value, dict) or set(value) != FIELDS or value.get('kind') != 'komizo.resource'
            or type(value.get('version')) is not int or value['version'] != 1
            or value['application'] != application or value['instance'] != 'production'
            or value['service'] != service or not isinstance(value['container_id'], str)
            or not IDENTITY.fullmatch(value['container_id'])
            or not isinstance(value['container_name'], str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}', value['container_name'])
            or not isinstance(value['release_digest'], str) or not DIGEST.fullmatch(value['release_digest'])
            or not isinstance(value['image_ref'], str) or not REFERENCE.fullmatch(value['image_ref'])
            or config_digest(value['image_config_digest']) != value['image_config_digest']
            or not isinstance(value['volumes'], dict)):
        raise ValueError('protected installed resource identity is invalid')
    deployment = value['deployment']
    if (not isinstance(deployment, dict) or set(deployment) != {'release', 'release_digest', 'provider'}
            or deployment['release_digest'] != value['release_digest']
            or not isinstance(deployment['provider'], dict) or set(deployment['provider']) != PROVIDERS
            or any(not isinstance(v, str) or not REVISION.fullmatch(v) for v in deployment['provider'].values())):
        raise ValueError('installed deployment provider identity is invalid')
    release = deployment['release']
    raw = canonical(release)
    if (not isinstance(release, dict) or len(raw) > MAX_DOCUMENT
            or release.get('kind') != 'komizo.release' or type(release.get('version')) is not int
            or release['version'] != 1 or release.get('application') != application
            or 'sha256:' + hashlib.sha256(raw).hexdigest() != value['release_digest']
            or release.get('providers') != deployment['provider']):
        raise ValueError('installed deployment differs from its exact release descriptor')
    source = release.get('source')
    if (not isinstance(source, dict) or not isinstance(source.get('revision'), str)
            or not REVISION.fullmatch(source['revision']) or not isinstance(source.get('repository'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source['repository'])):
        raise ValueError('installed deployment has no exact source identity')
    workload = release.get('workload')
    services = workload.get('services') if isinstance(workload, dict) else None
    if not isinstance(services, dict):
        raise ValueError('installed release has no service declarations')
    selected = services.get(service)
    artifacts = release.get('artifacts')
    if not isinstance(selected, dict) or not isinstance(artifacts, dict):
        raise ValueError('installed resource has no declared service artifact')
    artifact = artifacts.get(selected.get('artifact'))
    if (not isinstance(artifact, dict) or artifact.get('kind') != 'image'
            or artifact.get('reference') != value['image_ref']
            or artifact.get('config_digest') != value['image_config_digest']
            or artifact.get('digest') != value['image_ref'].rsplit('@', 1)[1]):
        raise ValueError('installed resource differs from its declared service image')
    for logical, volume in value['volumes'].items():
        if (not isinstance(logical, str) or not NAME.fullmatch(logical)
                or not isinstance(volume, dict) or set(volume) != {'name', 'id'}
                or not isinstance(volume['name'], str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}', volume['name'])
                or not isinstance(volume['id'], str) or not DIGEST.fullmatch(volume['id'])):
            raise ValueError('installed resource volume identity is invalid')
    return value


def query(application, service):
    if (not isinstance(application, str) or not NAME.fullmatch(application)
            or not isinstance(service, str) or not NAME.fullmatch(service)):
        raise ValueError('installed resource selector is invalid')
    try:
        result = subprocess.run(['/usr/local/bin/komizo-box', 'resource', '--application', application,
            '--instance', 'production', '--service', service], env=environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode or not 0 < len(result.stdout) <= MAX_DOCUMENT:
            raise ValueError('protected installed resource query refused')
        value = json.loads(result.stdout, object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite installed resource is refused')))
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError):
        raise ValueError('protected installed resource query failed') from None
    return validate(value, application, service)


def inspect(record, output):
    try:
        values = json.loads(output, object_pairs_hook=unique_object)
        if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict)
                or values[0].get('Id') != record['container_id']
                or config_digest(values[0].get('Image')) != record['image_config_digest']):
            raise ValueError('serving container differs from its installed identity')
        info = values[0]
        state = info.get('State') or {}
        if state.get('Running') is not True or state.get('Paused') or state.get('Restarting'):
            raise ValueError('installed resource is not running')
        service = record['deployment']['release']['workload']['services'][record['service']]
        if (info.get('Config') or {}).get('User', '') != service.get('user', ''):
            raise ValueError('installed resource runtime user differs from its declaration')
        return info
    except (UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
        raise ValueError('installed container inspection is unreadable') from None


def volume(record, info, logical, target):
    expected = record['volumes'].get(logical)
    mounts = [entry for entry in info.get('Mounts', []) if entry.get('Destination') == target]
    if (expected is None or len(mounts) != 1 or mounts[0].get('Type') != 'volume'
            or mounts[0].get('Name') != expected['name']):
        raise ValueError('installed data mount differs from its owned volume identity')
    return expected['name']


def metadata(records):
    if not records or any(record['deployment'] != records[0]['deployment'] for record in records):
        raise ValueError('serving resources use different installed releases')
    deployment = records[0]['deployment']
    images = {}
    for name, artifact in deployment['release']['artifacts'].items():
        if not isinstance(artifact, dict):
            raise ValueError('installed release artifact is invalid')
        if artifact.get('kind') == 'image':
            if (not isinstance(artifact.get('reference'), str) or not REFERENCE.fullmatch(artifact['reference'])
                    or config_digest(artifact.get('config_digest')) != artifact['config_digest']):
                raise ValueError('installed release has an invalid recovery image')
            images[name] = {'reference': artifact['reference'], 'image_id': artifact['config_digest']}
    return deployment['release']['source']['revision'], images, deployment


def unchanged(records):
    for record in records:
        if query(record['application'], record['service']) != record:
            raise ValueError('installed resource changed during capture')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', required=True)
    parser.add_argument('--service', required=True)
    args = parser.parse_args()
    print(query(args.application, args.service)['container_id'])


if __name__ == '__main__':
    main()
