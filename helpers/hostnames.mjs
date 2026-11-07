import assert from 'node:assert/strict';
import fs from 'node:fs';
import {readSource} from './source-config.mjs';

// The deployment manifest owns this data; the cookbook supplies only encoding.
const manifest = readSource('deploy/komizo.toml', {maximum: 65536});
assert.ok(manifest && typeof manifest === 'object' && !Array.isArray(manifest));
assert.deepEqual(Object.keys(manifest).sort(), ['compose', 'hostnames']);
assert.equal(manifest.compose, 'compose.yml');
assert.ok(Array.isArray(manifest.hostnames) && manifest.hostnames.length <= 128);
const hosts = manifest.hostnames.map(host => {
  assert.ok(host && typeof host === 'object' && !Array.isArray(host));
  assert.ok(Object.keys(host).every(key => ['name', 'container', 'tls'].includes(key)));
  assert.equal(typeof host.name, 'string');
  assert.equal(typeof host.container, 'string');
  assert.match(host.name, /^(\*\.)?[a-z0-9][a-z0-9.-]+$/);
  assert.match(host.container, /^[a-z0-9][a-z0-9_-]*$/);
  assert.ok(!host.tls || ['auto', 'on-demand'].includes(host.tls));
  return `${host.name} -> ${host.container}${host.tls ? ` ${host.tls}` : ''}`;
});
assert.ok(hosts.length);
assert.equal(new Set(manifest.hostnames.map(host => host.name)).size, hosts.length);
fs.mkdirSync('.artifacts/config', { recursive: true });
fs.writeFileSync('.artifacts/config/hostnames', hosts.join('\n') + '\n');
