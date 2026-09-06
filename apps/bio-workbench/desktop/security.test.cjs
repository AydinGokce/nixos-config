'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { batchLink, localURL, configureDownload } = require('./policy.cjs');

test('deep links select only an opaque batch ID at cold start or in a running app', () => {
  assert.equal(batchLink('bio-workbench://batch/abc_123-xyz'), 'abc_123-xyz');
  for (const url of [
    'https://batch/abc', 'bio-workbench://other/abc', 'bio-workbench://batch/abc?command=run',
    'bio-workbench://batch/abc#section', 'bio-workbench://batch/../etc/passwd',
    'bio-workbench://user@batch/abc', 'bio-workbench://batch:443/abc',
    'bio-workbench://batch/a%2fb', '/nix/store/desktop', '',
  ]) assert.equal(batchLink(url), null, url);
});

test('navigation and downloads reject foreign or malformed origins', () => {
  const origin = 'http://127.0.0.1:45678';
  assert.equal(localURL(origin + '/api/v1/artifacts/artifact', origin), true);
  assert.equal(localURL('blob:' + origin + '/local-export', origin), true);
  for (const url of [
    'https://example.org/result', 'http://127.0.0.1:12345/file', 'file:///etc/passwd',
    'javascript:alert(1)', 'data:text/html,unsafe', 'blob:https://example.org/elsewhere', 'malformed',
  ]) assert.equal(localURL(url, origin), false, url);
  assert.equal(localURL(origin, undefined), false);
});

test('native download settings are applied before the will-download callback returns', () => {
  let settings, prevented = false;
  const result = configureDownload({ preventDefault: () => { prevented = true; } }, {
    getURL: () => 'http://127.0.0.1:45678/api/v1/artifacts/abc',
    getFilename: () => 'prediction.cif',
    setSaveDialogOptions: value => { settings = value; },
  }, 'http://127.0.0.1:45678');
  assert.equal(result, undefined); // No promise yields control before Electron opens its dialog.
  assert.deepEqual(settings, { title: 'Save result', defaultPath: 'prediction.cif' });
  assert.equal(prevented, false);
});

test('foreign download is cancelled before any native save dialog is configured', () => {
  let prevented = false;
  configureDownload({ preventDefault: () => { prevented = true; } }, {
    getURL: () => 'https://example.org/unrelated',
    setSaveDialogOptions: () => assert.fail('untrusted download reached native save dialog'),
  }, 'http://127.0.0.1:45678');
  assert.equal(prevented, true);
});
