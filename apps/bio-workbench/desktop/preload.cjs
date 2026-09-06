'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('bioDesktop', {
  platform: process.platform,
  state: {
    get: key => { const result = ipcRenderer.sendSync('bio:state:get', key); if (result.error) throw new Error(result.error); return result.value; },
    set: (key, value) => { const result = ipcRenderer.sendSync('bio:state:set', key, value); if (result.error) throw new Error(result.error); },
  },
  chooseSSHKey: () => ipcRenderer.invoke('bio:choose-ssh-key'),
  openImportDialog: () => ipcRenderer.invoke('bio:open-import-dialog'),
  onOpenBatch: callback => {
    if (typeof callback !== 'function') throw new TypeError('Expected callback');
    const listener = (_, id) => callback(id);
    ipcRenderer.on('bio:open-batch', listener);
    ipcRenderer.invoke('bio:pending-batch').then(id => { if (id) callback(id); });
    return () => ipcRenderer.removeListener('bio:open-batch', listener);
  },
});
ipcRenderer.on('bio:import-files', () => window.dispatchEvent(new CustomEvent('bio:import-files')));
