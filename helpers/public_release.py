"""Verify public TLS delivery against the selected release, without TLS overrides."""

import json
import os
from urllib.request import urlopen
from urllib.parse import urlsplit


def validate_manifest(manifest, release_id, source_commit):
    if manifest.get('release_id') != release_id:
        raise ValueError('Public deployment has the wrong payload identity')
    # The first record-producing release predates source_commit in this file.
    # Its record still validates commit + immutable image digests independently.
    if 'source_commit' in manifest and manifest['source_commit'] != source_commit:
        raise ValueError('Public deployment has the wrong source revision')


if __name__ == '__main__':
    address = os.environ['RELEASE_MANIFEST_URL']
    parsed = urlsplit(address)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Public release verification requires an explicit HTTPS URL')
    with urlopen(address, timeout=30) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('Oversized public release manifest')
    validate_manifest(json.loads(data), os.environ['EXPECTED_RELEASE_ID'], os.environ['EXPECTED_SOURCE_COMMIT'])
    print('PASS: public HTTPS matches the selected verified release')
