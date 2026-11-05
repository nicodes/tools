import assert from 'node:assert/strict';
import test from 'node:test';
import { bindFixtureOrigins } from '../helpers/fixture-origins.mjs';

test('prebuilt fixture binds real local endpoint without compiling or changing its payload', async () => {
  let handler;
  await bindFixtureOrigins({ async route(pattern, callback) {
    assert.equal(pattern, 'http://127.0.0.1:18080/**'); handler = callback;
  } }, { 'http://127.0.0.1:18080': 'http://127.0.0.1:45678' });
  const response = { status: () => 200 };
  let accepted;
  await handler({ request: () => ({ url: () => 'http://127.0.0.1:18080/v1/me?hint=fixture' }),
    async fetch(options) {
      assert.deepEqual(options, { url: 'http://127.0.0.1:45678/v1/me?hint=fixture', maxRedirects: 0, timeout: 30_000 });
      return response;
    }, async fulfill(options) { accepted = options; } });
  assert.deepEqual(accepted, { response });
});

test('external, credentialled, privileged and path aliases cannot replace browser containment', async () => {
  for (const value of ['https://example.test', 'http://localhost:1234', 'http://127.0.0.1:80',
    'http://user:pass@127.0.0.1:1234', 'http://127.0.0.1:1234/path', 'http://127.0.0.1:1234/?secret=fixture']) {
    let calls = 0;
    await assert.rejects(bindFixtureOrigins({ async route() { calls++; } }, {
      'http://127.0.0.1:18080': 'http://127.0.0.1:45678', 'http://127.0.0.1:18081': value,
    }));
    assert.equal(calls, 0, 'every endpoint must be checked before installing routing');
  }
});

test('redirects and mismatched requests are refused before fulfillment', async () => {
  let handler;
  await bindFixtureOrigins({ async route(_, callback) { handler = callback; } }, {
    'http://127.0.0.1:18080': 'http://127.0.0.1:45678',
  });
  let fetches = 0, fulfillments = 0;
  const route = { request: () => ({ url: () => 'http://127.0.0.1:18080/v1/me' }),
    async fetch() { fetches++; return { status: () => 302 }; }, async fulfill() { fulfillments++; } };
  await assert.rejects(handler(route), /refuses redirects/);
  assert.equal(fetches, 1); assert.equal(fulfillments, 0);
  route.request = () => ({ url: () => 'http://127.0.0.1:18081/v1/me' });
  await assert.rejects(handler(route), /declared synthetic origin/);
  assert.equal(fetches, 1);
});
