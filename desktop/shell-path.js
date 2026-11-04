'use strict';

const { spawnSync } = require('node:child_process');
const path = require('node:path');

const MARKER = '__BEANEST_LOGIN_PATH__';

// Apps opened from Finder or the Dock inherit launchd's minimal PATH, so CLIs
// installed through nvm, Homebrew or ~/.local/bin (and the node they need)
// are invisible to the room server. Read the login shell's PATH once; the
// marker skips any greeting the shell prints, and a slow shell is abandoned.
function loginShellPath(env = process.env, { platform = process.platform, run = spawnSync } = {}) {
  if (platform !== 'darwin') return null;
  const shell = typeof env.SHELL === 'string' && path.isAbsolute(env.SHELL) ? env.SHELL : '/bin/zsh';
  const result = run(shell, ['-ilc', `printf '${MARKER}%s' "$PATH"`],
    { encoding: 'utf8', timeout: 5000, env, stdio: ['ignore', 'pipe', 'ignore'] });
  if (!result || result.error || result.status !== 0 || typeof result.stdout !== 'string') return null;
  const index = result.stdout.lastIndexOf(MARKER);
  if (index < 0) return null;
  const value = result.stdout.slice(index + MARKER.length).trim();
  return value && !/[\0\r\n]/.test(value) ? value : null;
}

// Login-shell entries first, then anything inherited; absolute and unique.
function mergedPath(loginPath, inherited) {
  const entries = [];
  for (const part of [...String(loginPath || '').split(':'), ...String(inherited || '').split(':')]) {
    if (part && path.isAbsolute(part) && !entries.includes(part)) entries.push(part);
  }
  return entries.join(':');
}

module.exports = { loginShellPath, mergedPath };
