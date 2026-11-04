'use strict';

const URL_LINE = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/?$/;

function parseServerUrl(line) {
  const match = URL_LINE.exec(line.trim());
  if (!match) throw new Error('Room server returned an invalid loopback URL');
  const port = Number(match[1]);
  if (port > 65535) throw new Error('Room server returned an invalid loopback port');
  return `http://127.0.0.1:${port}/`;
}

function waitForExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener('exit', finish);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    child.once('exit', finish);
  });
}

async function stopOwnedChild(child, graceMs = 20000) {
  if (!child) return { exited: true, forced: false };
  if (child.exitCode !== null || child.signalCode !== null) {
    return { exited: true, forced: false };
  }
  child.kill('SIGTERM');
  await waitForExit(child, graceMs);
  if (child.exitCode !== null || child.signalCode !== null) {
    return { exited: true, forced: false };
  }
  child.kill('SIGKILL');
  await waitForExit(child, 1000);
  return {
    exited: child.exitCode !== null || child.signalCode !== null,
    forced: true,
  };
}

module.exports = { parseServerUrl, stopOwnedChild };
