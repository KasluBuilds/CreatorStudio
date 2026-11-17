'use strict';
const { app, BrowserWindow, ipcMain, shell, Notification, net, session } = require('electron');
const path = require('path');
const { Controller, AUTO_REFRESH_MS } = require('./src/controller');
const { isAllowedLink } = require('./src/links');

const args = process.argv.slice(1);
const flag = (n) => args.includes(n);
const opt = (n) => { const a = args.find((x) => x.startsWith(`${n}=`)); return a ? a.slice(n.length + 1) : null; };
const SELFTEST = flag('--selftest');
if (opt('--userdata')) app.setPath('userData', path.resolve(opt('--userdata')));
if (SELFTEST) app.disableHardwareAcceleration();          // software rendering: screenshots work on any machine
app.setAppUserModelId('studio.creator.dashboard');

let win = null;
let controller = null;
let timer = null;

function send(state) {
  if (win && !win.isDestroyed()) win.webContents.send('state:changed', state);
  if (win && !win.isDestroyed() && state.attention && state.attention.length && !win.isFocused()) win.flashFrame(true);
}

function makeFetch() {
  if (opt('--fixture')) return require('./src/fixture').makeFixtureFetch({ root: path.resolve(opt('--fixture')) });
  if (flag('--offline')) return async () => { throw new TypeError('fetch failed (simulated offline)'); };
  return (url, init) => net.fetch(url, init);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440, height: 940, minWidth: 980, minHeight: 640, backgroundColor: '#0e1320', title: 'Creator Studio Dashboard', show: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true,
      webSecurity: true, spellcheck: false, devTools: !app.isPackaged || SELFTEST },
  });
  win.removeMenu();
  win.webContents.setWindowOpenHandler(({ url }) => { if (isAllowedLink(url)) shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith('file://')) { e.preventDefault(); if (isAllowedLink(url)) shell.openExternal(url); } });
  win.on('focus', () => win.flashFrame(false));
  win.webContents.on('before-input-event', (event, input) => {          // F5 or Ctrl+R refreshes the data
    if (input.type === 'keyDown' && (input.key === 'F5' || (input.control && input.key.toLowerCase() === 'r'))) { event.preventDefault(); controller.refresh(); }
  });
  win.loadFile(path.join(__dirname, 'ui', 'index.html'));
}

async function start() {
  await app.whenReady();
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  controller = new Controller({
    fetchFn: makeFetch(), dir: app.getPath('userData'), onChange: send,
    notify: ({ title, body }) => { if (Notification.isSupported()) new Notification({ title, body }).show(); },
  });
  controller.appVersion = app.getVersion();
  controller.init();

  ipcMain.handle('state:get', () => controller.getState());
  ipcMain.handle('state:refresh', () => controller.refresh());
  ipcMain.handle('link:open', async (_e, url) => { if (isAllowedLink(url)) { await shell.openExternal(url); return true; } return false; });

  createWindow();
  win.webContents.once('did-finish-load', () => {
    const first = controller.refresh();            // refresh on open; the saved snapshot (if any) is already on screen
    if (SELFTEST) {
      const { runSelftest } = require('./src/selftest');
      first.then(() => runSelftest({ app, win, controller, opts: { out: opt('--out'), offline: flag('--offline') } }))
        .catch((err) => { console.error('selftest crashed:', err); app.exit(2); });
    }
  });
  if (!SELFTEST) timer = setInterval(() => controller.refresh(), AUTO_REFRESH_MS);
}

if (!SELFTEST && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.on('window-all-closed', () => { if (timer) clearInterval(timer); app.quit(); });
  start();
}
