'use strict';
// Stream Marathon – Kern: Timer, Twitch-Anbindung, lokaler Server für Overlay und Steuerung
const http = require('http');
const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');

const DEF = {
  port: 7777,
  // Timer
  channel: '', startMin: 60, capH: 0, ignoreAfterEnd: true,
  // Events
  en_follow: true, follow: 30, dedupe: true,
  en_sub: true, prime: 300, t1: 300, t2: 600, t3: 1500,
  en_gift: true, giftPct: 100,
  en_bits: true, bits: 60, bitsMin: 1,
  en_raid: false, raid: 2, raidMin: 5,
  // Design
  title: 'SUBATHON', endText: 'MARATHON VORBEI', font: 'Rubik', format: 'hms', size: 110,
  color: '#ffffff', accent: '#9146ff', shadow: 60, outline: 0, align: 'center',
  boxOn: false, boxColor: '#000000', boxOpacity: 50, boxRadius: 16,
  timerX: 50, timerY: 0,
  showStats: true, st_f: true, st_s: true, st_b: true, st_r: false, statSize: 100,
  pops: true, popDur: 3.2, popManual: false, sound: false, volume: 50,
  // Twitch-Login
  clientId: '', token: '', login: '',
  // Hotkeys
  hk_toggle: 'F9', hk_plus: 'F10', hk_minus: '', hkSec: 300, hk_hh: '',
  // Zusatz-Overlays
  ov_goals: true, goals: [
    { label: 'Horror-Game spielen', type: 'subs', target: 25 },
    { label: '24-Stunden-Stream', type: 'subs', target: 50 },
    { label: 'Haare färben', type: 'bits', target: 10000 }
  ],
  ov_hh: true, hhMult: 2, hhMin: 30,
  ov_top: true, topCount: 5,
  ov_wheel: true, wheelMin: 5, wheelFields: [300, 600, 900, 1800, 60, 1200, 2700, 3600],
  ov_ticker: true, tickerCount: 10, tickerSpeed: 40,
  // Größe der einzelnen Overlays in %
  sz_goals: 100, sz_hh: 100, sz_top: 100, sz_wheel: 100, sz_ticker: 100,
  // Im Hauptoverlay anzeigen + Position (tl, tr, bl, br, tc, bc, ml, mr)
  m_goals: false, p_goals: 'tl', m_hh: false, p_hh: 'tc', m_top: false, p_top: 'tr',
  m_wheel: false, p_wheel: 'mr', m_ticker: false, mScale: 100,
  // Nutzungsmeldung (immer aktiv): telemetry = 'ask' bis der Hinweis bestätigt ist, dann 'seen'
  telemetry: 'ask', installId: '', lastPing: 0, announced: false
};
const SCOPES = ['moderator:read:followers', 'user:write:chat', 'channel:read:redemptions', 'channel:manage:redemptions'];
const UPDATE_URL = (() => { try { return require('./package.json').updateUrl || ''; } catch (e) { return ''; } })();
const cmpVer = (a, b) => { const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number); for (let i = 0; i < 3; i++) { const d = (x[i] || 0) - (y[i] || 0); if (d) return d; } return 0; };
const VERSION = (() => { try { return require('./package.json').version; } catch (e) { return '?'; } })();
const WEBHOOK = (() => { try { return fs.readFileSync(path.join(__dirname, 'webhook.txt'), 'utf8').trim(); } catch (e) { return ''; } })();
const fmtDur = sec => {
  sec = Math.round(Math.abs(sec)); const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), x = sec % 60;
  return [d ? d + 'T' : '', h ? h + 'h' : '', m ? m + 'm' : '', x ? x + 's' : ''].filter(Boolean).join(' ') || '0s';
};

const readJson = (f, d) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch (e) { return d; } };
function writeJson(f, data) {
  try { fs.writeFileSync(f + '.tmp', JSON.stringify(data)); fs.renameSync(f + '.tmp', f); } catch (e) {}
}
const num = n => Number(n).toLocaleString('de-DE');
const tierName = p => p === 'Prime' ? 'Prime' : p === '3000' ? 'T3' : p === '2000' ? 'T2' : 'T1';
const unesc = v => (v || '').replace(/\\s/g, ' ').replace(/\\:/g, ';').replace(/\\\\/g, '\\');

function parseIrc(line) {
  const tags = {};
  if (line[0] === '@') {
    const i = line.indexOf(' ');
    line.slice(1, i).split(';').forEach(p => { const j = p.indexOf('='); tags[p.slice(0, j)] = unesc(p.slice(j + 1)); });
    line = line.slice(i + 1);
  }
  if (line[0] === ':') line = line.slice(line.indexOf(' ') + 1);
  const ti = line.indexOf(' :');
  let text = '';
  if (ti >= 0) { text = line.slice(ti + 2); line = line.slice(0, ti); }
  const parts = line.split(' ');
  return { tags, command: parts[0], params: parts.slice(1), text };
}

class Core extends EventEmitter {
  constructor(dir, opts = {}) {
    super();
    this.version = VERSION;
    this.webhook = opts.webhook || WEBHOOK;
    this.dir = dir;
    fs.mkdirSync(dir, { recursive: true });
    this.cfgFile = path.join(dir, 'config.json');
    this.stateFile = path.join(dir, 'state.json');
    this.cfg = Object.assign({}, DEF, readJson(this.cfgFile, {}));
    // Ältere Versionen hatten eine Auswahl → Hinweis einmal neu anzeigen
    if (this.cfg.telemetry !== 'seen' && this.cfg.telemetry !== 'ask') { this.cfg.telemetry = 'ask'; writeJson(this.cfgFile, this.cfg); }
    if (!this.cfg.installId) { this.cfg.installId = require('crypto').randomUUID(); writeJson(this.cfgFile, this.cfg); }
    const saved = readJson(this.stateFile, null);
    this.state = saved && saved.state && saved.state.n ? saved.state : this.newState();
    this.log = (saved && saved.log) || [];
    if (!this.state.hh) this.state.hh = { until: 0, mult: 1 };
    if (!this.state.top) this.state.top = {};
    this.seen = new Set((saved && saved.seen) || []);
    this.logId = this.log.reduce((m, x) => Math.max(m, x.id), 0);
    this.status = { irc: 'aus', follow: 'aus', hotkeys: {} };
    this.clients = new Set();
    this.initExtras(saved);
    // App wurde beendet oder ist abgestürzt, während der Timer lief → pausiert weitermachen
    if (this.state.running && saved && saved.resume) {
      // Neustart durch ein Update: Timer läuft einfach weiter
      this.pushLog('Update-Neustart – Timer läuft weiter', 0, 'info');
    } else if (this.state.running) {
      const at = (saved && saved.savedAt) || Date.now();
      this.state.remaining = Math.max(0, this.state.endsAt - at);
      this.state.running = false;
      this.pushLog('App neu gestartet – Timer pausiert', 0, 'info');
    }
  }

