import base64
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('registry_artifact', Path(__file__).parents[1] / 'helpers/registry_artifact.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
r = recovery.registry


class Response(io.BytesIO):
    def __init__(self, status, body=b'', headers=None):
        super().__init__(body); self.status = status; self.headers = headers or {}

    def getheader(self, name, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), default)

    def getheaders(self):
        return list(self.headers.items())


class Server:
    def __init__(self):
        self.responses = {}; self.calls = []; self.sock = None

    def __call__(self, host, timeout):
        self.host = host
        return self

    def request(self, method, path, body=None, headers=None):
        self.calls.append((self.host, method, path, dict(headers or {})))
        responses = self.responses.get((self.host, path))
        if not responses:
            raise AssertionError('unexpected registry request: ' + path)
        self.response = responses.pop(0)

    def getresponse(self):
        return self.response

    def close(self):
        pass

    def document(self, path, body, status=200, headers=None, host='ghcr.io'):
        self.responses.setdefault((host, path), []).append(Response(status, body, headers))


def image(server, *, corrupt_diff=False, layers=None, expanded=b'bounded uncompressed image layer'):
    layer = gzip.compress(expanded, mtime=0)
    diff = r.digest(b'different' if corrupt_diff else expanded)
    config = r.canonical({'os': 'linux', 'architecture': 'amd64', 'rootfs': {'type': 'layers', 'diff_ids': [diff]}})
    descriptor = lambda raw, media: {'mediaType': media, 'digest': r.digest(raw), 'size': len(raw)}
    c = descriptor(config, recovery.oci.CONFIG)
    l = descriptor(layer, 'application/vnd.oci.image.layer.v1.tar+gzip')
    manifest = r.canonical({'schemaVersion': 2, 'mediaType': recovery.oci.MANIFEST, 'config': c, 'layers': layers or [l]})
    artifact = {'kind': 'image', 'reference': 'ghcr.io/example/app@' + r.digest(manifest), 'digest': r.digest(manifest),
        'config_digest': r.digest(config), 'platform': 'linux/amd64'}
    server.document('/v2/example/app/manifests/' + r.digest(manifest), manifest)
    server.document('/v2/example/app/blobs/' + r.digest(config), config)
    server.document('/v2/example/app/blobs/' + r.digest(layer), layer)
    return artifact, manifest, config, layer


def static(server, payload=b'opaque accepted tar.gz payload'):
    artifact = {'kind': 'archive', 'blob_digest': r.digest(payload), 'size_bytes': len(payload)}
    manifest = r.artifact_manifest(artifact['blob_digest'], artifact['size_bytes'])
    artifact.update(reference='ghcr.io/example/app@' + r.digest(manifest), digest=r.digest(manifest))
    server.document('/v2/example/app/manifests/' + artifact['digest'], manifest)
    server.document('/v2/example/app/blobs/' + r.digest(b'{}'), b'{}')
    server.document('/v2/example/app/blobs/' + artifact['blob_digest'], payload)
    return artifact, payload


