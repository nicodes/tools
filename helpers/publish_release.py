"""Publish exact prevalidated native OCI release bytes without an engine."""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oci_registry
from release_packet import regular

def publish(packet, tools, environment, *, registry_factory=oci_registry.Registry):
    spec = importlib.util.spec_from_file_location('_publisher_oci_archive', Path(tools) / 'helpers/oci_archive.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archives = {}
    # Validate every image before obtaining credentials or creating registry effects.
    with ExitStack() as stack:
        for name, artifact in packet.descriptor['artifacts'].items():
            if artifact['kind'] == 'image':
                archive = stack.enter_context(module.OCIArchive(packet.root / ('images/' + name + '.oci.tar'),
                    expected_reference=artifact['reference'], expected_config=artifact['config_digest']))
                labels = archive.metadata['config'].get('config', {}).get('Labels', {})
                if (archive.metadata['manifest_digest'] != artifact['digest']
                        or archive.metadata['platform'] != artifact['platform']
                        or labels.get('org.opencontainers.image.revision') != packet.descriptor['source']['revision']
                        or labels.get('org.opencontainers.image.source') != 'https://github.com/' + packet.descriptor['source']['repository']):
                    raise ValueError('OCI archive differs from its accepted release descriptor')
                archives[name] = archive
        packet.unchanged()
        username = environment.get('KOMIZO_REGISTRY_USER', '')
        password = environment.get('KOMIZO_REGISTRY_TOKEN', '')
        if not username or not password:
            raise ValueError('publication requires explicit registry authority')
        published = {}
        for name, artifact in packet.descriptor['artifacts'].items():
            server, repository, manifest_digest = oci_registry.reference(artifact['reference'])
            owner = packet.descriptor['source']['repository'].split('/')[0].lower()
            if server != 'ghcr.io' or not repository.startswith(owner + '/'):
                raise ValueError('publication cannot send authority outside its source owner')
            remote = registry_factory(server, repository, username=username, password=password)
            packet.unchanged()
            if artifact['kind'] == 'image':
                archive = archives[name]
                for blob in archive.metadata['blobs']:
                    with archive.open_blob(blob['digest']) as source:
                        remote.upload_blob(blob['digest'], blob['size'], source)
                archive.unchanged()
                remote.put_manifest(manifest_digest, archive.manifest_bytes)
            else:
                with regular(packet.root / ('static/' + name + '.tar.gz')) as (source, _):
                    remote.publish_static(artifact, source)
            published[name] = artifact['reference']
        packet.unchanged()
        for archive in archives.values():
            archive.unchanged()
        return published

