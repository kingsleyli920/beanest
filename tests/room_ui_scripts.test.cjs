'use strict';

// Static guards for the room page's classic scripts. Every script shares one global
// lexical environment, so a name declared twice stops the later script from running
// at all. These checks need no DOM and run without loading the page.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const assetRoot = path.join(root, 'src', 'local_agent_coordinator', 'room_ui');
const html = fs.readFileSync(path.join(assetRoot, 'index.html'), 'utf8');
const scriptTags = [...html.matchAll(/<script\b[^>]*>/g)].map(([tag]) => tag);
const pageScripts = scriptTags.map((tag) => tag.match(/\bsrc="\/([^"]+)"/)?.[1]);
const read = (file) => fs.readFileSync(path.join(assetRoot, file), 'utf8');

// Compiling every script inside one strict block makes V8 itself reject any name
// declared twice, including a function declared twice, which plain concatenation
// allows and a browser silently lets the later file win.
function assertUniqueDeclarations(sources) {
  new vm.Script(`"use strict";{\n${sources.join('\n;\n')}\n}`, { filename: 'room_ui/all-scripts.js' });
}

test('the page lists its scripts as deferred files that exist, each once', () => {
  // Catches a typo in index.html, a repeated tag, or a script that would run before the DOM is parsed.
  assert.ok(pageScripts.length >= 6, `expected the page's scripts, found ${pageScripts.length}`);
  assert.ok(scriptTags.every((tag) => /\bdefer\b/.test(tag)), 'every script must be deferred to keep document order');
  assert.equal(new Set(pageScripts).size, pageScripts.length);
  for (const file of pageScripts) assert.ok(fs.existsSync(path.join(assetRoot, file)), `missing ${file}`);
});

test('every script in room_ui is loaded by the page', () => {
  // Catches a file split out of the page but never added to index.html.
  const onDisk = fs.readdirSync(assetRoot).filter((name) => name.endsWith('.js')).sort();
  assert.deepEqual(onDisk, [...pageScripts].sort());
});

test('every page script has a route on the local server', () => {
  // Catches a script missing from the server allow-list, which the browser would get as a 404.
  const routes = fs.readFileSync(path.join(root, 'src', 'local_agent_coordinator', 'ui_server.py'), 'utf8');
  for (const file of pageScripts) assert.ok(routes.includes(`"/${file}": ("${file}", "text/javascript; charset=utf-8")`), `no route for ${file}`);
});

test('every script parses and stays in strict mode', () => {
  // Catches a syntax error from a bad cut, and a moved block that lost the strict directive.
  for (const file of pageScripts) {
    execFileSync(process.execPath, ['--check', path.join(assetRoot, file)], { stdio: 'pipe' });
    // The English catalogs are data files and have never had a directive of their own.
    if (!file.startsWith('i18n-en-')) assert.ok(read(file).startsWith("'use strict';\n"), `${file} must start with its strict directive`);
  }
});

test('no top-level name is declared by more than one script', () => {
  // Catches the failure a browser reports as a redeclaration, which leaves that whole script unexecuted.
  assertUniqueDeclarations(pageScripts.map(read));
});

test('the duplicate check itself rejects every kind of repeated declaration', () => {
  // Catches the guard above going blind, for example if the wrapper stopped being strict.
  const fails = (first, second, name) => assert.throws(() => assertUniqueDeclarations([first, second]),
    (error) => error instanceof SyntaxError && error.message.includes(`'${name}'`));
  fails('const shared = 1;', 'const shared = 2;', 'shared');
  fails('let shared;', 'const shared = 2;', 'shared');
  fails('function shared() {}', 'function shared() {}', 'shared');
  fails('function shared() {}', 'const shared = 1;', 'shared');
  fails('const shared = 1;', 'function shared() {}', 'shared');
  fails('class Shared {}', 'class Shared {}', 'Shared');
  assert.doesNotThrow(() => assertUniqueDeclarations(['const first = 1; function one() {}', 'const second = 2; function two() {}']));
});
