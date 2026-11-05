// Bind prebuilt synthetic fixture origins to this test's owned loopback services.
import assert from 'node:assert/strict';

function origin(value) {
  const parsed = new URL(value);
  assert.equal(parsed.protocol, 'http:', 'fixture aliases require local HTTP');
  assert.equal(parsed.hostname, '127.0.0.1', 'fixture aliases require literal IPv4 loopback');
  assert(parsed.port && Number(parsed.port) >= 1024, 'fixture aliases require an explicit unprivileged port');
  assert.equal(parsed.href, parsed.origin + '/', 'fixture aliases must be bare origins without credentials or paths');
  return parsed.origin;
}

export async function bindFixtureOrigins(page, aliases) {
  assert(aliases && typeof aliases === 'object' && !Array.isArray(aliases), 'declare exact fixture origin bindings');
  const entries = Object.entries(aliases);
  assert(entries.length > 0 && entries.length <= 16, 'fixture aliases require a bounded nonempty map');
  // Validate every binding before installing any browser routing override.
  const bindings = entries.map(([from, to]) => [origin(from), origin(to)]);
  for (const [from, to] of bindings) {
    await page.route(from + '/**', async route => {
      const request = new URL(route.request().url());
      assert.equal(request.origin, from, 'fixture route left its declared synthetic origin');
      const response = await route.fetch({ url: to + request.pathname + request.search,
        maxRedirects: 0, timeout: 30_000 });
      assert(response.status() < 300 || response.status() >= 400, 'fixture origin transport refuses redirects');
      await route.fulfill({ response });
    });
  }
}
