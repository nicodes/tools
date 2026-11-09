#!/usr/bin/env python3
"""Publish exact OCI blobs and manifests without an engine or credential files."""
import base64
import hashlib
import http.client
import io
import json
import re
import time
import urllib.parse


MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
EMPTY_TYPE = 'application/vnd.oci.empty.v1+json'
STATIC_TYPE = 'application/vnd.komizo.static.v1'
STATIC_LAYER = STATIC_TYPE + '.tar+gzip'
EMPTY_CONFIG = b'{}'
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
MAX_DOCUMENT = 4 << 20
MAX_BLOB = 8 << 30
CHUNK = 1 << 20


class RegistryError(ValueError):
    """A bounded refusal that never includes a credential or response body."""


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RegistryError('duplicate JSON field')
        value[key] = item
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(body):
    return 'sha256:' + hashlib.sha256(body).hexdigest()


def artifact_manifest(blob_digest, size_bytes):
    if not isinstance(blob_digest, str) or not DIGEST.fullmatch(blob_digest):
        raise RegistryError('artifact requires a SHA256 payload identity')
    if type(size_bytes) is not int or not 0 < size_bytes <= MAX_BLOB:
        raise RegistryError('artifact requires a positive bounded size')
    return canonical({
        'schemaVersion': 2,
        'mediaType': MANIFEST_TYPE,
        'artifactType': STATIC_TYPE,
        'config': {'mediaType': EMPTY_TYPE, 'digest': digest(EMPTY_CONFIG), 'size': len(EMPTY_CONFIG)},
        'layers': [{'mediaType': STATIC_LAYER, 'digest': blob_digest, 'size': size_bytes}],
    })


def repository_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 255
            and re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', value) is not None
            and all(part not in {'', '.', '..'} for part in value.split('/')))


def reference(value):
    if not isinstance(value, str) or value.count('@') != 1:
        raise RegistryError('publication requires an immutable registry reference')
    location, identity = value.split('@')
    registry, separator, repository = location.partition('/')
    if (not separator or not DIGEST.fullmatch(identity) or not repository_name(repository)
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?', registry)
            or '..' in registry or registry.startswith('-')):
        raise RegistryError('publication requires a valid immutable registry reference')
    port = registry.rpartition(':')[2] if ':' in registry else '443'
    if not 1 <= int(port) <= 65535:
        raise RegistryError('registry port is outside its range')
    return registry, repository, identity


def _blob(reader, expected, length):
    if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
        raise RegistryError('blob requires a SHA256 identity')
    if type(length) is not int or not 0 <= length <= MAX_BLOB:
        raise RegistryError('blob size exceeds its bound')
    try:
        reader.seek(0)
        actual = hashlib.sha256()
        remaining = length
        while remaining:
            block = reader.read(min(CHUNK, remaining))
            if not isinstance(block, bytes) or not block or len(block) > remaining:
                raise RegistryError('blob is shorter than its declared size')
            actual.update(block)
            remaining -= len(block)
        if reader.read(1) or 'sha256:' + actual.hexdigest() != expected:
            raise RegistryError('blob bytes differ from their admitted identity')
        reader.seek(0)
    except (OSError, ValueError) as error:
        if isinstance(error, RegistryError):
            raise
        raise RegistryError('blob source cannot be read safely') from None


