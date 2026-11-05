// A listening /status endpoint is not proof that Metro's cold entry bundle has
// compiled. Treat that compilation as setup, under the existing 15-minute build
// budget, rather than consuming the browser's 20-second interaction budget.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
function localOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash || value !== url.origin) {
    throw new Error('Metro requires a literal 127.0.0.1 HTTP origin with a port');
  }
  return url;
}


export async function prepareMetro(origin, entryPath = '/', signal = AbortSignal.timeout(900_000)) {
  const base = localOrigin(origin);
  assert(typeof entryPath === 'string' && entryPath.length > 0 && entryPath.length <= 2048
    && entryPath.startsWith('/') && !/[\\\s?#\x00-\x1f\x7f]/.test(entryPath),
    'Metro entry requires a bounded literal pathname');
  const entry = new URL(entryPath, base);
  assert(entry.origin === base.origin && entry.pathname === entryPath
    && entry.href === base.origin + entryPath,
    'Metro entry must stay on its declared local origin');
  const shell = await fetch(entry, { signal, redirect: 'error' });
  assert(shell.ok, `Metro HTML returned HTTP ${shell.status}`);
  const html = await shell.text();
  const sources = [...html.matchAll(/<script\b[^>]*\bsrc\s*=\s*(["'])(.*?)\1/gi)];
  const bundles = sources.map(match => new URL(match[2].replaceAll('&amp;', '&'), base))
    .filter(url => url.pathname.endsWith('.bundle'));
  assert(bundles.length > 0, 'Metro HTML did not reference an entry bundle');
  for (const url of bundles) {
    assert.equal(url.origin, base.origin, 'Metro entry bundle left the local server');
    const response = await fetch(url, { signal, redirect: 'error' });
    assert(response.ok, `Metro entry compilation returned HTTP ${response.status}`);
    assert(/(?:java|ecma)script/i.test(response.headers.get('content-type') ?? ''), 'Metro entry was not JavaScript');
    assert((await response.arrayBuffer()).byteLength > 0, 'Metro entry bundle was empty');
  }
  return bundles.length;
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  const count = await prepareMetro(process.argv[2], process.argv[3]);
  console.log(`Prepared ${count} live Metro entry bundle(s)`);
}
