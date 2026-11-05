import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { prepareMetro } from '../helpers/prepare-metro.mjs';

async function fixture(handler, run) {
  const server = createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await run(`http://127.0.0.1:${server.address().port}`); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('readiness waits for the complete cold JavaScript bundle', async () => {
  let completed = false;
  await fixture((request, response) => {
    if (request.url === '/') {
      response.end('<script src="/entry.bundle?platform=web&amp;lazy=true" defer></script>');
    } else {
      assert.equal(request.url, '/entry.bundle?platform=web&lazy=true');
      response.writeHead(200, { 'Content-Type': 'application/javascript' });
      response.write('/* compiling */');
      setTimeout(() => { completed = true; response.end('globalThis.ready = true;'); }, 30);
    }
  }, async origin => {
    assert.equal(await prepareMetro(origin), 1);
    assert.equal(completed, true);
  });
});

test('bad, missing, empty, and cross-origin bundles fail instead of masking an error', async () => {
  for (const mode of ['missing', 'external', 'failed', 'html', 'empty']) {
    await fixture((request, response) => {
      if (request.url === '/') {
        response.end(mode === 'missing' ? '<html></html>' : `<script src="${mode === 'external' ? 'https://example.invalid' : ''}/entry.bundle"></script>`);
      } else if (mode === 'failed') {
        response.writeHead(500).end('compile failure');
      } else {
        response.writeHead(200, { 'Content-Type': mode === 'html' ? 'text/html' : 'text/javascript' });
        response.end(mode === 'empty' ? '' : '<html>not JavaScript</html>');
      }
    }, origin => assert.rejects(prepareMetro(origin)));
  }
});

test('a stalled compiler remains bounded by the caller deadline', async () => {
  await fixture((request, response) => {
    if (request.url === '/') response.end('<script src="/entry.bundle"></script>');
    // Deliberately never finish the bundle response.
  }, origin => assert.rejects(prepareMetro(origin, '/', AbortSignal.timeout(100))));
});

test('the caller selects its native entry route', async () => {
  const requests = [];
  await fixture((request, response) => {
    requests.push(request.url);
    if (request.url === '/workspace') response.end('<script src="/entry.bundle"></script>');
    else response.writeHead(200, { 'Content-Type': 'text/javascript' }).end('globalThis.ready = true;');
  }, async origin => {
    assert.equal(await prepareMetro(origin, '/workspace'), 1);
    assert.deepEqual(requests, ['/workspace', '/entry.bundle']);
  });
});

test('unsafe entry paths refuse before any local or external request', async () => {
  let requests = 0;
  await fixture((request, response) => { requests++; response.end('unexpected'); }, async origin => {
    for (const entry of ['https://example.invalid/', '//example.invalid/', '/a/../b', '/a?key=value',
      '/a#fragment', '/a\\b', '/line\nfeed', '', '/' + 'x'.repeat(2048)]) {
      await assert.rejects(prepareMetro(origin, entry));
    }
    assert.equal(requests, 0);
  });
});
