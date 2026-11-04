'use strict';

const fs = require('node:fs');
const path = require('node:path');
const roomHash = /^#room=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;

// Save only a chat identifier. Ports and authentication belong to each server
// instance, so persisting a complete URL would restore an obsolete connection.
function createChatLocation(filename) {
  function initialUrl(serverUrl) {
    const target = new URL(serverUrl);
    try {
      const saved = JSON.parse(fs.readFileSync(filename, 'utf8'));
      if (typeof saved.roomId === 'string' && roomHash.test(`#room=${saved.roomId}`)) {
        target.hash = `room=${saved.roomId}`;
      }
    } catch { /* A missing or damaged preference must not prevent startup. */ }
    return target.href;
  }

  function remember(candidate, serverUrl) {
    let target;
    try {
      target = new URL(candidate);
      if (target.origin !== new URL(serverUrl).origin || target.pathname !== '/') return;
    } catch { return; }
    const match = target.hash.match(roomHash);
    if (target.hash && !match) return;
    const temporary = `${filename}.tmp`;
    try {
      fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
      fs.writeFileSync(temporary, JSON.stringify({ roomId: match ? match[1] : null }), { mode: 0o600 });
      fs.renameSync(temporary, filename);
    } catch {
      try { fs.unlinkSync(temporary); } catch { /* Optional preference only. */ }
    }
  }
  return { initialUrl, remember };
}

module.exports = { createChatLocation };
