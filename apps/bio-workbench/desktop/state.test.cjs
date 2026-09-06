'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { DesktopState, shutdown } = require('./state.cjs');
test('native state survives a changed renderer origin and app restart', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'bio-state-test-'));
  try {
    const state = new DesktopState(directory);
    state.set('bio-workbench.input-draft.v1', JSON.stringify({ sequence: 'ACDE', notes: '<script>literal text</script>' }));
    state.set('bio-workbench.annotations.v1.' + 'a'.repeat(64), '{"annotations":[]}');
    state.flush();
    const restored = new DesktopState(directory);
    assert.equal(JSON.parse(restored.get('bio-workbench.input-draft.v1')).sequence, 'ACDE');
    assert.throws(() => restored.set('../../private-key', 'value'));
    assert.throws(() => restored.set('bio-workbench.input-draft.v1', 'a'.repeat(2 * 1024 * 1024 + 1)));
    assert.equal(fs.statSync(restored.file).mode & 0o777, 0o600);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
test('failed timed saves preserve dirty notes and report once until a successful retry', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'bio-state-test-'));
  const errors = [];
  const state = new DesktopState(directory, { onError: error => errors.push(error) });
  const key = 'bio-workbench.input-draft.v1';
  const temporary = path.join(directory, 'molecular-state.json.tmp');
  try {
    state.set(key, 'saved'); state.flush();
    // A real filesystem write failure exercises the asynchronous timer path.
    fs.mkdirSync(temporary);
    state.set(key, 'unsaved first revision');
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(errors.length, 1);
    assert.equal(state.dirty, true);
    assert.equal(state.get(key), 'unsaved first revision');
    assert.equal(new DesktopState(directory).get(key), 'saved');
    state.set(key, 'unsaved second revision');
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(errors.length, 1);
    assert.equal(state.get(key), 'unsaved second revision');
    fs.rmdirSync(temporary);
    state.flush();
    assert.equal(state.dirty, false);
    assert.equal(state.lastError, null);
    assert.equal(new DesktopState(directory).get(key), 'unsaved second revision');
    fs.mkdirSync(temporary);
    state.set(key, 'third revision');
    assert.throws(() => state.flush());
    assert.equal(errors.length, 2);
    assert.equal(state.dirty, true);
  } finally {
    clearTimeout(state.timer);
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
test('quit always stops the local backend even when saving notes fails', () => {
  let flushed = false;
  const signals = [];
  shutdown({ flush() { flushed = true; throw new Error('Disk full'); } }, { kill: signal => signals.push(signal) });
  assert.equal(flushed, true);
  assert.deepEqual(signals, ['SIGTERM']);
});
