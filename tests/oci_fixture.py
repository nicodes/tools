"""Independent small OCI fixtures for archive admission and native controls."""
import gzip
import hashlib
import io
import json
import tarfile

MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER = 'application/vnd.oci.image.layer.v1.tar'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def descriptor(body, media):
    return {'digest': 'sha256:' + hashlib.sha256(body).hexdigest(), 'size': len(body), 'mediaType': media}


def contents(reference, config, layers, *, compressed=False):
    rows = {}
    def blob(body, media):
        value = descriptor(body, media)
        rows['blobs/sha256/' + value['digest'][7:]] = body
        return value
    configuration = blob(encoded(config), CONFIG)
    layer_rows = [blob(gzip.compress(body, mtime=0) if compressed else body, LAYER + ('+gzip' if compressed else '')) for body in layers]
    manifest = blob(encoded({'schemaVersion': 2, 'mediaType': MANIFEST, 'config': configuration, 'layers': layer_rows}), MANIFEST)
    manifest['annotations'] = {'org.opencontainers.image.ref.name': reference}
    rows['index.json'] = encoded({'schemaVersion': 2, 'manifests': [manifest]})
    rows['oci-layout'] = encoded({'imageLayoutVersion': '1.0.0'})
    return rows


def write(path, rows):
    with tarfile.open(path, 'w', format=tarfile.USTAR_FORMAT) as output:
        for name, body in rows.items():
            item = tarfile.TarInfo(name); item.mode = 0o644; item.size = len(body)
            output.addfile(item, io.BytesIO(body))
    return {'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def filesystem(name, body):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as packet:
        item = tarfile.TarInfo(name); item.mode = 0o644; item.size = len(body)
        packet.addfile(item, io.BytesIO(body))
    return output.getvalue()