  newState() {
    return { running: false, started: false, endsAt: 0, remaining: this.cfg.startMin * 60000, added: 0, ended: false, n: { f: 0, s: 0, b: 0, r: 0 }, hh: { until: 0, mult: 1 }, top: {}, hourly: {}, evCount: 0 };
  }
  rem() { const s = this.state; return s.running ? Math.max(0, s.endsAt - Date.now()) : s.remaining; }

  // ---------- Speichern & Senden ----------
  saveState() {
    clearTimeout(this._st);
    this._st = setTimeout(() => this.saveStateNow(), 300);
  }
  saveStateNow(resume) {
    clearTimeout(this._st);
    writeJson(this.stateFile, { state: this.state, log: this.log.slice(-200), seen: [...this.seen].slice(-20000), savedAt: Date.now(), resume: !!resume });
  }
  saveCfg() { writeJson(this.cfgFile, this.cfg); this.saveProfile && this.saveProfile(); }
  changed() { this.saveState(); this.broadcast(); }

  snapshot() {
    const { token, ...pub } = this.cfg;
    const snap = { now: Date.now(), cfg: pub, hasToken: !!token, version: this.version, state: this.state, status: this.status, log: this.log.slice(-100) };
    return this.extraSnapshot(snap);
  }
  send(res, event, data) { try { res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`); } catch (e) {} }
  broadcast() {
    clearTimeout(this._bc);
    this._bc = setTimeout(() => { const snap = this.snapshot(); for (const c of this.clients) this.send(c, 'snap', snap); }, 15);
  }
  setStatus(k, v) { this.status[k] = v; this.broadcast(); }

  pushLog(text, sec, kind) {
    const e = { id: ++this.logId, t: Date.now(), text, sec, kind: kind || 'event' };
    this.log.push(e);
    if (this.log.length > 300) this.log = this.log.slice(-200);
    return e;
  }
  pop(entry) { for (const c of this.clients) this.send(c, 'pop', entry); }

  // ---------- Timer ----------
  addTime(sec, text, counts, opt = {}) {
    const c = this.cfg, s = this.state;
    text = this.cleanText(text);
    if (opt.user) opt = Object.assign({}, opt, { user: this.cleanName(opt.user) });
    sec = this.applyRules(sec, opt);
    if (s.ended && !opt.manual) {
      if (c.ignoreAfterEnd) { this.pushLog(text + ' (nach Ende ignoriert)', 0, 'info'); this.changed(); return; }
      s.running = true; s.endsAt = Date.now();
    }
    let mult = 1;
    if (sec > 0 && this.hhActive() && opt.kind !== 'manual') { mult = s.hh.mult || 2; sec *= mult; }
    let ms = Math.round(sec * 1000);
    if (c.capH > 0 && ms > 0) {
      const left = c.capH * 3600000 - (c.startMin * 60000 + s.added);
      ms = Math.max(0, Math.min(ms, left));
    }
    const before = this.rem();
    if (s.running) s.endsAt = Math.max(Math.max(s.endsAt, Date.now()) + ms, Date.now());
    else s.remaining = Math.max(0, s.remaining + ms);
    ms = this.rem() - before; // tatsächlich geänderte Zeit (nicht unter 0)
    s.added += ms;
    if (counts && !opt.test) for (const k in counts) s.n[k] = (s.n[k] || 0) + counts[k];
    if (this.rem() > 0) s.ended = false;
    if (opt.user && ms > 0 && !opt.test) s.top[opt.user] = (s.top[opt.user] || 0) + ms / 1000;
    this.recordStat(ms, opt);
    let label = text;
    if (mult > 1) label += ` (⚡ ×${mult})`;
    if (sec > 0 && ms === 0 && c.capH > 0) label += ' (Maximalzeit erreicht)';
    const e = this.pushLog(label, ms / 1000, opt.kind);
    if (ms > 0 && (!opt.manual || c.popManual)) this.pop(e);
    this.changed();
  }
  start() {
    const s = this.state;
    if (s.running || s.remaining <= 0) return;
    if (!s.started) this.report('marathon_start');
    s.running = true; s.started = true; s.ended = false; s.endsAt = Date.now() + s.remaining;
    this.pushLog('Timer gestartet', 0, 'info'); this.changed();
  }
  pause() {
    const s = this.state;
    if (!s.running) return;
    s.remaining = this.rem(); s.running = false;
    this.pushLog('Timer pausiert', 0, 'info'); this.changed();
  }
  toggle() { this.state.running ? this.pause() : this.start(); }
  reset() {
    this.state = this.newState(); this.seen.clear(); this.log = []; this.wheelBusy = false; this.wheelQueue = []; this.wheelGen = (this.wheelGen || 0) + 1;
    this.pushLog('Timer zurückgesetzt', 0, 'info'); this.changed();
  }
  tick() {
    const s = this.state;
    if (s.hh.until && Date.now() >= s.hh.until) {
      s.hh.until = 0; this.pushLog('⚡ Happy Hour vorbei', 0, 'info'); this.changed();
    }
    if (s.running && this.rem() <= 0) {
      s.running = false; s.remaining = 0; s.ended = true;
      this.pushLog('🏁 Marathon vorbei!', 0, 'info'); this.changed();
      this.report('marathon_end');
    }
  }

  // ---------- Nutzungsmeldung an den Entwickler (nur mit Zustimmung) ----------
  report(kind) {
    const c = this.cfg, s = this.state;
    if (!this.webhook) return;
    if (typeof fetch === 'undefined') return;
    if (kind === 'start') {
      if (Date.now() - (c.lastPing || 0) < 6 * 3600000) return;
      c.lastPing = Date.now(); this.saveCfg();
    }
    const who = c.channel || '';
    const link = who ? `[${who}](https://twitch.tv/${encodeURIComponent(who)})` : '*noch kein Kanal eingetragen*';
    const base = ((this.updUrl && this.updUrl()) || '').replace(/(app|beta)\/$/, '');
    const icon = base ? base + 'app/icon.png' : undefined;
    const osName = { win32: 'Windows', darwin: 'macOS', linux: 'Linux' }[process.platform] || process.platform;
    const plat = ['Twitch']; if (c.yt_on) plat.push('YouTube');
    const titles = {
      consent: ['🆕 Neue Installation', 0x00f593],
      start: ['▶️ App gestartet', 0x9146ff],
      marathon_start: ['🚀 Marathon gestartet', 0xffb31a],
      marathon_end: ['🏁 Marathon beendet', 0xff4d6d]
    };
    const [title, color] = titles[kind] || [kind, 0x9146ff];
    const ev = [];
    if (c.en_follow) ev.push(`❤️ Follow · **${fmtDur(c.follow)}**`);
    if (c.en_sub) ev.push(`💜 Sub · T1 **${fmtDur(c.t1)}** · T2 **${fmtDur(c.t2)}** · T3 **${fmtDur(c.t3)}** · Prime **${fmtDur(c.prime)}**`);
    if (c.en_gift) ev.push(`🎁 Gift-Sub · **${c.giftPct} %**`);
    if (c.en_bits) ev.push(`💎 100 Bits · **${fmtDur(c.bits)}**`);
    if (c.en_raid) ev.push(`🚀 Raid · **${fmtDur(c.raid)}** / Zuschauer`);
    const fields = [
      { name: '📺 Kanal', value: link, inline: true },
      { name: '🧩 Plattform', value: plat.join(' + '), inline: true },
      { name: '🏷️ Version', value: '`' + VERSION + '`', inline: true }
    ];
    if (kind === 'marathon_end') {
      fields.push({ name: '⏳ Gesamtzeit', value: '**' + fmtDur(c.startMin * 60 + s.added / 1000) + '**', inline: true });
      fields.push({ name: '➕ Dazugewonnen', value: fmtDur(s.added / 1000), inline: true });
      fields.push({ name: '📊 Statistik', value: `❤️ **${num(s.n.f)}** Follows\n⭐ **${num(s.n.s)}** Subs\n💎 **${num(s.n.b)}** Bits\n🚀 **${num(s.n.r || 0)}** Raids` });
    } else {
      fields.push({ name: '⏱️ Startzeit', value: fmtDur(c.startMin * 60), inline: true });
      fields.push({ name: '🧢 Maximum', value: c.capH > 0 ? c.capH + ' h' : 'unbegrenzt', inline: true });
      fields.push({ name: '🎨 Design', value: `${c.font}\n${c.color} / ${c.accent}`, inline: true });
      fields.push({ name: '⚡ Aktive Events', value: ev.join('\n') || '— keine —' });
    }
    if (this.recordReport) this.recordReport(title, fields);
    const embed = {
      author: { name: 'Stream Marathon', icon_url: icon },
      title, color, fields,
      thumbnail: icon ? { url: icon } : undefined,
      footer: { text: `ID ${(c.installId || '').slice(0, 8)} · ${osName}`, icon_url: icon },
      timestamp: new Date().toISOString()
    };
    fetch(this.webhook, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'Stream Marathon', avatar_url: icon, embeds: [embed] })
    }).catch(() => {});
  }

  // Baustein für eigene Meldungen (Fehler, Support) – gleiche Optik wie oben
  sendWebhook(title, color, fields) {
    if (!this.webhook || typeof fetch === 'undefined') return;
    const base = ((this.updUrl && this.updUrl()) || '').replace(/(app|beta)\/$/, '');
    const icon = base ? base + 'app/icon.png' : undefined;
    const osName = { win32: 'Windows', darwin: 'macOS', linux: 'Linux' }[process.platform] || process.platform;
    if (this.recordReport) this.recordReport(title, fields);
    fetch(this.webhook, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'Stream Marathon', avatar_url: icon, embeds: [{
        author: { name: 'Stream Marathon', icon_url: icon }, title, color, fields,
        footer: { text: `ID ${(this.cfg.installId || '').slice(0, 8)} · ${osName}`, icon_url: icon },
        timestamp: new Date().toISOString() }] })
    }).catch(() => {});
  }
  // Persönliche Pfade im Fehlertext anonymisieren (C:\Users\name\… -> C:\Users\…)
  cleanPath(s) { return String(s || '').replace(/([A-Za-z]:\\Users\\)[^\\/]+/g, '$1…').replace(/((?:\/home|\/Users)\/)[^\\/]+/g, '$1…'); }

  // Automatische Fehlermeldung an den Entwickler (nur wenn eingeschaltet)
  reportError(err, where) {
    if (!this.cfg.errReport) return;
    const msg = this.cleanPath(err && err.message || String(err)).slice(0, 300);
    const key = (where || '') + '|' + msg, now = Date.now();
    this._errSeen = this._errSeen || {};
    if (now - (this._errSeen[key] || 0) < 3600000) return;      // gleicher Fehler höchstens 1×/Stunde
    this._errSeen[key] = now;
    const stack = this.cleanPath(err && err.stack || '').split('\n').slice(1, 6).join('\n').slice(0, 900);
    const who = this.cfg.channel ? `[${this.cfg.channel}](https://twitch.tv/${encodeURIComponent(this.cfg.channel)})` : '*kein Kanal*';
    this.sendWebhook('🐞 Fehler in der App', 0xff4d6d, [
      { name: '📺 Kanal', value: who, inline: true },
      { name: '🏷️ Version', value: '`' + VERSION + '`', inline: true },
      { name: '📍 Stelle', value: where || 'unbekannt', inline: true },
      { name: '⚠️ Fehlermeldung', value: '```' + (msg || '—') + '```' },
      { name: '🔎 Details', value: stack ? '```' + stack + '```' : '—' }
    ]);
  }
  // Support-Nachricht, die der Nutzer selbst in der App abschickt
  reportSupport(d) {
    const cat = { bug: ['🐛 Bug-Meldung', 0xff4d6d], frage: ['❓ Frage', 0x9146ff], wunsch: ['💡 Wunsch', 0x00f593] }[d && d.cat] || ['🆘 Support', 0x9146ff];
    const who = this.cfg.channel ? `[${this.cfg.channel}](https://twitch.tv/${encodeURIComponent(this.cfg.channel)})` : '*kein Kanal*';
    const fields = [
      { name: '📺 Kanal', value: who, inline: true },
      { name: '🏷️ Version', value: '`' + VERSION + '`', inline: true }
    ];
    if (d && d.contact) fields.push({ name: '✉️ Antwort an', value: String(d.contact).slice(0, 80), inline: true });
    fields.push({ name: '💬 Nachricht', value: String(d && d.msg || '').slice(0, 1000) || '—' });
    this.sendWebhook(cat[0], cat[1], fields);
  }

  // ---------- Update-Anzeige ----------
  // Fragt GitHub direkt (ohne Zwischenspeicher), ob ein neues Update hochgeladen wurde,
  // und den zwischengespeicherten Download-Server, ob es schon verteilt ist.
  async checkLatest() {
    const UPDATE_URL = this.updUrl();
    if (!UPDATE_URL || typeof fetch === 'undefined') return;
    const L = Object.assign({}, this.status.latest);
    const get = async (url, headers) => {
      const r = await fetch(url, { cache: 'no-store', headers: headers || {} });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    };
    const m = /^https:\/\/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.*)$/.exec(UPDATE_URL);
    if (m && Date.now() - (this._apiAt || 0) > 110000) {
      this._apiAt = Date.now();
      try {
        const j = await get(`https://api.github.com/repos/${m[1]}/${m[2]}/contents/${m[4]}manifest.json?ref=${m[3]}`,
          { Accept: 'application/vnd.github.raw+json', 'User-Agent': 'StreamMarathon' });
        L.published = j.version; L.publishedAt = j.date; L.apiError = false;
      } catch (e) { L.apiError = true; }
    }
    try {
      const j = await get(UPDATE_URL + 'manifest.json?t=' + Date.now());
      L.cdn = j.version; L.cdnAt = j.date; L.error = false;
      if (!L.published || cmpVer(j.version, L.published) > 0) { L.published = j.version; L.publishedAt = j.date; }
    } catch (e) { L.error = true; }
    L.checkedAt = Date.now();
    // Ankündigung des Entwicklers abholen (liegt im Repo unter notice.json, nicht im app/-Ordner)
    try {
      const base = UPDATE_URL.replace(/(app|beta)\/$/, '');
      const r = await fetch(base + 'notice.json?t=' + Date.now(), { cache: 'no-store' });
      if (r.ok) {
        const n = await r.json();
        this.setStatus('notice', (n && n.id && (n.text || n.title)) ? {
          id: String(n.id).slice(0, 64), title: String(n.title || '').slice(0, 120),
          text: String(n.text || '').slice(0, 1000), level: n.level === 'warn' ? 'warn' : 'info',
          action: n.action === 'restart' ? 'restart' : '', date: n.date || ''
        } : null);
      } else if (r.status === 404) { this.setStatus('notice', null); }
    } catch (e) {}
    this.setStatus('latest', L);
    // Sobald das Update verteilt ist: sofort selbst herunterladen und neu starten
    if (L.cdn && cmpVer(L.cdn, this.version) > 0 && !this._installing && !this.isBadVersion(L.cdn)) this.installUpdate(L.cdn);
    // Solange ein Update verteilt wird, öfter nachsehen
    clearTimeout(this._updT);
    const pending = L.published && L.cdn && cmpVer(L.published, L.cdn) > 0;
    this._updT = setTimeout(() => this.checkLatest(), pending ? 30000 : 120000);
  }

  async installUpdate(ver) {
    const UPDATE_URL = this.updUrl();
    this._installing = true;
    const crypto = require('crypto');
    const root = path.join(this.dir, '..', 'updates'), tmp = path.join(root, '.tmp-core-' + process.pid);
    try {
      this.setStatus('installing', { version: ver, step: 'download', pct: 0 });
      const m = await (await fetch(UPDATE_URL + 'manifest.json?t=' + Date.now(), { cache: 'no-store' })).json();
      if (m.version !== ver || !m.files) throw new Error('Manifest passt nicht');
      fs.rmSync(tmp, { recursive: true, force: true }); fs.mkdirSync(tmp, { recursive: true });
      const files = Object.entries(m.files); let i = 0;
      for (const [file, hash] of files) {
        if (!/^[\w.\-/]+$/.test(file) || file.includes('..') || file === 'main.js') throw new Error('ungültiger Dateiname');
        const r = await fetch(UPDATE_URL + file + '?t=' + Date.now(), { cache: 'no-store' });
        if (!r.ok) throw new Error('Download fehlgeschlagen');
        const buf = Buffer.from(await r.arrayBuffer());
        if (crypto.createHash('sha256').update(buf).digest('hex') !== hash) throw new Error('Prüfsumme falsch');
        fs.mkdirSync(path.dirname(path.join(tmp, file)), { recursive: true });
        fs.writeFileSync(path.join(tmp, file), buf);
        this.setStatus('installing', { version: ver, step: 'download', pct: Math.round(++i / files.length * 100) });
      }
      const dest = path.join(root, ver);
      fs.rmSync(dest, { recursive: true, force: true });
      fs.renameSync(tmp, dest);
      this.setStatus('installing', { version: ver, step: 'restart', pct: 100 });
      this.pushLog(`Update ${ver} heruntergeladen – App startet neu`, 0, 'info'); this.changed();
      setTimeout(() => this.emit('restart'), 2500);
    } catch (e) {
      try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (err) {}
      this._installing = false;
      this.setStatus('installing', { version: ver, step: 'error' });
    }
  }

  // ---------- Happy Hour ----------
  hhActive() { const h = this.state.hh; return !!(h && h.until && Date.now() < h.until); }
  hhStart(min) {
    min = Math.max(1, Math.min(1440, Number(min) || 30));
    this.state.hh = { until: Date.now() + min * 60000, mult: Math.max(1, this.cfg.hhMult || 2) };
    this.pushLog(`⚡ Happy Hour gestartet: ${min} Min, alles ×${this.state.hh.mult}`, 0, 'info');
    this.changed();
  }
  hhStop() { if (!this.hhActive()) return; this.state.hh.until = 0; this.pushLog('⚡ Happy Hour beendet', 0, 'info'); this.changed(); }
  hhToggle() { this.hhActive() ? this.hhStop() : this.hhStart(this.cfg.hhMin); }

  // ---------- Glücksrad ----------
  spinWheel(who, count, test) {
    const f = this.cfg.wheelFields;
    if (!this.cfg.ov_wheel || !f || f.length < 2) return;
    // Mehrere Gift-Bomben gleichzeitig: nacheinander drehen
    this.wheelQueue = this.wheelQueue || [];
    this.wheelQueue.push({ who, count, test });
    if (!this.wheelBusy) this.nextWheel();
  }
  nextWheel() {
    const job = this.wheelQueue.shift();
    if (!job) { this.wheelBusy = false; return; }
    this.wheelBusy = true;
    const f = this.cfg.wheelFields.slice();
    const index = Math.floor(Math.random() * f.length);
    const spin = { id: Date.now(), fields: f, index, who: job.who, count: job.count, sec: f[index] * (this.hhActive() ? (this.state.hh.mult || 1) : 1) };
    const gen = this.wheelGen = this.wheelGen || 0;
    for (const c of this.clients) this.send(c, 'wheel', spin);
    this.pushLog(`🎡 Glücksrad für ${job.who} (${job.count} Gift-Subs)`, 0, 'info');
    this.changed();
    // Zeit erst gutschreiben, wenn das Rad steht (Animation ca. 6 s)
    setTimeout(() => {
      if (gen !== this.wheelGen) return;
      this.addTime(f[index], (job.test ? 'Test: ' : '') + `Glücksrad für ${job.who}`, null,
        { kind: 'wheel', user: job.test ? null : job.who, test: job.test, manual: job.test });
      setTimeout(() => { if (gen === this.wheelGen) this.nextWheel(); }, 3500);
    }, 6500);
  }

  // ---------- Events ----------
  tierSec(plan) { const c = this.cfg; return plan === 'Prime' ? c.prime : plan === '3000' ? c.t3 : plan === '2000' ? c.t2 : c.t1; }

  onIrc(line, ws) {
    if (line.startsWith('PING')) { try { ws && ws.send('PONG :tmi.twitch.tv'); } catch (e) {} return; }
    const m = parseIrc(line), c = this.cfg, t = m.tags;
    const who = t['display-name'] || t.login || 'Jemand';
    if (m.command === '366') this.setStatus('irc', 'verbunden mit #' + this.ircCh);
    if (m.command === 'RECONNECT' && ws) { try { ws.close(); } catch (e) {} }
    if (m.command === 'USERNOTICE') {
      const id = t['msg-id'], plan = t['msg-param-sub-plan'];
      if ((id === 'sub' || id === 'resub') && c.en_sub) {
        const months = parseInt(t['msg-param-cumulative-months'], 10);
        this.addTime(this.tierSec(plan), `${tierName(plan)}-Sub von ${who}` + (id === 'resub' && months > 1 ? ` (${months} Monate)` : ''), { s: 1 }, { kind: 'sub', user: who });
      } else if ((id === 'subgift' || id === 'anonsubgift') && c.en_gift) {
        const giver = id === 'anonsubgift' || t.login === 'ananonymousgifter' ? 'Anonym' : who;
        const to = t['msg-param-recipient-display-name'] || 'jemandem';
        this.addTime(this.tierSec(plan) * c.giftPct / 100, `${giver} schenkt ${to} einen ${tierName(plan)}-Sub`, { s: 1 }, { kind: 'gift', user: giver });
      } else if (id === 'submysterygift' && c.en_gift) {
        const cnt = parseInt(t['msg-param-mass-gift-count'], 10) || 0;
        const giver = t.login === 'ananonymousgifter' ? 'Anonym' : who;
        if (cnt >= c.wheelMin) this.spinWheel(giver, cnt);
      } else if (id === 'raid' && c.en_raid) {
        const v = parseInt(t['msg-param-viewerCount'], 10) || 0;
        const from = t['msg-param-displayName'] || who;
        if (v >= c.raidMin) this.addTime(v * c.raid, `Raid von ${from} mit ${num(v)} Zuschauern`, { r: 1 }, { kind: 'raid', user: from });
        else { this.pushLog(`Raid von ${from} mit ${v} Zuschauern (unter Minimum)`, 0, 'info'); this.changed(); }
      }
    }
    if (m.command === 'PRIVMSG') this.onChat(t, m.text);
    if (m.command === 'PRIVMSG' && t.bits && c.en_bits) {
      const b = parseInt(t.bits, 10) || 0;
      if (b >= Math.max(1, c.bitsMin)) this.addTime(b / 100 * c.bits, `${num(b)} Bits von ${who}`, { b }, { kind: 'bits', user: who });
    }
  }

  onFollow(ev) {
    const c = this.cfg;
    if (!c.en_follow) return;
    // Erkennung über die feste Twitch-User-ID (ändert sich nie), sonst Login/Name – so zählt ein Re-Follow nicht erneut
    const key = 'f:' + String(ev.user_id || ev.user_login || ev.user_name || '').toLowerCase();
    // Schutz ist an, außer er wurde ausdrücklich ausgeschaltet (fehlender Wert = an)
    if (c.dedupe !== false) {
      // Altes Format (nur Login/Name) weiter beachten, damit nach dem Update niemand doppelt zählt
      const oldKey = ev.user_login || ev.user_name;
      if (this.seen.has(key) || (oldKey && this.seen.has(oldKey))) { this.pushLog(`Follow von ${ev.user_name} (schon gezählt)`, 0, 'info'); this.changed(); return; }
      this.seen.add(key);
    }
    this.addTime(c.follow, `Follow von ${ev.user_name}`, { f: 1 }, { kind: 'follow', user: ev.user_name });
  }

  test(kind) {
    const c = this.cfg, T = 'Test: ';
    const map = {
      follow: [c.follow, 'Follow von Tester', 'follow'],
      prime: [c.prime, 'Prime-Sub von Tester', 'sub'],
      t1: [c.t1, 'T1-Sub von Tester', 'sub'],
      t2: [c.t2, 'T2-Sub von Tester', 'sub'],
      t3: [c.t3, 'T3-Sub von Tester', 'sub'],
      gift: [c.t1 * c.giftPct / 100, 'Tester schenkt Zuschauer einen T1-Sub', 'gift'],
      bits100: [c.bits, '100 Bits von Tester', 'bits'],
      bits1000: [c.bits * 10, '1.000 Bits von Tester', 'bits'],
      raid: [20 * c.raid, 'Raid von Tester mit 20 Zuschauern', 'raid']
    };
    if (kind === 'wheel') return this.spinWheel('Tester', Math.max(c.wheelMin, 5), true);
    const m = map[kind];
    if (m) this.addTime(m[0], T + m[1], null, { test: true, manual: true, kind: m[2] });
  }

  // ---------- Twitch-Chat (Subs, Gifts, Bits, Raids – ohne Login) ----------
  connIrc() {
    const ch = (this.cfg.channel || '').trim().toLowerCase().replace(/^#/, '');
    if (this.irc) { const o = this.irc; this.irc = null; try { o.close(); } catch (e) {} }
    clearTimeout(this.ircRetry);
    this.ircCh = ch;
    if (!ch) return this.setStatus('irc', 'kein Kanal eingetragen');
    if (typeof WebSocket === 'undefined') return this.setStatus('irc', 'WebSocket nicht verfügbar');
    const ws = new WebSocket('wss://irc-ws.chat.twitch.tv:443');
    this.irc = ws;
    this.setStatus('irc', 'verbinde…');
    ws.onopen = () => {
      ws.send('CAP REQ :twitch.tv/tags twitch.tv/commands');
      ws.send('PASS SCHMOOPIIE');
      ws.send('NICK justinfan' + Math.floor(Math.random() * 80000 + 1000));
      ws.send('JOIN #' + ch);
    };
    ws.onmessage = e => String(e.data).split('\r\n').forEach(l => { if (l) { try { this.onIrc(l, ws); } catch (err) {} } });
    ws.onerror = () => {};
    ws.onclose = () => {
      if (this.irc !== ws) return;
      this.setStatus('irc', 'getrennt – neuer Versuch in 5 s');
      this.ircRetry = setTimeout(() => { if (this.irc === ws) this.connIrc(); }, 5000);
    };
  }

  // ---------- EventSub (Follows – braucht Login) ----------
  async connFollow() {
    const c = this.cfg, gen = this.esGen = (this.esGen || 0) + 1;
    if (this.es) { const o = this.es; this.es = null; try { o.close(); } catch (e) {} }
    clearTimeout(this.esRetry);
    if (!c.clientId || !c.token) return this.setStatus('follow', 'nicht verbunden (Bereich „Twitch“)');
    this.setStatus('follow', 'prüfe Login…');
    try {
      const r = await fetch('https://id.twitch.tv/oauth2/validate', { headers: { Authorization: 'OAuth ' + c.token } });
      const v = await r.json().catch(() => ({}));
      if (gen !== this.esGen) return;
      if (r.status === 401) return this.setStatus('follow', 'Login abgelaufen – bitte neu verbinden');
      if (!r.ok) throw new Error(v.message || 'HTTP ' + r.status);
      if (v.client_id !== c.clientId) return this.setStatus('follow', 'Login passt nicht zur Client-ID – neu verbinden');
      if (!(v.scopes || []).includes('moderator:read:followers')) return this.setStatus('follow', 'Berechtigung fehlt – neu verbinden');
      this.uid = v.user_id;
      this.scopes = v.scopes || [];
      if (this.cfg.login !== v.login) { this.cfg.login = v.login; this.saveCfg(); }
      if (!this.cfg.channel) { this.cfg.channel = v.login; this.saveCfg(); this.connIrc(); }
      const ch = this.cfg.channel.trim().toLowerCase().replace(/^#/, '');
      this.bid = this.uid;
      if (ch !== v.login) {
        const u = await fetch('https://api.twitch.tv/helix/users?login=' + encodeURIComponent(ch), { headers: { Authorization: 'Bearer ' + c.token, 'Client-Id': c.clientId } });
        const j = await u.json().catch(() => ({}));
        if (gen !== this.esGen) return;
        if (!j.data || !j.data[0]) return this.setStatus('follow', `Kanal „${ch}“ nicht gefunden`);
        this.bid = j.data[0].id;
      }
    } catch (e) {
      if (gen !== this.esGen) return;
      this.setStatus('follow', 'keine Verbindung zu Twitch – neuer Versuch in 30 s');
      this.esRetry = setTimeout(() => { if (gen === this.esGen) this.connFollow(); }, 30000);
      return;
    }
    this.openEs('wss://eventsub.wss.twitch.tv/ws', gen, false);
  }
  openEs(url, gen, isReconnect) {
    if (typeof WebSocket === 'undefined') return this.setStatus('follow', 'WebSocket nicht verfügbar');
    const ws = new WebSocket(url);
    this.es = ws;
    if (!isReconnect) this.setStatus('follow', 'verbinde…');
    ws.onmessage = async e => {
      let m; try { m = JSON.parse(e.data); } catch (err) { return; }
      const type = m.metadata && m.metadata.message_type;
      if (type === 'session_welcome') {
        if (isReconnect) return this.setStatus('follow', 'verbunden als ' + this.cfg.login);
        try {
          const r = await fetch('https://api.twitch.tv/helix/eventsub/subscriptions', {
            method: 'POST',
            headers: { Authorization: 'Bearer ' + this.cfg.token, 'Client-Id': this.cfg.clientId, 'Content-Type': 'application/json' },
            body: JSON.stringify({ type: 'channel.follow', version: '2',
              condition: { broadcaster_user_id: this.bid, moderator_user_id: this.uid },
              transport: { method: 'websocket', session_id: m.payload.session.id } })
          });
          if (gen !== this.esGen) return;
          this.extraSubscribe(m.payload.session.id);
          if (r.ok) this.setStatus('follow', 'verbunden als ' + this.cfg.login);
          else {
            const j = await r.json().catch(() => ({}));
            this.setStatus('follow', r.status === 403 ? 'Kein Zugriff – du musst Streamer oder Mod im Kanal sein' : 'Fehler: ' + (j.message || r.status));
          }
        } catch (err) { this.setStatus('follow', 'Fehler beim Anmelden der Follows'); }
      } else if (type === 'notification' && m.payload.subscription.type === 'channel.follow') {
        this.onFollow(m.payload.event);
      } else if (type === 'notification') {
        this.onEventSub(m.payload.subscription.type, m.payload.event);
      } else if (type === 'session_reconnect') {
        ws.onclose = null;
        this.openEs(m.payload.session.reconnect_url, gen, true);
        setTimeout(() => { try { ws.close(); } catch (err) {} }, 5000);
      }
    };
    ws.onerror = () => {};
    ws.onclose = () => {
      if (this.es !== ws || gen !== this.esGen) return;
      this.setStatus('follow', 'getrennt – neuer Versuch in 5 s');
      this.esRetry = setTimeout(() => { if (gen === this.esGen) this.connFollow(); }, 5000);
    };
  }

  // ---------- Einstellungen ----------
  setConfig(patch) {
    const old = Object.assign({}, this.cfg);
    for (const k in patch) {
      if (!(k in DEF) || ['token', 'login', 'installId', 'lastPing', 'announced', 'adminHash', 'cp_rewardId', 'deckKey'].includes(k)) continue;
      if (!this.canSet(k)) continue;
      if (this.setExtraKey(k, patch[k])) continue;
      if (k === 'telemetry' && patch[k] !== 'seen') continue;
      const d = DEF[k], v = patch[k];
      if (k === 'goals') {
        if (!Array.isArray(v)) continue;
        this.cfg.goals = v.slice(0, 10).map(g => ({
          label: String((g && g.label) || '').slice(0, 60).trim(),
          type: ['subs', 'bits', 'follows', 'raids', 'hours'].includes(g && g.type) ? g.type : 'subs',
          target: Math.max(1, Math.min(1e9, Number(g && g.target) || 1))
        }));
        continue;
      }
      if (k === 'wheelFields') {
        if (!Array.isArray(v)) continue;
        const f = v.map(Number).filter(n => isFinite(n) && n > 0).map(n => Math.min(86400, Math.round(n))).slice(0, 12);
        if (f.length >= 2) this.cfg.wheelFields = f;
        continue;
      }
      if (typeof d === 'number') { const n = Number(v); if (isFinite(n)) this.cfg[k] = Math.max(0, n); }
      else if (typeof d === 'boolean') this.cfg[k] = !!v;
      else this.cfg[k] = String(v).slice(0, 300).trim();
    }
    for (const w of ['goals', 'hh', 'top', 'wheel', 'price']) if (!['tl', 'tr', 'bl', 'br', 'tc', 'bc', 'ml', 'mr'].includes(this.cfg['p_' + w])) this.cfg['p_' + w] = DEF['p_' + w];
    // Keine zwei Overlays an derselben Stelle: das gerade geänderte springt zur nächsten freien Stelle
    {
      const ORDER = ['tl', 'tr', 'ml', 'mr', 'bl', 'bc', 'br'];
      const changed = w => ['m_', 'p_', 'ov_'].some(pre => (pre + w) in patch);
      const act = ['goals', 'hh', 'top', 'wheel', 'price'].filter(w => this.cfg['ov_' + w] && this.cfg['m_' + w]);
      const used = new Set();
      for (const w of [...act.filter(w => !changed(w)), ...act.filter(changed)]) {
        let p = this.cfg['p_' + w];
        if (p === 'tc') continue; // unter dem Timer dürfen mehrere stehen (werden gestapelt)
        if (used.has(p)) {
          const i0 = ORDER.indexOf(p);
          for (let i = 1; i <= ORDER.length; i++) { const q = ORDER[(i0 + i) % ORDER.length]; if (!used.has(q)) { p = q; break; } }
        }
        this.cfg['p_' + w] = p; used.add(p);
      }
    }
    this.cfg.mScale = Math.max(30, Math.min(300, this.cfg.mScale || 100));
    for (const k of ['statSize', 'sz_goals', 'sz_hh', 'sz_top', 'sz_wheel', 'sz_ticker', 'sz_price', 'popSize', 'endSize']) this.cfg[k] = Math.max(30, Math.min(400, this.cfg[k] || 100));
    this.cfg.timerX = Math.max(0, Math.min(100, this.cfg.timerX));
    this.cfg.timerY = Math.max(0, Math.min(100, this.cfg.timerY));
    this.cfg.tickerSpeed = Math.max(5, Math.min(300, this.cfg.tickerSpeed || 40));
    this.cfg.channel = this.cfg.channel.toLowerCase().replace(/^#/, '').replace(/^https?:\/\/(www\.)?twitch\.tv\//, '').replace(/\/.*$/, '');
    if (!this.cfg.port || this.cfg.port < 1024 || this.cfg.port > 65535) this.cfg.port = old.port;
    this.cfg.port = Math.round(this.cfg.port);
    this.saveCfg();
    this.afterConfig(old);
    const s = this.state;
    if (this.cfg.startMin !== old.startMin && !s.started && !s.running && s.added === 0) s.remaining = this.cfg.startMin * 60000;
    if (this.cfg.channel !== old.channel) this.connIrc();
    if (this.cfg.channel !== old.channel || this.cfg.clientId !== old.clientId || this.cfg.en_follow !== old.en_follow) this.connFollow();
    this.emit('config');
    this.changed();
  }
  setToken(token) {
    this.cfg.token = String(token || '').trim();
    if (!this.cfg.token) this.cfg.login = '';
    this.saveCfg();
    this.connFollow();
    this.broadcast();
  }
  authUrl() {
    return 'https://id.twitch.tv/oauth2/authorize?client_id=' + encodeURIComponent(this.cfg.clientId) +
      '&redirect_uri=' + encodeURIComponent(`http://localhost:${this.port}/auth`) +
      '&response_type=token&scope=' + encodeURIComponent(SCOPES.join(' ')) + '&force_verify=true';
  }

  action(a, body) {
    switch (a) {
      case 'start': return this.start();
      case 'pause': return this.pause();
      case 'toggle': return this.toggle();
      case 'reset': return this.reset();
      case 'add': { const sec = Number(body.sec) || 0; if (sec) this.addTime(sec, 'Manuell', null, { manual: true, kind: 'manual' }); return; }
      case 'test': return this.test(String(body.kind));
      case 'previewPop': return this.pop({ id: 0, t: Date.now(), text: 'T1-Sub von Vorschau', sec: this.cfg.t1, kind: 'preview' });
      case 'logout': return this.setToken('');
      case 'clearLog': this.log = []; return this.changed();
      case 'reconnect': this.connIrc(); return this.connFollow();
      case 'hhStart': return this.hhStart(Number(body.min) || this.cfg.hhMin);
      case 'hhStop': return this.hhStop();
      case 'hhToggle': return this.hhToggle();
      case 'clearTop': this.state.top = {}; return this.changed();
      case 'checkUpdate': this._apiAt = 0; return this.checkLatest();
      case 'restartNow': return this.emit('restart');
      default: return this.extraAction(a, body || {});
    }
  }

  // ---------- HTTP ----------
  async handle(req, res) {
    const u = new URL(req.url, 'http://x');
    const p = u.pathname;
    const html = name => {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end(fs.readFileSync(path.join(__dirname, 'ui', name)));
    };
    const json = (code, data) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(data)); };

    if (req.method === 'GET') {
      if (p === '/' || p === '/control') return html('control.html');
      if (p === '/overlay') return html('overlay.html');
      if (/^\/overlay\/(goals|hh|top|wheel|ticker|price)$/.test(p)) return html('widgets.html');
      if (p === '/auth') return html('auth.html');
      if (p === '/api/snap') return json(200, this.snapshot());
      if (p === '/api/authurl') return json(200, { url: this.cfg.clientId ? this.authUrl() : '' });
      if (await this.extraGet(p, u, req, res, json)) return;
      if (p === '/api/stream') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
        res.write('retry: 2000\n\n');
        this.clients.add(res);
        this.send(res, 'snap', this.snapshot());
        req.on('close', () => this.clients.delete(res));
        return;
      }
      res.writeHead(404); return res.end('Nicht gefunden');
    }

    if (req.method === 'POST') {
      // Nur Anfragen von der eigenen Seite annehmen
      const origin = req.headers.origin;
      if (origin && !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\]):\d+$/.test(origin)) { res.writeHead(403); return res.end(); }
      if (!/application\/json/.test(req.headers['content-type'] || '')) { res.writeHead(415); return res.end(); }
      let raw = '';
      for await (const chunk of req) { raw += chunk; if (raw.length > 65536) { res.writeHead(413); return res.end(); } }
      let body = {}; try { body = JSON.parse(raw || '{}'); } catch (e) { return json(400, { ok: false }); }
      if (p === '/api/config') { this.setConfig(body); return json(200, { ok: true }); }
      if (p === '/api/token') { this.setToken(body.token); return json(200, { ok: true }); }
      if (p.startsWith('/api/action/')) { this.action(p.slice(12), body); return json(200, { ok: true }); }
      if (await this.extraPost(p, body, json)) return;
    }
    res.writeHead(404); res.end();
  }

  listen() {
    const handler = (q, r) => this.handle(q, r).catch(() => { try { r.writeHead(500); r.end(); } catch (e) {} });
    return new Promise((resolve, reject) => {
      const s1 = http.createServer(handler);
      s1.once('error', reject);
      s1.listen(this.cfg.port, '127.0.0.1', () => {
        this.port = this.cfg.port;
        this.servers = [s1];
        const s2 = http.createServer(handler);
        s2.on('error', () => {});
        s2.listen(this.port, '::1', () => this.servers.push(s2));
        this._tick = setInterval(() => this.tick(), 250);
        this._save = setInterval(() => { if (this.state.running) this.saveStateNow(); }, 5000);
        this.checkLatest();
        this._ka = setInterval(() => { for (const c of this.clients) { try { c.write(': ka\n\n'); } catch (e) {} } }, 15000);
        this.connIrc();
        this.connFollow();
        this.startExtras();
        setTimeout(() => {
          if (!this.cfg.announced) { this.cfg.announced = true; this.cfg.lastPing = Date.now(); this.saveCfg(); this.report('consent'); }
          else this.report('start');
        }, 5000);
        resolve(this.port);
      });
    });
  }

  shutdown(keepRunning) {
    if (this._down) return; this._down = true;
    if (keepRunning) {
      this.saveStateNow(true);
    } else {
      if (this.state.running) { this.state.remaining = this.rem(); this.state.running = false; this.pushLog('App beendet – Timer pausiert', 0, 'info'); }
      this.saveStateNow(false);
    }
    clearInterval(this._tick); clearInterval(this._save); clearInterval(this._ka); clearTimeout(this._updT);
    this.stopExtras();
    try { this.irc && this.irc.close(); } catch (e) {}
    try { this.es && this.es.close(); } catch (e) {}
    for (const c of this.clients) { try { c.end(); } catch (e) {} }
    (this.servers || []).forEach(s => { try { s.close(); } catch (e) {} });
  }
}

require('./extras').install(Core, DEF, { readJson, writeJson, fmtDur, num, cmpVer, UPDATE_URL });
module.exports = { Core, DEF };
