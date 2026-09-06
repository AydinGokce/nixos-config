'use strict';
const fs = require('node:fs');
const path = require('node:path');
const KEY = /^bio-workbench\.(input-draft\.v1|annotations\.v1\.[a-f0-9]{64})$/;
const MAX_ENTRY = 2 * 1024 * 1024;
const MAX_TOTAL = 32 * 1024 * 1024;
class DesktopState {
  constructor(directory, { onError = () => {} } = {}) {
    this.directory = directory;
    this.file = path.join(directory, 'molecular-state.json');
    this.values = {};
    this.timer = null;
    this.dirty = false;
    this.lastError = null;
    this.errorReported = false;
    this.onError = onError;
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    if (fs.existsSync(this.file)) {
      const size = fs.statSync(this.file).size;
      if (size > MAX_TOTAL) throw new Error('Saved molecular state exceeds its size limit');
      const values = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error('Invalid saved molecular state');
      for (const [key, value] of Object.entries(values)) this.validate(key, value);
      this.values = values;
    }
  }
  validate(key, value = '') {
    if (typeof key !== 'string' || !KEY.test(key) || typeof value !== 'string' || Buffer.byteLength(value) > MAX_ENTRY) throw new Error('Invalid molecular state entry');
  }
  get(key) { this.validate(key); return this.values[key] ?? null; }
  set(key, value) {
    this.validate(key, value);
    const next = { ...this.values, [key]: value };
    if (Buffer.byteLength(JSON.stringify(next)) > MAX_TOTAL) throw new Error('Local annotation storage is full; export notes before adding more');
    this.values = next;
    this.dirty = true;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      try { this.flush(); }
      catch { /* flush reports the error and retains dirty state for retry. */ }
    }, 250);
  }
  flush() {
    clearTimeout(this.timer); this.timer = null;
    if (!this.dirty) return;
    const temporary = path.join(this.directory, 'molecular-state.json.tmp');
    try {
      const fd = fs.openSync(temporary, 'w', 0o600);
      try { fs.writeFileSync(fd, JSON.stringify(this.values)); fs.fsyncSync(fd); }
      finally { fs.closeSync(fd); }
      fs.renameSync(temporary, this.file);
      this.dirty = false;
      this.lastError = null;
      this.errorReported = false;
    } catch (error) {
      this.lastError = error;
      if (!this.errorReported) {
        this.errorReported = true;
        this.onError(error);
      }
      throw error;
    }
  }
}
function shutdown(state, backend) {
  try { if (state) state.flush(); }
  catch { /* A failed save has already been reported; cleanup must still run. */ }
  finally { if (backend) backend.kill('SIGTERM'); }
}
module.exports = { DesktopState, shutdown };