class ExactArtifactRecovery(unittest.TestCase):
    def test_raw_image_manifest_config_layer_and_platform_survive_oci_transport(self):
        server = Server(); artifact, manifest, config, layer = image(server)
        with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
            target = Path(directory) / 'image.oci.tar'
            self.assertEqual(recovery.fetch_image(artifact, target), target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.stat().st_nlink, 1)
            with recovery.oci.OCIArchive(target, artifact['reference'], artifact['config_digest']) as accepted:
                self.assertEqual(accepted.manifest_bytes, manifest)
                for raw in [config, layer]:
                    with accepted.open_blob(r.digest(raw)) as stream:
                        self.assertEqual(stream.read(), raw)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['image.oci.tar'])
        self.assertTrue(all('Authorization' not in call[3] for call in server.calls))

    def test_static_download_preserves_exact_payload_and_complete_identity(self):
        server = Server(); artifact, payload = static(server)
        with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
            target = Path(directory) / 'asset.tar.gz'
            recovery.fetch_static(artifact, target)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_read_authentication_requests_pull_only_and_never_forwards_to_signed_cdn(self):
        server = Server(); artifact, payload = static(server)
        path = '/v2/example/app/manifests/' + artifact['digest']
        server.responses[('ghcr.io', path)].insert(0, Response(401, headers={
            'WWW-Authenticate': 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:example/app:pull"'}))
        server.document('/token?service=ghcr.io&scope=repository%3Aexample%2Fapp%3Apull', b'{"token":"private-token"}')
        blob_path = '/v2/example/app/blobs/' + artifact['blob_digest']
        server.responses[('ghcr.io', blob_path)] = [Response(307, headers={
            'Location': 'https://pkg-containers.githubusercontent.com/signed?token=signed-query'})]
        server.document('/signed?token=signed-query', payload, host='pkg-containers.githubusercontent.com')
        with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
            root = Path(directory); auth = root / 'auth.json'
            auth.write_text(json.dumps({'auths': {'ghcr.io': {'auth': base64.b64encode(b'owner:private-password').decode()}}}))
            auth.chmod(0o600)
            recovery.fetch_static(artifact, root / 'asset', authfile=auth)
        token_call = next(call for call in server.calls if call[2].startswith('/token?'))
        self.assertEqual(token_call[3]['Authorization'], 'Basic ' + base64.b64encode(b'owner:private-password').decode())
        self.assertNotIn('push', token_call[2])
        self.assertNotIn('Authorization', server.calls[-1][3])

    def test_blob_length_digest_and_expanded_rootfs_fail_without_destination(self):
        for failure in ['short', 'long', 'digest', 'diff']:
            with self.subTest(failure=failure):
                server = Server(); artifact, _, _, layer = image(server, corrupt_diff=failure == 'diff')
                key = ('ghcr.io', '/v2/example/app/blobs/' + r.digest(layer))
                if failure in {'short', 'long', 'digest'}:
                    body = layer[:-1] if failure == 'short' else layer+b'x' if failure == 'long' else b'x'*len(layer)
                    server.responses[key] = [Response(200, body)]
                with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
                    root = Path(directory)
                    with self.assertRaises(ValueError):
                        recovery.fetch_image(artifact, root/'image')
                    self.assertEqual(list(root.iterdir()), [])

    def test_compressed_expansion_and_deadline_are_bounded_before_publication(self):
        server=Server(); artifact,_,_,_=image(server,expanded=b'x'*(2<<20))
        with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client,'HTTPSConnection',server), patch.object(recovery,'MAX_BYTES',10000):
            with self.assertRaisesRegex(ValueError,'decompressed bound'):
                recovery.fetch_image(artifact,Path(directory)/'image')
            self.assertEqual(list(Path(directory).iterdir()),[])
        server=Server(); artifact,_=static(server)
        with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client,'HTTPSConnection',server):
            with self.assertRaises(ValueError): recovery.fetch_static(artifact,Path(directory)/'asset',timeout=0)
            self.assertEqual(server.calls,[])

    def test_rejects_noncanonical_static_manifest_before_fetching_blobs(self):
        server = Server(); artifact, _ = static(server)
        path = '/v2/example/app/manifests/' + artifact['digest']
        body = server.responses[('ghcr.io', path)][0].getvalue() + b'\n'
        identity = r.digest(body)
        artifact.update(reference='ghcr.io/example/app@'+identity, digest=identity)
        server.document('/v2/example/app/manifests/'+identity, body)
        with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
            with self.assertRaisesRegex(ValueError, 'manifest differs'):
                recovery.fetch_static(artifact, Path(directory)/'asset')
        self.assertEqual(len(server.calls), 1)

    def test_private_no_replace_destination_validates_before_credentials_and_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root/'existing'; target.write_bytes(b'preserve')
            alias = root/'alias'; alias.symlink_to(root, target_is_directory=True)
            for path in [target, alias/'new', Path('relative')]:
                with self.subTest(path=path), patch.object(recovery, 'credentials') as auth, patch.object(recovery.http.client, 'HTTPSConnection') as network:
                    with self.assertRaises(ValueError): recovery.fetch_image({}, path)
                    auth.assert_not_called(); network.assert_not_called()
            self.assertEqual(target.read_bytes(), b'preserve')
            os.chmod(root, 0o755)
            with self.assertRaises(ValueError): recovery.fetch_static({}, root/'new')

    def test_hostile_blob_redirects_never_receive_authorization(self):
        for url in ['https://other.invalid/blob', 'http://pkg-containers.githubusercontent.com/blob',
                    'https://owner@pkg-containers.githubusercontent.com/blob', 'https://pkg-containers.githubusercontent.com:444/blob']:
            server = Server(); artifact, _ = static(server)
            key = ('ghcr.io', '/v2/example/app/blobs/' + artifact['blob_digest'])
            server.responses[key] = [Response(307, headers={'Location': url})]
            with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
                with self.assertRaisesRegex(ValueError, 'redirect'):
                    recovery.fetch_static(artifact, Path(directory)/'asset')
            self.assertTrue(all(call[0] == 'ghcr.io' for call in server.calls))

    def test_non_native_layer_descriptors_and_oversized_closures_refuse_before_blob_get(self):
        for layer in [{'mediaType': 'application/vnd.docker.image.rootfs.diff.tar.gzip', 'digest': 'sha256:'+'a'*64, 'size': 1},
                      {'mediaType': 'application/vnd.oci.image.layer.v1.tar', 'digest': 'sha256:'+'a'*64, 'size': recovery.MAX_BYTES},
                      {'mediaType': 'application/vnd.oci.image.layer.v1.tar', 'digest': 'sha256:'+'a'*64, 'size': 1, 'urls': ['https://other.invalid']}]:
            server=Server(); artifact,_,_,_=image(server,layers=[layer])
            with tempfile.TemporaryDirectory() as directory, patch.object(recovery.http.client, 'HTTPSConnection', server):
                with self.assertRaises(ValueError): recovery.fetch_image(artifact,Path(directory)/'image')
            self.assertEqual(len(server.calls),1)

    def test_authfile_helpers_and_environment_authority_are_not_selected(self):
        server=Server(); artifact,_=static(server)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); auth=root/'auth.json'; auth.write_text('{"credsStore":"arbitrary","auths":{}}'); auth.chmod(0o600)
            with patch.object(recovery.http.client,'HTTPSConnection',server):
                with self.assertRaises(ValueError): recovery.fetch_static(artifact,root/'asset',authfile=auth)
            self.assertEqual(server.calls,[])
            with patch.dict(os.environ,{'REGISTRY_AUTH_FILE':str(auth),'DOCKER_CONFIG':str(root)}), patch.object(recovery.http.client,'HTTPSConnection',server):
                recovery.fetch_static(artifact,root/'asset')
            self.assertTrue(all('Authorization' not in call[3] for call in server.calls))
