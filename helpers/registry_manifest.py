"""Copy an already-verified config manifest to its verified source tag, byte-for-byte."""

import base64
import hashlib
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from godot_release_record import DIGEST, REPO


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Authenticated registry requests must not redirect')


def main():
    opener = build_opener(NoRedirect())
    ref = os.environ['CONFIG_REF']
    prefix = f'ghcr.io/{REPO}-config@'
    if not ref.startswith(prefix) or not DIGEST.fullmatch(ref[len(prefix):]):
        raise ValueError('Unexpected config digest')
    # Host admission permits only protected current/previous source receipts.
    # Preserve the verified source identity instead of creating an unsigned alias.
    tag = os.environ.get('RESTORE_REVISION', '')
    if not re.fullmatch(r'[a-f0-9]{40}', tag):
        raise ValueError('Rollback needs its verified full source revision')
    repository = REPO + '-config'
    credentials = base64.b64encode((os.environ['GITHUB_ACTOR'] + ':' + os.environ['GH_TOKEN']).encode()).decode()
    query = urlencode({'service': 'ghcr.io', 'scope': f'repository:{repository}:pull,push'})
    token_request = Request('https://ghcr.io/token?' + query, headers={'Authorization': 'Basic ' + credentials})
    with opener.open(token_request, timeout=30) as response:
        token_data = response.read(1024 * 1024 + 1)
    if len(token_data) > 1024 * 1024:
        raise ValueError('Oversized registry authentication response')
    token = json.loads(token_data)['token']
    types = ['application/vnd.oci.image.index.v1+json', 'application/vnd.oci.image.manifest.v1+json',
             'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.docker.distribution.manifest.v2+json']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': ', '.join(types)}
    endpoint = f'https://ghcr.io/v2/{repository}/manifests/'
    with opener.open(Request(endpoint + ref[len(prefix):], headers=headers), timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
        content_type = response.headers['Content-Type'].split(';')[0]
    if len(data) > 8 * 1024 * 1024 or content_type not in types or 'sha256:' + hashlib.sha256(data).hexdigest() != ref[len(prefix):]:
        raise ValueError('Registry manifest does not match the verified digest')
    request = Request(endpoint + tag, data=data, method='PUT', headers={**headers, 'Content-Type': content_type})
    with opener.open(request, timeout=30) as response:
        if response.status != 201 or response.headers.get('Docker-Content-Digest') != ref[len(prefix):]:
            raise ValueError('Rollback tag did not preserve the exact manifest digest')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'tag={tag}\n')


if __name__ == '__main__':
    main()
