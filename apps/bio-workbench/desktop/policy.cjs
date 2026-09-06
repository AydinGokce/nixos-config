'use strict';

function batchLink(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'bio-workbench:' && parsed.hostname === 'batch' &&
        /^\/[A-Za-z0-9_-]{1,160}$/.test(parsed.pathname) && !parsed.search && !parsed.hash &&
        !parsed.username && !parsed.password && !parsed.port) {
      return parsed.pathname.slice(1);
    }
  } catch (_) {}
  return null;
}

function localURL(url, origin) {
  if (!origin) return false;
  try { return new URL(url).origin === origin; }
  catch (_) { return false; }
}

function configureDownload(event, item, origin) {
  if (!localURL(item.getURL(), origin)) {
    event.preventDefault();
    return;
  }
  // Electron owns its native Save dialog and cancellation. These options must
  // be set synchronously in will-download; an awaited dialog is too late.
  item.setSaveDialogOptions({ title: 'Save result', defaultPath: item.getFilename() });
}

module.exports = { batchLink, localURL, configureDownload };
