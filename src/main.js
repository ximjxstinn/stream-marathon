'use strict';
// Stream Marathon – fester Starter mit automatischen Updates.
// Diese Datei wird NIE per Update ersetzt. Sie lädt die neueste geprüfte
// App-Version (shell.js, core.js, ui/) und fällt auf die mitgelieferte zurück,
// wenn ein Update nicht startet.
const { app } = require('electron');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');

const BUNDLED = __dirname;
const PKG = require('./package.json');
const UPDATE_URL = PKG.updateUrl || '';

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');

const cmp = (a, b) => {
  const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number);
  for (let i = 0; i < 3; i++) { const d = (x[i] || 0) - (y[i] || 0); if (d) return d; }
  return 0;
};
const isVer = v => /^\d+\.\d+\.\d+$/.test(v);
const readJ = (f, d) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch (e) { return d; } };
const writeJ = (f, o) => { try { fs.writeFileSync(f, JSON.stringify(o)); } catch (e) {} };
const sha256 = b => crypto.createHash('sha256').update(b).digest('hex');

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  const root = path.join(app.getPath('userData'), 'updates');
  const bootFile = path.join(root, 'boot.json');
  fs.mkdirSync(root, { recursive: true });
  const boot = readJ(bootFile, {});
  boot.bad = Array.isArray(boot.bad) ? boot.bad : [];
  // Letzter Start mit einer Update-Version kam nicht bis "bereit" → als kaputt merken
  if (boot.trying && !boot.ok && !boot.bad.includes(boot.trying)) boot.bad.push(boot.trying);

  // Alle installierten Versionen, neueste zuerst, dann die mitgelieferte
  const candidates = [];
  try {
    for (const v of fs.readdirSync(root)) {
      if (!isVer(v) || cmp(v, PKG.version) <= 0 || boot.bad.includes(v)) continue;
      if (fs.existsSync(path.join(root, v, 'shell.js'))) candidates.push(v);
    }
  } catch (e) {}
  candidates.sort((a, b) => cmp(b, a));
  const options = candidates.map(v => ({ dir: path.join(root, v), version: v }));
  options.push({ dir: BUNDLED, version: PKG.version });

  function readWebhook(dir) {
    for (const d of [dir, BUNDLED]) {
      try { const w = fs.readFileSync(path.join(d, 'webhook.txt'), 'utf8').trim(); if (w) return w; } catch (e) {}
    }
    return '';
  }

  // Erste Version laden, die sich starten lässt
  let cur = null, api = {};
  for (const opt of options) {
    const isUpdate = opt.dir !== BUNDLED;
    boot.trying = isUpdate ? opt.version : null; boot.ok = false;
    writeJ(bootFile, boot);
    const ctx = {
      version: opt.version,
      updatedFrom: boot.lastVersion && boot.lastVersion !== opt.version ? boot.lastVersion : null,
      webhook: readWebhook(opt.dir),
      ready() { boot.ok = true; boot.lastVersion = opt.version; writeJ(bootFile, boot); }
    };
    try {
      api = require(path.join(opt.dir, 'shell.js'))(ctx) || {};
      cur = opt;
      break;
    } catch (e) {
      if (!isUpdate) throw e;
      boot.bad.push(opt.version);
    }
  }

  // Alte und kaputte Update-Ordner aufräumen (die zwei neuesten funktionierenden bleiben)
  try {
    const keep = new Set(candidates.filter(v => !boot.bad.includes(v)).slice(0, 2));
    for (const v of fs.readdirSync(root)) {
      if ((isVer(v) && !keep.has(v) && v !== cur.version) || v.startsWith('.tmp-')) fs.rmSync(path.join(root, v), { recursive: true, force: true });
    }
  } catch (e) {}

  // ---------- Update-Prüfung ----------
  let busy = false, installed = null;
  async function checkForUpdate() {
    if (!UPDATE_URL || busy || installed) return;
    busy = true;
    const tmp = () => path.join(root, '.tmp-' + process.pid);
    try {
      const r = await fetch(UPDATE_URL + 'manifest.json?t=' + Date.now(), { cache: 'no-store' });
      if (!r.ok) return;
      const m = await r.json();
      if (!isVer(m.version) || cmp(m.version, cur.version) <= 0 || boot.bad.includes(m.version) || !m.files) return;
      const t = tmp();
      fs.rmSync(t, { recursive: true, force: true });
      fs.mkdirSync(t, { recursive: true });
      for (const [file, hash] of Object.entries(m.files)) {
        if (!/^[\w.\-/]+$/.test(file) || file.includes('..') || file === 'main.js') throw new Error('ungültiger Dateiname');
        const fr = await fetch(UPDATE_URL + file + '?t=' + Date.now(), { cache: 'no-store' });
        if (!fr.ok) throw new Error('Download fehlgeschlagen: ' + file);
        const buf = Buffer.from(await fr.arrayBuffer());
        if (sha256(buf) !== hash) throw new Error('Prüfsumme falsch: ' + file);
        fs.mkdirSync(path.dirname(path.join(t, file)), { recursive: true });
        fs.writeFileSync(path.join(t, file), buf);
      }
      if (!fs.existsSync(path.join(t, 'shell.js')) || !fs.existsSync(path.join(t, 'core.js'))) throw new Error('Update unvollständig');
      const dest = path.join(root, m.version);
      fs.rmSync(dest, { recursive: true, force: true });
      fs.renameSync(t, dest);
      installed = m.version;
      try { api.notify && api.notify(`Update ${m.version} wird installiert – App startet gleich neu…`); } catch (e) {}
      setTimeout(() => {
        try { api.shutdown && api.shutdown(true); } catch (e) {}
        try { app.releaseSingleInstanceLock(); } catch (e) {}
        app.relaunch();
        app.exit(0);
      }, 4000);
    } catch (e) {
      try { fs.rmSync(tmp(), { recursive: true, force: true }); } catch (err) {}
    } finally {
      busy = false;
    }
  }
  app.whenReady().then(() => {
    setTimeout(checkForUpdate, 8000);
    setInterval(checkForUpdate, 10 * 60 * 1000);
  });
}
