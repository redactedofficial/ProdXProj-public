'use strict';
const path = require('node:path');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const crypto = require('node:crypto');
const running = new Map();

function inside(root, candidate) {
  const base = fs.realpathSync(root);
  const file = fs.realpathSync(path.resolve(root, candidate));
  const relative = path.relative(base, file);
  if (relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
    throw new Error('File is outside this project.');
  }
  return file;
}

function invoke(python, extensionPath, request, trusted, onProgress) {
  if (!trusted) return Promise.reject(new Error('Trust this workspace before using ProdXProj.'));
  const readonly = ['plan', 'check', 'emulate-plan', 'emulate-status', 'remote-preview', 'operation-status'].includes(request.action);
  const key = path.resolve(request.root || extensionPath);
  if (!readonly && running.has(key)) return Promise.reject(new Error(`Operation ${running.get(key)} is still running; conflicting retries are blocked.`));
  const operationId = crypto.randomUUID();
  if (!readonly) running.set(key, operationId);
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-I', path.join(extensionPath, 'bridge.py')], {
      cwd: extensionPath, shell: false, windowsHide: true,
      env: {...process.env, PYTHONIOENCODING: 'utf-8'}
    });
    let output = '', overflow = false;
    // This is an observation deadline, never a cancellation deadline. Killing the
    // bridge cannot undo Docker/SSH effects or guarantee descendant termination.
    const timer = setTimeout(() => {
      if (typeof onProgress === 'function') onProgress({operationId, state: 'running',
        message: `Operation ${operationId} is still running. Completion is not yet known; retries remain blocked.`});
    }, 600000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', data => {
      if (!overflow) output += data;
      if (Buffer.byteLength(output) > 16 * 1024 * 1024) {overflow = true; output = '';}
    });
    // Never log subprocess stderr: dependency/hook messages can disclose secrets.
    child.stderr.resume();
    child.on('error', () => {clearTimeout(timer); reject(new Error('Cannot start Python. Use ProdXProj: Select Python Interpreter.'));});
    child.on('close', () => {
      clearTimeout(timer);
      try {
        if (overflow) throw new Error('Preview exceeds editor output limit.');
        const response = JSON.parse(output);
        if (!response.ok) throw new Error(response.message);
        resolve(response.result);
      } catch (error) {reject(error instanceof SyntaxError ? new Error('Backend stopped without a result; check Python dependencies.') : error);}
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({...request, operationId}));
  }).finally(() => {if (running.get(key) === operationId) running.delete(key);});
}
module.exports = {inside, invoke};
