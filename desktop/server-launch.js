'use strict';

// Where the room service comes from. A checkout-backed app runs the project's
// own Python; a release app carries its interpreter and backend in Resources
// and keeps user data outside the (read-only, signed) bundle.
const path = require('node:path');

const SERVER_MODULE = 'local_agent_coordinator.room_server';
const SOURCE_DATA_NAME = 'Local Agent Coordinator';
// Not plain "Beanest": development builds have used that profile name.
const BUNDLED_DATA_NAME = 'io.github.kingsleyli920.beanest';

// -I ignores the user's PYTHON* variables and user site, so a stray PYTHONPATH
// cannot replace bundled code. It also drops the script directory from sys.path,
// hence the explicit insert. The first argument is the backend directory.
const BUNDLED_BOOTSTRAP = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));'
  + `runpy.run_module('${SERVER_MODULE}',run_name='__main__',alter_sys=True)`;

function parseInstallation(raw) {
  let metadata;
  try {
    metadata = JSON.parse(raw);
  } catch {
    return { kind: 'invalid' };
  }
  if (metadata?.bundled === true) return { kind: 'bundled' };
  if (typeof metadata?.projectRoot === 'string' && path.isAbsolute(metadata.projectRoot)) {
    return { kind: 'source', root: metadata.projectRoot };
  }
  return { kind: 'invalid' };
}

function dataDirectoryName(installation) {
  return installation.kind === 'bundled' ? BUNDLED_DATA_NAME : SOURCE_DATA_NAME;
}

function runtimeDirectoryFor(installation, dataPath) {
  if (installation.kind === 'invalid') throw new Error('Invalid local installation metadata');
  return installation.kind === 'bundled'
    ? path.join(dataPath, 'desktop-rooms')
    : path.join(installation.root, 'runtime', 'desktop-rooms');
}

function serverLaunch(installation, { resourcesPath, runtime, env, exists }) {
  const serverArguments = ['--runtime', runtime, '--port', '0'];
  if (installation.kind === 'bundled') {
    return {
      command: path.join(resourcesPath, 'python', 'bin', 'python3'),
      args: ['-I', '-B', '-c', BUNDLED_BOOTSTRAP, path.join(resourcesPath, 'backend'), ...serverArguments],
      cwd: runtime,
      env,
    };
  }
  if (installation.kind !== 'source') throw new Error('Invalid local installation metadata');
  const venvPython = path.join(installation.root, '.venv', 'bin', 'python');
  return {
    command: exists(venvPython) ? venvPython : 'python3',
    args: ['-m', SERVER_MODULE, ...serverArguments],
    cwd: installation.root,
    env: { ...env, PYTHONPATH: path.join(installation.root, 'src') },
  };
}

module.exports = {
  BUNDLED_BOOTSTRAP, BUNDLED_DATA_NAME, SOURCE_DATA_NAME,
  parseInstallation, dataDirectoryName, runtimeDirectoryFor, serverLaunch,
};
