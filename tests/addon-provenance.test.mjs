import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { assertMainRef, assertChecksum, assertReleaseIdentity, buildCandidate, renderNotes, assertNotes,
         assertGodotLog, FIXED_ZIP_STAMP } from '../helpers/addon-provenance.mjs';

const sha = 'a'.repeat(64);
const commit = 'b'.repeat(40);
const policy = { name: 'example-addon', candidate_schema: 'example.candidate.v1', zip_entries: 4,
  description: 'Native addon description.', archive: 'dist/example.zip', vendor_package: '@example/sdk',
  vendor_digest_field: 'browser_sha256', vendor_version_field: 'sdk_version', forbidden_tokens: ['private-consumer'] };
const info = { tag: 'v1.2.3', version: '1.2.3', commit, zipSha256: sha, vendorBrowserSha256: sha,
  entries: 4, vendorVersion: '6.0.0', integrity: 'sha512-source' };

test('main, release identity and checksum gates reject changed identities', () => {
  assert.doesNotThrow(() => assertMainRef({ ref: 'refs/heads/main', head: commit, originMain: commit }));
  assert.throws(() => assertMainRef({ ref: 'refs/heads/feature', head: commit, originMain: commit }));
  assert.throws(() => assertMainRef({ ref: 'refs/heads/main', head: commit, originMain: 'c'.repeat(40) }));
  assert.throws(() => assertChecksum(sha, 'c'.repeat(64)));
  assert.throws(() => assertReleaseIdentity({ tag: 'v9.9.9', version: info.version, commit }));
});

test('candidate schema and vendor field names come from owning source policy', () => {
  const candidate = buildCandidate(info, policy);
  assert.equal(candidate.sdk_version, '6.0.0');
  assert.equal(candidate.browser_sha256, sha);
  assert.equal(candidate.release, false);
  assert.equal(candidate.isolated_test_only, true);
  assert.throws(() => buildCandidate({ ...info, entries: 3 }, policy));
  assert.throws(() => buildCandidate({ ...info, version: 'private-consumer' }, policy));
  assert.throws(() => buildCandidate({ ...info, version: 'pk_test_secret' }, policy));
});

test('notes retain exact provenance and strict engine logs require reached success', () => {
  const notes = renderNotes(info, policy);
  assert.doesNotThrow(() => assertNotes(notes, info));
  assert.throws(() => assertNotes(notes.replace(sha, 'changed'), info));
  assert.throws(() => assertGodotLog('ok fixture\nSCRIPT ERROR: failure\n'));
  assert.throws(() => assertGodotLog('no reached assertion\n', { requireOk: true }));
  assert.throws(() => assertGodotLog('ok fixture\nWARNING: ObjectDB instances were leaked\n'));
  assert.doesNotThrow(() => assertGodotLog('ok fixture\n', { requireOk: true }));
});

test('archive epoch stays fixed across timezone environments', () => {
  assert.deepEqual(FIXED_ZIP_STAMP, { time: 0, day: 33 });
  const helper = new URL('../helpers/addon-provenance.mjs', import.meta.url).href;
  for (const TZ of ['UTC', 'Pacific/Auckland', 'America/Los_Angeles']) {
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', `import {FIXED_ZIP_STAMP as s} from ${JSON.stringify(helper)}; if(s.time!==0||s.day!==33)process.exit(1)`], { env: { ...process.env, TZ }, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
  }
});
