'use strict';
// The only bridge between the sandboxed page and the app. The page can read the state, ask for a refresh, choose which Orbit snapshot file to show (read-only) and ask to open
// a whitelisted link. It cannot read files, run commands or reach the network.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('dashboard', {
  getState: () => ipcRenderer.invoke('state:get'),
  refresh: () => ipcRenderer.invoke('state:refresh'),
  setTheme: (mode) => ipcRenderer.invoke('theme:set', String(mode)),
  orbitSelect: () => ipcRenderer.invoke('orbit:select'),
  orbitReload: () => ipcRenderer.invoke('orbit:reload'),
  openLink: (url) => ipcRenderer.invoke('link:open', String(url)),
  onState: (cb) => { ipcRenderer.on('state:changed', (_event, state) => cb(state)); },
});
