import assert from 'node:assert/strict';
import fs from 'node:fs';

// The deployment manifest owns this data; the cookbook supplies only encoding.
const manifest = Bun.YAML.parse(fs.readFileSync('deploy/komizo.yml', 'utf8'));
assert.equal(manifest.compose, 'compose.yml');
const hosts = manifest.hostnames.map(host => {
  assert.match(host.name, /^(\*\.)?[a-z0-9][a-z0-9.-]+$/);
  assert.match(host.container, /^[a-z0-9][a-z0-9_-]*$/);
  assert.ok(!host.tls || ['auto', 'on-demand'].includes(host.tls));
  return `${host.name} -> ${host.container}${host.tls ? ` ${host.tls}` : ''}`;
});
assert.ok(hosts.length);
assert.equal(new Set(manifest.hostnames.map(host => host.name)).size, hosts.length);
fs.mkdirSync('.artifacts/config', { recursive: true });
fs.writeFileSync('.artifacts/config/hostnames', hosts.join('\n') + '\n');
