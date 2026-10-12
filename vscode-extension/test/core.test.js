'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {inside, invoke} = require('../src/core');
test('denies backend execution in untrusted workspaces', async () => {
  await assert.rejects(invoke('nonexistent', '.', {}, false), /Trust this workspace/);
});
test('file access is confined to real project paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'prodx-extension-'));
  try {
    fs.writeFileSync(path.join(root, 'script.py'), 'pass');
    assert.equal(inside(root, 'script.py'), path.join(root, 'script.py'));
    assert.throws(() => inside(root, '..'), /outside/);
  } finally {fs.rmSync(root, {recursive:true, force:true});}
});
test('missing Python has actionable failure without stderr exposure', async () => {
  await assert.rejects(invoke('prodx-nonexistent-python', path.resolve(__dirname, '..'), {}, true), /Cannot start Python/);
});
test('observation deadline keeps bridge and finite descendant tracked and blocks retries', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'prodx-deadline-'));
  const original = global.setTimeout;
  const marker = path.join(root, 'finished');
  const child = `import time;from pathlib import Path;time.sleep(0.5);Path(${JSON.stringify(marker)}).write_text('done')`;
  fs.writeFileSync(path.join(root, 'bridge.py'), `import subprocess,sys,json\nsubprocess.run([sys.executable,'-c',${JSON.stringify(child)}],check=True)\nprint(json.dumps({'ok':True,'result':{'finished':True}}))\n`);
  const notices = [];
  try {
    global.setTimeout = (callback, delay, ...args) => original(callback, delay === 600000 ? 20 : delay, ...args);
    const promise = invoke(process.env.PRODX_TEST_PYTHON || 'python', root, {root, action:'emulate-up'}, true, note => notices.push(note));
    await new Promise(resolve => original(resolve, 100));
    assert.equal(notices.length, 1);
    assert.equal(notices[0].state, 'running');
    assert.equal(fs.existsSync(marker), false);
    await assert.rejects(invoke('nonexistent', root, {root, action:'emulate-up'}, true), /conflicting retries are blocked/);
    assert.equal((await promise).finished, true);
    assert.equal(fs.readFileSync(marker, 'utf8'), 'done');
  } finally {global.setTimeout = original; fs.rmSync(root, {recursive:true, force:true});}
});