class Registry:
    def __init__(self, registry, repository, *, username='', password='', timeout=900, actions=('pull', 'push')):
        reference(registry + '/' + repository + '@sha256:' + '0' * 64)
        if (bool(username) != bool(password) or not isinstance(username, str)
                or not isinstance(password, str) or ':' in username
                or any(char in username + password for char in '\r\n\0')
                or len(username) > 256 or len(password) > 32768
                or type(timeout) is not int or not 1 <= timeout <= 2400):
            raise RegistryError('registry authority or deadline is invalid')
        if (not isinstance(actions, (list, tuple)) or not actions
                or any(action not in {'pull', 'push'} for action in actions)
                or len(set(actions)) != len(actions)):
            raise RegistryError('registry permission scope is invalid')
        self._actions = tuple(actions)
        self.registry = registry
        self.repository = repository
        self._username = username
        self._password = password
        self._token = ''
        self._deadline = time.monotonic() + timeout
        self._prefix = '/v2/' + repository

    def _timeout(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RegistryError('registry operation exceeded its deadline')
        return min(remaining, 30)

    def _send(self, method, path, headers, body=b'', size=None):
        if not path.startswith('/') or any(char in path for char in '\r\n\0'):
            raise RegistryError('registry request path is invalid')
        connection = http.client.HTTPSConnection(self.registry, timeout=self._timeout())
        try:
            if isinstance(body, bytes):
                connection.request(method, path, body=body, headers=headers)
            else:
                if type(size) is not int or not 0 <= size <= MAX_BLOB:
                    raise RegistryError('stream size exceeds its bound')
                body.seek(0)
                connection.putrequest(method, path)
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.putheader('Content-Length', str(size))
                connection.endheaders()
                remaining = size
                while remaining:
                    self._timeout()
                    block = body.read(min(CHUNK, remaining))
                    if not isinstance(block, bytes) or not block:
                        raise RegistryError('blob changed during upload')
                    connection.send(block)
                    remaining -= len(block)
                if body.read(1):
                    raise RegistryError('blob changed during upload')
            response = connection.getresponse()
            chunks = []
            length = 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(self._timeout())
                block = response.read1(min(CHUNK, MAX_DOCUMENT + 1 - length))
                if not block:
                    break
                length += len(block)
                if length > MAX_DOCUMENT:
                    raise RegistryError('registry response exceeds its bound')
                chunks.append(block)
            value = b''.join(chunks)
            return response.status, {key.lower(): value for key, value in response.getheaders()}, value
        except (OSError, http.client.HTTPException):
            raise RegistryError('registry exchange failed; inspect publication before retrying') from None
        finally:
            connection.close()

    def _authenticate(self, challenge):
        if not challenge.startswith('Bearer '):
            raise RegistryError('registry requires an unsupported authentication scheme')
        if not re.fullmatch(r'[a-zA-Z_]+="[^"\\]*"(?:\s*,\s*[a-zA-Z_]+="[^"\\]*")*', challenge[7:]):
            raise RegistryError('registry authentication challenge is invalid')
        fields = unique_object(re.findall(r'([a-zA-Z_]+)="([^"\\]*)"', challenge[7:]))
        if not {'realm', 'service'} <= set(fields) <= {'realm', 'service', 'scope'}:
            raise RegistryError('registry authentication challenge is invalid')
        realm = urllib.parse.urlsplit(fields['realm'])
        expected = urllib.parse.urlsplit('https://' + self.registry)
        scope = 'repository:' + self.repository + ':' + ','.join(self._actions)
        advertised = fields.get('scope', scope).split(':')
        if (realm.scheme != 'https' or realm.hostname != expected.hostname
                or (realm.port or 443) != (expected.port or 443)
                or realm.username or realm.password or realm.fragment
                or fields['service'] != self.registry
                or len(advertised) != 3 or advertised[:2] != ['repository', self.repository]
                or not set(advertised[2].split(',')) <= {'pull', 'push'}
                or not realm.path.startswith('/')):
            raise RegistryError('registry authentication cannot leave its approved authority')
        query = urllib.parse.parse_qsl(realm.query, keep_blank_values=True)
        if any(key in {'service', 'scope'} for key, _ in query):
            raise RegistryError('registry authentication query is ambiguous')
        path = realm.path + '?' + urllib.parse.urlencode(query + [('service', self.registry), ('scope', scope)])
        headers = {'Accept': 'application/json'}
        if self._password:
            encoded = base64.b64encode((self._username + ':' + self._password).encode()).decode()
            headers['Authorization'] = 'Basic ' + encoded
        status, _, body = self._send('GET', path, headers)
        if status != 200:
            raise RegistryError('registry authentication refused')
        try:
            data = json.loads(body, object_pairs_hook=unique_object)
            token = data.get('token', data.get('access_token'))
            if (not isinstance(token, str) or not 0 < len(token) <= 32768
                    or any(char in token for char in '\r\n\0')
                    or ('token' in data and 'access_token' in data and data['token'] != data['access_token'])):
                raise RegistryError('registry returned an invalid authorization token')
        except (ValueError, AttributeError):
            raise RegistryError('registry returned invalid authorization data') from None
        self._token = token

    def _request(self, method, path, headers=None, body=b'', size=None):
        headers = dict(headers or {})
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        result = self._send(method, path, headers, body, size)
        if result[0] == 401:
            self._authenticate(result[1].get('www-authenticate', ''))
            headers['Authorization'] = 'Bearer ' + self._token
            result = self._send(method, path, headers, body, size)
        if 300 <= result[0] < 400:
            raise RegistryError('registry redirects are not allowed')
        return result

    def _upload_path(self, location, expected_digest):
        if (not isinstance(location, str) or not 0 < len(location) <= 32768
                or any(ord(char) < 32 or ord(char) == 127 for char in location)):
            raise RegistryError('registry upload location is invalid')
        # urljoin normalizes dot segments. Refuse them in the advertised path
        # before resolving a relative location, including in-scope traversals.
        if any(part in {'.', '..'} for part in urllib.parse.urlsplit(location).path.split('/')):
            raise RegistryError('blob upload cannot leave its approved repository')
        target = urllib.parse.urlsplit(urllib.parse.urljoin('https://' + self.registry, location))
        origin = urllib.parse.urlsplit('https://' + self.registry)
        prefixes = [self._prefix + '/blobs/uploads/']
        if origin.hostname == 'ghcr.io' and (origin.port or 443) == 443:
            prefixes.append(self._prefix + '/blobs/upload/')
        if (target.scheme != 'https' or target.hostname != origin.hostname
                or (target.port or 443) != (origin.port or 443)
                or target.username or target.password or target.fragment
                or not any(target.path.startswith(prefix) and len(target.path) > len(prefix)
                           for prefix in prefixes)
                or '%' in target.path or '\\' in target.path
                or any(part in {'.', '..'} for part in target.path.split('/'))):
            raise RegistryError('blob upload cannot leave its approved repository')
        query = urllib.parse.parse_qsl(target.query, keep_blank_values=True)
        if any(key == 'digest' for key, _ in query):
            raise RegistryError('registry upload location selects its own digest')
        # The registry's query may carry signed opaque state. Preserve its
        # original bytes; only the caller-selected digest is appended.
        return (target.path + '?' + (target.query + '&' if target.query else '')
                + urllib.parse.urlencode([('digest', expected_digest)]))

    def upload_blob(self, blob_digest, size_bytes, reader):
        _blob(reader, blob_digest, size_bytes)
        status, headers, _ = self._request('HEAD', self._prefix + '/blobs/' + blob_digest)
        if status == 200:
            if headers.get('docker-content-digest', blob_digest) != blob_digest:
                raise RegistryError('registry reports a different blob identity')
            return
        if status != 404:
            raise RegistryError('registry blob inspection refused')
        status, headers, _ = self._request('POST', self._prefix + '/blobs/uploads/')
        if status != 202 or not headers.get('location'):
            raise RegistryError('registry did not admit a private blob upload')
        path = self._upload_path(headers['location'], blob_digest)
        status, headers, _ = self._request('PUT', path,
            {'Content-Type': 'application/octet-stream'}, reader, size_bytes)
        _blob(reader, blob_digest, size_bytes)
        if status != 201 or headers.get('docker-content-digest', blob_digest) != blob_digest:
            raise RegistryError('registry did not accept the exact blob')

    def put_manifest(self, expected_digest, raw, media_type=MANIFEST_TYPE):
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DOCUMENT
                or not DIGEST.fullmatch(expected_digest) or digest(raw) != expected_digest
                or media_type != MANIFEST_TYPE):
            raise RegistryError('manifest bytes differ from their admitted identity')
        status, headers, body = self._request('GET', self._prefix + '/manifests/' + expected_digest,
            {'Accept': media_type})
        if status == 200:
            if body != raw or headers.get('docker-content-digest', expected_digest) != expected_digest:
                raise RegistryError('existing registry manifest differs from the accepted bytes')
            return self.registry + '/' + self.repository + '@' + expected_digest
        if status != 404:
            raise RegistryError('registry manifest inspection refused')
        status, headers, _ = self._request('PUT', self._prefix + '/manifests/' + expected_digest,
            {'Content-Type': media_type}, raw)
        if status != 201 or headers.get('docker-content-digest', expected_digest) != expected_digest:
            raise RegistryError('registry did not accept the exact manifest')
        return self.registry + '/' + self.repository + '@' + expected_digest

    def publish_static(self, artifact, reader):
        if (not isinstance(artifact, dict)
                or set(artifact) != {'kind', 'reference', 'digest', 'size_bytes', 'media_type'}
                or artifact['kind'] != 'archive' or artifact['media_type'] != STATIC_LAYER):
            raise RegistryError('static publication requires its complete archive declaration')
        registry, repository, expected = reference(artifact['reference'])
        if registry != self.registry or repository != self.repository:
            raise RegistryError('static publication differs from the authorized repository')
        raw = artifact_manifest(artifact['digest'], artifact['size_bytes'])
        if digest(raw) != expected:
            raise RegistryError('static manifest differs from the accepted release')
        _blob(reader, artifact['digest'], artifact['size_bytes'])
        self.upload_blob(digest(EMPTY_CONFIG), len(EMPTY_CONFIG), io.BytesIO(EMPTY_CONFIG))
        self.upload_blob(artifact['digest'], artifact['size_bytes'], reader)
        return self.put_manifest(expected, raw)
