'use strict';
// Stream Marathon – App-Fenster, Hotkeys, Server-Start.
// Diese Datei wird per Update ausgetauscht; der feste Starter ist main.js.
const { app, BrowserWindow, shell, globalShortcut, dialog, Menu } = require('electron');
const path = require('path');
const { Core } = require('./core');

module.exports = function startShell(ctx) {
  let win = null, core = null, quitting = false;

  app.on('second-instance', () => {
    if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
  });

  function registerHotkeys() {
    globalShortcut.unregisterAll();
    const c = core.cfg, res = {};
    const actions = {
      hk_toggle: () => core.toggle(),
      hk_plus: () => core.addTime(core.cfg.hkSec, 'Hotkey', null, { manual: true, kind: 'manual' }),
      hk_minus: () => core.addTime(-core.cfg.hkSec, 'Hotkey', null, { manual: true, kind: 'manual' }),
      hk_hh: () => core.hhToggle(),
      hk_pausescr: () => core.setPanic('pause')
    };
    for (const k in actions) {
      const acc = String(c[k] || '').trim();
      if (!acc) { res[k] = 'aus'; continue; }
      try { res[k] = globalShortcut.register(acc, actions[k]) ? 'aktiv' : 'schon belegt'; }
      catch (e) { res[k] = 'ungültig'; }
    }
    core.setStatus('hotkeys', res);
  }

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    core = new Core(path.join(app.getPath('userData'), 'data'), { webhook: ctx.webhook });
    // Beim Neustart nach einem Update kann der alte Prozess den Port noch kurz belegen → kurz warten
    let listenErr = null;
    for (let i = 0; i < 20; i++) {
      try { await core.listen(); listenErr = null; break; }
      catch (e) { listenErr = e; await new Promise(r => setTimeout(r, 500)); }
    }
    if (listenErr) {
      ctx.ready();
      dialog.showErrorBox('Stream Marathon',
        `Port ${core.cfg.port} ist schon belegt.\n\nLäuft Stream Marathon vielleicht schon? Sonst ändere den Port in\n${path.join(app.getPath('userData'), 'data', 'config.json')}`);
      app.quit();
      return;
    }
    ctx.ready();
    if (ctx.updatedFrom) { core.pushLog(`App auf Version ${core.version} aktualisiert`, 0, 'info'); core.status.justUpdated = { from: ctx.updatedFrom, to: core.version }; }
    registerHotkeys();
    core.on('config', registerHotkeys);
    core.on('restart', () => {
      quitting = true;
      try { globalShortcut.unregisterAll(); } catch (e) {}
      core.shutdown(true);
      try { app.releaseSingleInstanceLock(); } catch (e) {}
      app.relaunch();
      app.exit(0);
    });

    win = new BrowserWindow({
      width: 1200, height: 820, minWidth: 920, minHeight: 620,
      backgroundColor: '#0e0e10', title: 'Stream Marathon',
      icon: path.join(__dirname, 'icon.png'), autoHideMenuBar: true,
      webPreferences: { contextIsolation: true, nodeIntegration: false }
    });
    win.loadURL(`http://127.0.0.1:${core.port}/`);
    win.webContents.setWindowOpenHandler(({ url }) => {
      if (/^https?:\/\//.test(url)) shell.openExternal(url);
      return { action: 'deny' };
    });
    win.webContents.on('will-navigate', (e, url) => {
      if (!url.startsWith(`http://127.0.0.1:${core.port}/`)) { e.preventDefault(); shell.openExternal(url); }
    });
    win.on('close', e => {
      if (quitting) return;
      if (core.state.running) {
        const r = dialog.showMessageBoxSync(win, {
          type: 'question', buttons: ['Beenden', 'Abbrechen'], defaultId: 1, cancelId: 1,
          title: 'Stream Marathon', message: 'Der Timer läuft noch.',
          detail: 'Wenn du die App schließt, wird der Timer pausiert und das Overlay in OBS bleibt stehen. Beim nächsten Start kannst du einfach weitermachen.'
        });
        if (r === 1) { e.preventDefault(); return; }
      }
      quitting = true;
    });
  });

  app.on('will-quit', () => {
    globalShortcut.unregisterAll();
    if (core) core.shutdown(false);
  });
  app.on('window-all-closed', () => app.quit());

  return {
    // Für Updates: Zustand sichern, laufender Timer läuft nach dem Neustart weiter
    shutdown(keepRunning) {
      quitting = true;
      try { globalShortcut.unregisterAll(); } catch (e) {}
      if (core) { core.shutdown(keepRunning); core = null; }
    },
    notify(msg) { if (core) core.setStatus('update', msg); }
  };
};
