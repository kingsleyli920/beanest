'use strict';

const { parseServerUrl } = require('./lifecycle');

async function readOwnedActivity(serverUrl, connection, fetchStatus = fetch) {
  if (parseServerUrl(serverUrl) !== serverUrl || connection?.url !== serverUrl
      || typeof connection.token !== 'string' || !/^[A-Za-z0-9_-]{20,200}$/.test(connection.token)
      || typeof connection.instance_id !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(connection.instance_id)) {
    throw new Error('wake_service_unavailable');
  }
  const response = await fetchStatus(new URL('api/mcp/status', serverUrl), {
    headers: { 'X-LAC-Token': connection.token, 'X-LAC-Instance': connection.instance_id },
    signal: AbortSignal.timeout(5000), redirect: 'error',
  });
  if (!response.ok) throw new Error('wake_service_unavailable');
  const status = await response.json();
  if (status.instance_id !== connection.instance_id) throw new Error('wake_service_unavailable');
  return { autonomous_work_active: status.autonomous_work_active };
}

// The service owns whether an autonomous objective is active. The desktop only
// holds a process-scoped wake lock; it never changes system power preferences.
function createPowerGuard({ powerSaveBlocker, readStatus, clock = Date.now,
  schedule = setInterval, unschedule = clearInterval, intervalMs = 15000, staleMs = 60000 }) {
  let blocker = null;
  let timer = null;
  let disposed = false;
  let refreshing = false;
  let lastConfirmedAt = -Infinity;

  function setActive(active) {
    if (active && blocker === null) blocker = powerSaveBlocker.start('prevent-app-suspension');
    if (!active && blocker !== null) {
      powerSaveBlocker.stop(blocker);
      blocker = null;
    }
  }

  async function refresh() {
    if (disposed || refreshing) return;
    refreshing = true;
    try {
      const status = await readStatus();
      if (typeof status?.autonomous_work_active !== 'boolean') throw new Error('wake_status_unavailable');
      if (!disposed) {
        lastConfirmedAt = clock();
        setActive(status.autonomous_work_active);
      }
    } catch {
      // A short local status failure must not immediately suspend active work.
      // A lost service cannot keep the computer awake indefinitely either.
      if (!disposed && clock() - lastConfirmedAt >= staleMs) setActive(false);
    } finally {
      refreshing = false;
    }
  }

  return {
    start() {
      if (disposed || timer !== null) return;
      timer = schedule(() => { void refresh(); }, intervalMs);
      timer.unref?.();
      void refresh();
    },
    refresh,
    dispose() {
      disposed = true;
      if (timer !== null) unschedule(timer);
      timer = null;
      setActive(false);
    },
  };
}

module.exports = { createPowerGuard, readOwnedActivity };
