'use strict';
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
const { DesktopState, shutdown } = require('./state.cjs');
const { batchLink, localURL, configureDownload } = require('./policy.cjs');
let window, backend, origin, quitting = false, pendingBatch;
let state;
const root = path.resolve(__dirname, '..');

function receiveLink(url) {
  const id = batchLink(url);
  if (!id) return;
  pendingBatch = id;
  if (window && !window.webContents.isLoading()) window.webContents.send('bio:open-batch', id);
  if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); }
}
function trusted(event) {
  return window && event.sender === window.webContents && event.senderFrame === window.webContents.mainFrame &&
    localURL(event.senderFrame?.url, origin);
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.setName('Bio Workbench');
  app.setAsDefaultProtocolClient('bio-workbench');
  app.on('second-instance', (_, argv) => { argv.forEach(receiveLink); if (window) { window.show(); window.focus(); } });
  app.on('open-url', (event, url) => { event.preventDefault(); receiveLink(url); });
  process.argv.forEach(receiveLink);
  app.on('before-quit', () => { quitting = true; shutdown(state, backend); });
  app.on('window-all-closed', () => app.quit());
  app.on('web-contents-created', (_, contents) => {
    contents.on('will-attach-webview', event => event.preventDefault());
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    contents.on('will-navigate', (event, url) => { if (!localURL(url, origin)) event.preventDefault(); });
  });

  app.whenReady().then(async () => {
    state = new DesktopState(app.getPath('userData'), {
      onError: error => dialog.showErrorBox('Local drafts and annotations could not be saved',
        'Your changes remain in this app until it closes. Export important notes before quitting. ' +
        'Cloud annotation saves have their own status.\n\n' + error.message),
    });
    const python = process.env.BIO_DESKTOP_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
    backend = spawn(python, ['-m', 'bio_desktop.server', '--assets', process.env.BIO_DESKTOP_ASSETS || path.join(root, 'frontend', 'dist')], {
      env: { ...process.env, PYTHONPATH: [path.join(root, 'backend'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
      stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
    });
    let errorText = '';
    backend.stderr.on('data', chunk => { errorText = (errorText + chunk.toString()).slice(-4000); });
    backend.on('error', error => { dialog.showErrorBox('Unable to start Bio Workbench', error.message); app.quit(); });
    backend.on('exit', () => {
      if (!quitting) { dialog.showErrorBox('Bio Workbench connection service stopped', errorText || 'Restart the app to reconnect. Cloud runs continue independently.'); app.quit(); }
    });
    const lines = readline.createInterface({ input: backend.stdout });
    let startupTimeout;
    try {
      origin = await new Promise((resolve, reject) => {
        startupTimeout = setTimeout(() => reject(new Error('The local connection service did not start')), 15000);
        lines.once('line', line => {
          try {
            const value = JSON.parse(line);
            if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(value.url)) throw new Error('Invalid local connection URL');
            resolve(value.url);
          } catch (error) { reject(error); }
        });
      });
    } catch (error) {
      dialog.showErrorBox('Unable to start Bio Workbench', error.message); app.quit(); return;
    } finally { clearTimeout(startupTimeout); lines.close(); }

    window = new BrowserWindow({
      width: 1500, height: 1000, minWidth: 1000, minHeight: 720, show: false,
      title: 'Bio Workbench', backgroundColor: '#10171d',
      icon: path.join(__dirname, 'icon.svg'),
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, spellcheck: false },
    });
    window.webContents.session.setPermissionRequestHandler((_, __, callback) => callback(false));
    window.webContents.session.setPermissionCheckHandler(() => false);
    window.webContents.session.on('will-download', (event, item) => configureDownload(event, item, origin));
    ipcMain.handle('bio:choose-ssh-key', async event => {
      if (!trusted(event)) throw new Error('Invalid desktop caller');
      const result = await dialog.showOpenDialog(window, { title: 'Choose SSH private key', properties: ['openFile', 'showHiddenFiles'] });
      return result.canceled ? null : result.filePaths[0];
    });
    ipcMain.handle('bio:open-import-dialog', async event => {
      if (!trusted(event)) throw new Error('Invalid desktop caller');
      // React first mounts the upload input. Supply a native menu gesture to
      // that one fixed control; the renderer cannot provide executable code.
      await window.webContents.executeJavaScript('document.querySelector(\'input[aria-label="Choose molecular files"]\')?.click()', true);
    });
    ipcMain.handle('bio:pending-batch', event => { if (!trusted(event)) throw new Error('Invalid desktop caller'); return pendingBatch || null; });
    ipcMain.on('bio:state:get', (event, key) => {
      try { if (!trusted(event)) throw new Error('Invalid desktop caller'); event.returnValue = { value: state.get(key) }; }
      catch (error) { event.returnValue = { error: error.message }; }
    });
    ipcMain.on('bio:state:set', (event, key, value) => {
      try { if (!trusted(event)) throw new Error('Invalid desktop caller'); state.set(key, value); event.returnValue = {}; }
      catch (error) { event.returnValue = { error: error.message }; }
    });
    const template = [
      ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
      { label: 'File', submenu: [
        { label: 'Import sequences or structures…', accelerator: 'CmdOrCtrl+O', click: () => window.webContents.send('bio:import-files') },
        { type: 'separator' }, { role: 'quit' },
      ] },
      { role: 'editMenu' },
      { label: 'View', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
      { role: 'windowMenu' },
      { label: 'Help', submenu: [{ label: 'About Bio Workbench', click: () => dialog.showMessageBox(window, { type: 'info', title: 'Bio Workbench', message: 'Bio Workbench', detail: 'Submit, monitor and compare cloud models. Closing the app leaves cloud runs active. Harrison shares your batches and annotations.' }) }] },
    ];
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
    window.once('ready-to-show', () => window.show());
    await window.loadURL(origin + (pendingBatch ? '/?batch=' + encodeURIComponent(pendingBatch) : '/'));
  }).catch(error => { dialog.showErrorBox('Bio Workbench', error.message); app.quit(); });
}
