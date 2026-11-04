'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loginShellPath, mergedPath } = require('./shell-path');

test('a Dock launch gains the login shell PATH ahead of launchd defaults', () => {
  // Catches: opening the app from Finder left PATH=/usr/bin:/bin:/usr/sbin:/sbin,
  // so nvm/Homebrew CLIs looked missing or unconfirmed.
  const calls = [];
  const run = (shell, args, options) => {
    calls.push({ shell, args, options });
    return { status: 0, stdout: 'Welcome to a chatty zsh!\n__BEANEST_LOGIN_PATH__/Users/me/.nvm/versions/node/v22/bin:/opt/homebrew/bin:/usr/bin' };
  };
  const value = loginShellPath({ SHELL: '/bin/zsh', PATH: '/usr/bin:/bin' }, { platform: 'darwin', run });
  assert.equal(value, '/Users/me/.nvm/versions/node/v22/bin:/opt/homebrew/bin:/usr/bin');
  assert.equal(calls[0].shell, '/bin/zsh');
  assert.deepEqual(calls[0].args.slice(0, 1), ['-ilc']);
  assert.equal(calls[0].options.timeout, 5000);
  assert.equal(mergedPath(value, '/usr/bin:/bin:/usr/sbin:/sbin'),
    '/Users/me/.nvm/versions/node/v22/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin');
});

test('a failing, slow, odd or non-macOS shell keeps the inherited PATH', () => {
  // Catches: a broken login shell blanking PATH or injecting relative entries.
  for (const result of [{ status: 1, stdout: '' }, { error: new Error('timed out') }, { status: 0, stdout: 'no marker' },
    { status: 0, stdout: '__BEANEST_LOGIN_PATH__/a\n/b' }, { status: 0, stdout: '__BEANEST_LOGIN_PATH__' }]) {
    assert.equal(loginShellPath({ SHELL: '/bin/zsh' }, { platform: 'darwin', run: () => result }), null);
  }
  assert.equal(loginShellPath({ SHELL: '/bin/zsh' }, { platform: 'linux', run: () => { throw new Error('not called'); } }), null);
  assert.equal(loginShellPath({ SHELL: 'relative-shell' }, { platform: 'darwin', run: (shell) => ({ status: 0, stdout: `__BEANEST_LOGIN_PATH__/${shell}` }) }), '//bin/zsh');
  assert.equal(mergedPath(null, '/usr/bin:/bin'), '/usr/bin:/bin');
  assert.equal(mergedPath('relative:/opt/homebrew/bin::/usr/bin', '/usr/bin'), '/opt/homebrew/bin:/usr/bin');
});
