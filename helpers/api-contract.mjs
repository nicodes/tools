import assert from 'node:assert/strict';

/** Exercise actual routing, middleware and readiness at an isolated API. */
export async function verifyApiContract(base) {
  for (const path of ['/healthz', '/readyz']) {
    const response = await fetch(base + path, { signal: AbortSignal.timeout(10000) });
    assert.equal(response.status, 200, path + ': ' + await response.clone().text());
    const value = await response.json();
    assert.equal(typeof value.revision, 'string');
    assert(value.revision.length > 0);
    assert.match(response.headers.get('x-request-id'), /^[a-f0-9]{32}$/);
    const head = await fetch(base + path, {method:'HEAD', signal:AbortSignal.timeout(10000)});
    assert.equal(head.status, 200, 'HEAD ' + path);
    assert.equal(await head.text(), '');
  }
  const response = await fetch(base + '/__contract_missing_route', {
    headers:{'X-Request-ID':'caller-controlled'}, signal:AbortSignal.timeout(10000),
  });
  assert.equal(response.status, 404);
  const value = await response.json();
  assert.match(value.error.code, /^[a-z0-9_.-]+$/);
  assert.equal(typeof value.error.message, 'string');
  assert(value.error.message.length <= 1024);
  assert.match(value.error.request_id, /^[a-f0-9]{32}$/);
  assert.equal(value.error.request_id, response.headers.get('x-request-id'));
  assert(!('message' in value));
  assert(!('error_message' in value));
}
