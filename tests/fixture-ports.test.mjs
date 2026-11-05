import assert from 'node:assert/strict';
import test from 'node:test';
import { uniquePortAllocator } from '../helpers/fixture-ports.mjs';

test('a released API port cannot also become the fixture app port', async () => {
  const candidates = ['40747', '40748', '40747', '40749', '40749', '40750'];
  const port = uniquePortAllocator(async () => candidates.shift());
  const api = await port();
  const auth = await port();
  const app = await port();
  const github = await port();
  assert.deepEqual([api, auth, app, github], ['40747', '40748', '40749', '40750']);
});

test('a stuck allocator fails in bounded time instead of hanging the gate', async () => {
  let calls = 0;
  const port = uniquePortAllocator(async () => { calls++; return '40747'; });
  await port();
  await assert.rejects(port(), /distinct fixture port/);
  assert(calls < 100);
});

test('separate fixtures use independent allocation histories', async () => {
  const first = uniquePortAllocator(async () => '40747');
  const second = uniquePortAllocator(async () => '40747');
  assert.equal(await first(), await second());
});
