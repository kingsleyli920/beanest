'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createPowerGuard, readOwnedActivity } = require('./power-guard');

const connection = { url: 'http://127.0.0.1:8765/', token: 'example_local_token_not_a_secret_123',
  instance_id: '11111111-2222-4333-8444-555555555555' };

test('wake status reads only the owned loopback MCP instance and returns one boolean', async () => {
  const result = await readOwnedActivity(connection.url, connection, async (url, options) => {
    assert.equal(String(url), `${connection.url}api/mcp/status`);
    assert.equal(options.headers['X-LAC-Token'], connection.token);
    assert.equal(options.headers['X-LAC-Instance'], connection.instance_id);
    assert.equal(options.redirect, 'error');
    assert.ok(options.signal instanceof AbortSignal);
    return { ok: true, async json() { return { instance_id: connection.instance_id,
      autonomous_work_active: true, other_private_data: 'must not cross this boundary' }; } };
  });
  assert.deepEqual(result, { autonomous_work_active: true });
});

test('invalid loopback or changed connection cannot send a credential', async () => {
  let requests = 0;
  const send = async () => { requests += 1; throw new Error('unexpected request'); };
  for (const url of ['https://example.test/', 'http://127.0.0.1:8766/']) {
    await assert.rejects(readOwnedActivity(url, connection, send));
  }
  await assert.rejects(readOwnedActivity(connection.url, { ...connection, token: 'bad\nheader' }, send));
  await assert.rejects(readOwnedActivity(connection.url, { ...connection, instance_id: 'not-an-instance' }, send));
  assert.equal(requests, 0);
});

test('HTTP denial and wrong service instance cannot report active work', async () => {
  await assert.rejects(readOwnedActivity(connection.url, connection, async () => ({ ok: false })));
  await assert.rejects(readOwnedActivity(connection.url, connection, async () => ({ ok: true,
    async json() { return { instance_id: 'another-instance', autonomous_work_active: true }; } })));
});

function fixture(readStatus) {
  let now = 0;
  const calls = [];
  const timers = [];
  const guard = createPowerGuard({
    powerSaveBlocker: {
      start(type) { calls.push(['start', type]); return 0; },
      stop(id) { calls.push(['stop', id]); return true; },
    },
    readStatus, clock: () => now,
    schedule(fn, ms) { const timer = { fn, ms, unref() {} }; timers.push(timer); return timer; },
    unschedule(timer) { calls.push(['clear', timer]); },
  });
  return { guard, calls, timers, advance(ms) { now += ms; } };
}

test('only active objectives acquire a wake lock; pause and completion release it', async () => {
  let active = false;
  const f = fixture(async () => ({ autonomous_work_active: active }));
  await f.guard.refresh(); assert.deepEqual(f.calls, []);
  active = true;
  await f.guard.refresh(); await f.guard.refresh();
  assert.deepEqual(f.calls, [['start', 'prevent-app-suspension']]);
  active = false;
  await f.guard.refresh();
  assert.deepEqual(f.calls.at(-1), ['stop', 0]);
});

test('unavailable or malformed status cannot acquire a wake lock', async () => {
  let value = { autonomous_work_active: 'true' };
  const f = fixture(async () => value);
  await f.guard.refresh(); value = {}; await f.guard.refresh();
  assert.deepEqual(f.calls, []);
});

test('transient failures preserve a recent lock but a stale service releases it', async () => {
  let fail = false;
  const f = fixture(async () => { if (fail) throw new Error('offline'); return { autonomous_work_active: true }; });
  await f.guard.refresh(); fail = true; f.advance(59000); await f.guard.refresh();
  assert.equal(f.calls.length, 1);
  f.advance(1000); await f.guard.refresh();
  assert.deepEqual(f.calls.at(-1), ['stop', 0]);
});

test('one in-flight read is shared and cannot acquire after disposal', async () => {
  let resolve;
  let reads = 0;
  const f = fixture(() => { reads += 1; return new Promise((done) => { resolve = done; }); });
  const pending = f.guard.refresh(); await f.guard.refresh();
  assert.equal(reads, 1);
  f.guard.dispose(); resolve({ autonomous_work_active: true }); await pending;
  await f.guard.refresh(); assert.equal(reads, 1); assert.deepEqual(f.calls, []);
});

test('start and dispose are idempotent and clean the owned timer and lock', async () => {
  const f = fixture(async () => ({ autonomous_work_active: true }));
  f.guard.start(); f.guard.start(); await Promise.resolve();
  assert.equal(f.timers.length, 1); assert.equal(f.timers[0].ms, 15000);
  f.guard.dispose(); f.guard.dispose();
  assert.equal(f.calls.filter(([action]) => action === 'clear').length, 1);
  assert.equal(f.calls.filter(([action]) => action === 'stop').length, 1);
});
