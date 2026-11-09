import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit


SPEC = importlib.util.spec_from_file_location('oci_registry', Path(__file__).resolve().parents[1] / 'helpers/oci_registry.py')
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


class MemoryRegistry(registry.Registry):
    def __init__(self):
        super().__init__('ghcr.io', 'example/mailroom-web')
        self.blobs = {}
        self.manifests = {}
        self.calls = []
        self.location = '/v2/example/mailroom-web/blobs/uploads/owned'

    def _send(self, method, path, headers, body=b'', size=None):
        self.calls.append((method, path))
        parsed = urlsplit(path)
        if '/blobs/uploads/' in parsed.path or '/blobs/upload/' in parsed.path:
            if method == 'POST':
                return 202, {'location': self.location}, b''
            self.assert_upload(method == 'PUT')
            raw = body.read() if hasattr(body, 'read') else body
            identity = parse_qs(parsed.query)['digest'][0]
            if registry.digest(raw) != identity or len(raw) != size:
                return 400, {}, b''
            self.blobs[identity] = raw
            return 201, {'docker-content-digest': identity}, b''
        identity = parsed.path.rsplit('/', 1)[-1]
        if '/blobs/' in parsed.path:
            return (200, {'docker-content-digest': identity}, b'') if identity in self.blobs else (404, {}, b'')
        if '/manifests/' in parsed.path:
            if method == 'GET':
                return ((200, {'docker-content-digest': identity}, self.manifests[identity])
                        if identity in self.manifests else (404, {}, b''))
            self.assert_upload(method == 'PUT' and registry.digest(body) == identity)
            self.manifests[identity] = body
            return 201, {'docker-content-digest': identity}, b''
        raise AssertionError('unexpected registry request')

    @staticmethod
    def assert_upload(condition):
        if not condition:
            raise AssertionError('invalid upload protocol')


