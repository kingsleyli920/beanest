'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { parseServerUrl, stopOwnedChild } = require('./lifecycle');

test('accepts only an IPv4 loopback startup URL with a valid port', () => {
  assert.equal(parseServerUrl('http://127.0.0.1:42319\n'), 'http://127.0.0.1:42319/');
  for (const value of [
    'http://localhost:42319', 'http://127.0.0.2:42319', 'https://127.0.0.1:42319',
    'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:42319/path',
  ]) assert.throws(() => parseServerUrl(value));
});

test('sends TERM and waits for the owned process to exit', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.signals = [];
  child.kill = (signal) => {
    child.signals.push(signal);
    setImmediate(() => { child.exitCode = 0; child.emit('exit', 0, signal); });
  };
  const result = await stopOwnedChild(child, 50);
  assert.deepEqual(child.signals, ['SIGTERM']);
  assert.deepEqual(result, { exited: true, forced: false });
});

test('escalates to KILL when the owned process ignores TERM', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.signals = [];
  child.kill = (signal) => {
    child.signals.push(signal);
    if (signal === 'SIGKILL') setImmediate(() => { child.signalCode = signal; child.emit('exit', null, signal); });
  };
  const result = await stopOwnedChild(child, 5);
  assert.deepEqual(child.signals, ['SIGTERM', 'SIGKILL']);
  assert.deepEqual(result, { exited: true, forced: true });
});

test('reports when forced termination still cannot be confirmed', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.signals = [];
  child.kill = (signal) => { child.signals.push(signal); };
  const result = await stopOwnedChild(child, 1);
  assert.deepEqual(result, { exited: false, forced: true });
  assert.deepEqual(child.signals, ['SIGTERM', 'SIGKILL']);
});