class ArtifactPublication(unittest.TestCase):
    def setUp(self):
        self.payload = b'closed static application bytes'
        self.manifest = registry.artifact_manifest(registry.digest(self.payload), len(self.payload))
        self.artifact = {'kind': 'archive', 'reference': 'ghcr.io/example/mailroom-web@' + registry.digest(self.manifest),
                         'digest': registry.digest(self.payload), 'size_bytes': len(self.payload),
                         'media_type': registry.STATIC_LAYER}

    def test_exact_blob_and_artifact_manifest_are_published_without_an_engine(self):
        remote = MemoryRegistry()
        self.assertEqual(remote.publish_static(self.artifact, io.BytesIO(self.payload)), self.artifact['reference'])
        self.assertEqual(remote.blobs, {registry.digest(b'{}'): b'{}', registry.digest(self.payload): self.payload})
        self.assertEqual(remote.manifests, {registry.digest(self.manifest): self.manifest})
        self.assertEqual(json.loads(self.manifest), {
            'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'artifactType': 'application/vnd.komizo.static.v1',
            'config': {'mediaType': 'application/vnd.oci.empty.v1+json',
                       'digest': 'sha256:' + hashlib.sha256(b'{}').hexdigest(), 'size': 2},
            'layers': [{'mediaType': 'application/vnd.komizo.static.v1.tar+gzip',
                        'digest': 'sha256:' + hashlib.sha256(self.payload).hexdigest(), 'size': len(self.payload)}],
        })

    def test_already_published_exact_bytes_do_not_repeat_mutations(self):
        remote = MemoryRegistry()
        remote.publish_static(self.artifact, io.BytesIO(self.payload))
        remote.calls.clear()
        remote.publish_static(self.artifact, io.BytesIO(self.payload))
        self.assertTrue(all(method in {'HEAD', 'GET'} for method, _ in remote.calls))

    def test_ghcr_singular_upload_location_preserves_opaque_query_and_exact_content(self):
        query = 'state=opaque%2fvalue%3d&empty=&literal=one+two&repeat=1&repeat=2'
        for prefix in ['/v2/example/mailroom-web/blobs/upload/',
                       'https://ghcr.io/v2/example/mailroom-web/blobs/upload/',
                       'https://ghcr.io:443/v2/example/mailroom-web/blobs/upload/']:
            remote = MemoryRegistry()
            remote.location = prefix + 'owned?' + query
            with self.subTest(prefix=prefix):
                self.assertEqual(remote.publish_static(self.artifact, io.BytesIO(self.payload)), self.artifact['reference'])
                uploads = [path for method, path in remote.calls if method == 'PUT' and '/blobs/upload/' in path]
                self.assertEqual(len(uploads), 2)
                for path in uploads:
                    self.assertTrue(path.startswith('/v2/example/mailroom-web/blobs/upload/owned?' + query + '&digest='))
                    self.assertEqual(len(parse_qs(urlsplit(path).query)['digest']), 1)
                self.assertEqual(remote.blobs[registry.digest(self.payload)], self.payload)

    def test_generic_registry_plural_upload_preserves_the_original_query(self):
        remote = MemoryRegistry()
        remote.registry = 'registry.example'
        self.assertEqual(remote._upload_path('/v2/example/mailroom-web/blobs/uploads/owned?state=a%2fb%3d',
                                             registry.digest(self.payload)),
                         '/v2/example/mailroom-web/blobs/uploads/owned?state=a%2fb%3d&digest=sha256%3A'
                         + hashlib.sha256(self.payload).hexdigest())

    def test_singular_upload_is_not_granted_to_other_registry_authorities(self):
        for authority in ['registry.example', 'ghcr.io:444']:
            remote = MemoryRegistry()
            remote.registry = authority
            remote.location = '/v2/example/mailroom-web/blobs/upload/owned'
            with self.subTest(authority=authority), self.assertRaises(registry.RegistryError):
                remote.publish_static(self.artifact | {'reference': self.artifact['reference'].replace('ghcr.io', authority)},
                                      io.BytesIO(self.payload))
            self.assertFalse(any(method == 'PUT' for method, _ in remote.calls))

    def test_changed_payload_or_manifest_refuses_before_registry_effects(self):
        for artifact, body in [(self.artifact, self.payload + b'changed'),
                               (self.artifact | {'reference': 'ghcr.io/example/mailroom-web@sha256:' + '0' * 64}, self.payload),
                               (self.artifact | {'size_bytes': True}, self.payload),
                               (self.artifact | {'reference': self.artifact['reference'].replace('example/', 'another/')}, self.payload),
                               (self.artifact | {'extra': 'unexpected'}, self.payload)]:
            remote = MemoryRegistry()
            with self.subTest(artifact=artifact), self.assertRaises(registry.RegistryError):
                remote.publish_static(artifact, io.BytesIO(body))
            self.assertFalse(remote.calls)

    def test_upload_location_cannot_move_credentials_to_another_origin_or_repository(self):
        for location in ['https://foreign.example/v2/example/mailroom-web/blobs/uploads/owned',
                         'http://ghcr.io/v2/example/mailroom-web/blobs/uploads/owned',
                         '/v2/another/application/blobs/uploads/owned',
                         '/v2/example/mailroom-web/blobs/uploads/../manifests/owned',
                         '/v2/example/mailroom-web/blobs/uploads/owned?digest=sha256:chosen',
                         'https://foreign.example/v2/example/mailroom-web/blobs/upload/owned',
                         'https://ghcr.io:444/v2/example/mailroom-web/blobs/upload/owned',
                         'https://name:secret@ghcr.io/v2/example/mailroom-web/blobs/upload/owned',
                         '/v2/another/application/blobs/upload/owned',
                         '/v2/example/mailroom-web-other/blobs/upload/owned',
                         '/v2/example/mailroom-web/blobs/upload/',
                         '/v2/example/mailroom-web/blobs/upload/../manifests/owned',
                         '/v2/example/mailroom-web/blobs/upload/unused/../owned',
                         '/v2/example/mailroom-web/blobs/uploads/unused/../owned',
                         'https://ghcr.io/v2/example/mailroom-web/blobs/upload/./owned',
                         '/v2/example/mailroom-web/blobs/upload/%2e%2e/manifests/owned',
                         '/v2/example/mailroom-web/blobs/upload/owned#fragment',
                         '/v2/example/mailroom-web/blobs/upload/owned?%64igest=sha256:chosen',
                         '/v2/example/mailroom-web/blobs/upload/owned?state=line\nbreak']:
            remote = MemoryRegistry()
            remote.location = location
            with self.subTest(location=location), self.assertRaises(registry.RegistryError):
                remote.publish_static(self.artifact, io.BytesIO(self.payload))
            self.assertFalse(any(method == 'PUT' for method, _ in remote.calls))

    def test_existing_corrupt_manifest_is_never_overwritten(self):
        remote = MemoryRegistry()
        remote.manifests[registry.digest(self.manifest)] = b'changed'
        with self.assertRaisesRegex(registry.RegistryError, 'existing registry manifest'):
            remote.put_manifest(registry.digest(self.manifest), self.manifest)
        self.assertFalse(any(method == 'PUT' for method, _ in remote.calls))

    def test_malformed_or_mutable_registry_references_are_refused(self):
        for value in ['ghcr.io/example/mailroom-web:latest', 'https://ghcr.io/example/mailroom-web@sha256:' + 'a' * 64,
                      'ghcr.io/example/../private@sha256:' + 'a' * 64,
                      'ghcr.io/example/mailroom-web@sha256:' + 'A' * 64,
                      'ghcr.io:65536/example/mailroom-web@sha256:' + 'a' * 64]:
            with self.subTest(value=value), self.assertRaises(registry.RegistryError):
                registry.reference(value)


class Authentication(unittest.TestCase):
    def test_only_same_origin_exact_repository_token_authority_can_receive_credentials(self):
        remote = MemoryRegistry()
        for challenge in [
                'Basic realm="registry"',
                'Bearer realm="https://foreign.example/token",service="ghcr.io"',
                'Bearer realm="http://ghcr.io/token",service="ghcr.io"',
                'Bearer realm="https://ghcr.io/token",service="foreign.example"',
                'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:another/app:pull,push"',
                'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="registry:catalog:*"',
                'Bearer realm="https://ghcr.io/token?scope=other",service="ghcr.io"',
                'Bearer realm="https://ghcr.io/token",realm="https://foreign.example/token",service="ghcr.io"']:
            with self.subTest(challenge=challenge), self.assertRaises(registry.RegistryError):
                remote._authenticate(challenge)
            self.assertFalse(remote.calls)

    def test_pull_challenge_uses_the_callers_exact_repository_push_scope(self):
        class AuthRegistry(MemoryRegistry):
            def _send(self, method, path, headers, body=b'', size=None):
                self.calls.append((method, path))
                if method != 'GET' or path != '/token?service=ghcr.io&scope=repository%3Aexample%2Fmailroom-web%3Apull%2Cpush':
                    raise AssertionError('token scope widened or selected another authority')
                return 200, {}, b'{"token":"bounded-token"}'
        remote = AuthRegistry()
        remote._authenticate('Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:example/mailroom-web:pull"')
        self.assertEqual(remote._token, 'bounded-token')

    def test_unknown_json_duplicates_are_refused(self):
        with self.assertRaises(registry.RegistryError):
            json.loads('{"token":"first","token":"second"}', object_pairs_hook=registry.unique_object)


if __name__ == '__main__':
    unittest.main()
