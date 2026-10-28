'use strict';
// Stream Marathon – Erweiterungen: Profile, Admin-Bereich, Stream Deck, Chat-Befehle,
// Kanalpunkte, YouTube, Event-Eingang, Changelog.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// Einstellungen, die NICHT pro Profil gespeichert werden
const GLOBAL_KEYS = new Set(['port', 'channel', 'clientId', 'token', 'login', 'telemetry', 'installId', 'lastPing', 'announced',
  'hk_toggle', 'hk_plus', 'hk_minus', 'hkSec', 'hk_hh', 'hk_pausescr',
  'adminHash', 'adminAutoLock', 'updChannel', 'autoBackup', 'deckKey',
  'yt_key', 'yt_channel', 'cp_rewardId', 'cp_on', 'cp_rewards', 'cp_migrated', 'noticeSeen', 'ghToken', 'errReport']);
// Diese Einstellungen gehen nur bei entsperrtem Admin-Bereich
const ADMIN_KEYS = new Set(['fNames', 'fWords', 'nightOn', 'nightFrom', 'nightTo', 'nightPct', 'hourCap',
  'adminAutoLock', 'updChannel', 'autoBackup', 'pauseText', 'ghToken']);

const NEW_DEF = {
  noticeSeen: '', ghToken: '', errReport: true,
  // Admin
  adminHash: '', adminAutoLock: true, updChannel: 'stable', autoBackup: true,
  fNames: '', fWords: '', nightOn: false, nightFrom: '02:00', nightTo: '08:00', nightPct: 50, hourCap: 0,
  pauseText: 'Gleich geht\'s weiter …', pauseTimer: true, hk_pausescr: '',
  // Stream Deck / Event-Eingang
  deckKey: '',
  // Chat-Befehle
  cmd_on: true, cmdCooldown: 10, cmds: [
    { cmd: '!timer', text: '⏱ Noch {zeit} im Subathon!', on: true },
    { cmd: '!ziele', text: '🎯 {ziel}: {stand}/{zielwert}', on: true },
    { cmd: '!top', text: '🏆 Top-Supporter: {top3}', on: true },
    { cmd: '!subathon', text: 'Sub = +{t1}, 100 Bits = +{bits}, Follow = +{follow}', on: true },
    { cmd: '!happyhour', text: '⚡ Happy Hour! Alles ×{faktor} – noch {rest}', on: false }
  ],
  // Kanalpunkte
  cp_on: false, cp_title: '+1 Minute Subathon', cp_cost: 500, cp_sec: 60, cp_max: 3, cp_cool: 300, cp_rewardId: '',
  cp_migrated: false, cp_rewards: [{ title: '+1 Minute Subathon', cost: 500, sec: 60, max: 3, cool: 300, color: '#9146FF', on: true, id: '' }],
  // YouTube
  yt_on: false, yt_key: '', yt_channel: '', yt_sc: 60, yt_member: 300, yt_poll: 45,
  // Einblendungen (+5m …): Position und Größe
  popPos: 'below', popX: 0, popY: 0, popSize: 100,
  showEnd: true, endLabel: 'Ende ca.',
  // Preisliste
  ov_price: false, m_price: false, p_price: 'mr', sz_price: 100, priceCredit: true,
  priceTitle: '⏱ So verlängerst du den Timer', priceColor: '#bf94ff', priceFmt: 'min',
  priceRows: [
    { k: 'follow', ic: '❤️', n: 'Follow', v: '', on: true },
    { k: 't1', ic: '💜', n: 'Sub (Tier 1)', v: '', on: true },
    { k: 't2', ic: '💜', n: 'Sub (Tier 2)', v: '', on: false },
    { k: 't3', ic: '💜', n: 'Sub (Tier 3)', v: '', on: false },
    { k: 'prime', ic: '👑', n: 'Prime-Sub', v: '', on: true },
    { k: 'gift', ic: '🎁', n: 'Sub verschenken', v: '', on: true },
    { k: 'bits', ic: '✨', n: 'Bits', v: '', on: true },
    { k: 'raid', ic: '🚀', n: 'Raid', v: '', on: true },
    { k: 'points', ic: '💠', n: 'Kanalpunkte', v: '', on: false },
    { k: 'yt', ic: '▶️', n: 'YouTube Super Chat', v: '', on: false },
    { k: 'tiktok', ic: '♪', n: 'TikTok-Geschenke', v: '', on: false }
  ],
  // TikTok über Zusatz-Tools (Event-Eingang)
  tt_on: true, tt_coin: 2, tt_follow: 0, tt_share: 0, tt_sub: 300
};

const hashPw = (pw, salt) => {
  salt = salt || crypto.randomBytes(12).toString('hex');
  return salt + ':' + crypto.scryptSync(String(pw), salt, 32).toString('hex');
};
const checkPw = (pw, stored) => {
  const [salt, h] = String(stored).split(':');
  if (!salt || !h) return false;
  const a = Buffer.from(hashPw(pw, salt).split(':')[1], 'hex'), b = Buffer.from(h, 'hex');
  return a.length === b.length && crypto.timingSafeEqual(a, b);
};
const list = s => String(s || '').split(/[,\n;]+/).map(x => x.trim().toLowerCase()).filter(Boolean);
const hm = s => { const m = /^(\d{1,2}):(\d{2})$/.exec(String(s).trim()); return m ? Math.min(23, +m[1]) * 60 + Math.min(59, +m[2]) : null; };
const hourKey = t => { const d = new Date(t); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + 'T' + String(d.getHours()).padStart(2, '0'); };

function install(Core, DEF, H) {
  Object.assign(DEF, NEW_DEF);
  const P = Core.prototype;

  // ================= Grundgerüst =================
  P.initExtras = function (saved) {
    const c = this.cfg;
    if (!c.adminHash) c.adminHash = hashPw('admin');
    if (!c.deckKey) c.deckKey = crypto.randomBytes(6).toString('hex');
    if (!c.cp_migrated) {
      c.cp_rewards = [{ title: c.cp_title || '+1 Minute Subathon', cost: c.cp_cost || 500, sec: c.cp_sec || 60, max: c.cp_max || 0, cool: c.cp_cool || 0, color: '#9146FF', on: true, id: c.cp_rewardId || '' }];
      c.cp_migrated = true;
    }
    this.state.panic = this.state.panic || '';
    this.state.hourly = this.state.hourly || {};
    this.state.evCount = this.state.evCount || 0;
    this.diag = [];
    this.adminUntil = 0;
    this.adminTokens = new Set();
    this.cmdLast = {};
    this.profFile = path.join(this.dir, 'profiles.json');
    this.backupDir = path.join(this.dir, 'backups');
    this.loadProfiles();
    try { this.changelog = JSON.parse(fs.readFileSync(path.join(__dirname, 'changelog.json'), 'utf8')); } catch (e) { this.changelog = []; }
    H.writeJson(this.cfgFile, this.cfg);
    // Diagnose: Statusänderungen mitschreiben
    const orig = this.setStatus.bind(this);
    const NAMES = { irc: 'Twitch-Chat', follow: 'Twitch-Login', chat: 'Chat-Befehle', points: 'Kanalpunkte', yt: 'YouTube' };
    this.setStatus = (k, v) => {
      if (NAMES[k] && this.status[k] !== v) this.addDiag(`${NAMES[k]}: ${v}`);
      orig(k, v);
    };
  };
  P.recordReport = function (title, fields) {
    this.reports = this.reports || H.readJson(path.join(this.dir, 'reports.json'), []);
    this.reports.push({ t: Date.now(), title, fields: (fields || []).map(f => ({ name: f.name, value: String(f.value).replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/`/g, '') })) });
    this.reports = this.reports.slice(-20);
    H.writeJson(path.join(this.dir, 'reports.json'), this.reports);
    this.broadcast();
  };
  P.addDiag = function (text) {
    this.diag.push({ t: Date.now(), text: String(text).slice(0, 200) });
    if (this.diag.length > 200) this.diag = this.diag.slice(-150);
  };
  P.startExtras = function () {
    this._xTick = setInterval(() => this.extraTick(), 30000);
    this._bak = setInterval(() => { if (this.cfg.autoBackup) this.writeBackup(); }, 15 * 60000);
    this.connYouTube();
    this.addDiag(`App ${this.version} gestartet, Port ${this.port}`);
  };
  P.stopExtras = function () {
    clearInterval(this._xTick); clearInterval(this._bak); clearTimeout(this._yt);
    if (this.cfg.autoBackup) this.writeBackup();
  };
  P.extraTick = function () {
    if (this.adminUntil && this.adminUntil !== Infinity && Date.now() > this.adminUntil) { this.adminUntil = 0; this.adminTokens.clear(); this.broadcast(); }
  };
  P.extraSnapshot = function (snap) {
    const pub = snap.cfg;
    delete pub.adminHash; delete pub.yt_key; delete pub.ghToken;
    snap.hasYtKey = !!this.cfg.yt_key;
    snap.hasGhToken = !!this.cfg.ghToken;
    snap.scopes = this.scopes || null;
    snap.profiles = { active: this.prof.active, names: Object.keys(this.prof.list) };
    snap.admin = { unlocked: this.isAdmin(), until: this.adminUntil === Infinity ? 0 : this.adminUntil, defaultPw: checkPw('admin', this.cfg.adminHash) };
    snap.diag = this.diag.slice(-40);
    snap.changelog = this.changelog;
    snap.reports = (this.reports || H.readJson(path.join(this.dir, 'reports.json'), [])).slice(-20);
    return snap;
  };

  // ================= Profile =================
  P.profileOf = function (cfg) {
    const o = {};
    for (const k in DEF) if (!GLOBAL_KEYS.has(k)) o[k] = cfg[k];
    return o;
  };
  P.loadProfiles = function () {
    const p = H.readJson(this.profFile, null);
    if (p && p.list && p.active && p.list[p.active]) this.prof = p;
    else { this.prof = { active: 'Standard', list: { Standard: this.profileOf(this.cfg) } }; H.writeJson(this.profFile, this.prof); }
    // Werte des aktiven Profils übernehmen (falls config.json älter ist)
    Object.assign(this.cfg, this.prof.list[this.prof.active]);
  };
  P.saveProfile = function () {
    if (!this.prof) return;
    this.prof.list[this.prof.active] = this.profileOf(this.cfg);
    H.writeJson(this.profFile, this.prof);
  };
  P.profileAction = function (a, b) {
    const name = String(b.name || '').trim().slice(0, 40);
    const L = this.prof.list;
    if (a === 'switch' && L[name] && name !== this.prof.active) {
      this.saveProfile();
      const old = Object.assign({}, this.cfg);
      this.prof.active = name;
      Object.assign(this.cfg, DEF, this.globals(), L[name]);
      H.writeJson(this.cfgFile, this.cfg); H.writeJson(this.profFile, this.prof);
      this.pushLog(`👤 Profil „${name}“ aktiv`, 0, 'info');
      this.afterConfig(old); this.emit('config'); this.changed();
    } else if (a === 'new' && name && !L[name]) {
      L[name] = this.profileOf(Object.assign({}, DEF));
      this.profileAction('switch', { name });
    } else if (a === 'copy' && name && !L[name]) {
      this.saveProfile(); L[name] = JSON.parse(JSON.stringify(L[this.prof.active]));
      this.profileAction('switch', { name });
    } else if (a === 'rename' && name && !L[name]) {
      L[name] = L[this.prof.active]; delete L[this.prof.active]; this.prof.active = name;
      H.writeJson(this.profFile, this.prof); this.changed();
    } else if (a === 'delete' && Object.keys(L).length > 1) {
      const del = this.prof.active, next = Object.keys(L).find(n => n !== del);
      const old = Object.assign({}, this.cfg);
      delete L[del]; this.prof.active = next;
      Object.assign(this.cfg, DEF, this.globals(), L[next]);
      H.writeJson(this.cfgFile, this.cfg); H.writeJson(this.profFile, this.prof);
      this.pushLog(`👤 Profil „${del}“ gelöscht – „${next}“ aktiv`, 0, 'info');
      this.afterConfig(old); this.emit('config'); this.changed();
    } else if (a === 'next') {
      const n = Object.keys(L); this.profileAction('switch', { name: n[(n.indexOf(this.prof.active) + 1) % n.length] });
    } else if (a === 'nth') {
      const n = Object.keys(L); const i = Math.max(1, parseInt(b.n, 10) || 1) - 1; if (n[i]) this.profileAction('switch', { name: n[i] });
    }
  };
  P.globals = function () { const o = {}; for (const k of GLOBAL_KEYS) o[k] = this.cfg[k]; return o; };

  // ================= Einstellungen =================
  P.canSet = function (k) { return !ADMIN_KEYS.has(k) || this.isAdmin(); };
  P.setExtraKey = function (k, v) {
    if (k === 'cmds') {
      if (!Array.isArray(v)) return true;
      this.cfg.cmds = v.slice(0, 20).map(x => ({
        cmd: ('!' + String(x && x.cmd || '').replace(/^!+/, '').replace(/\s+/g, '').toLowerCase()).slice(0, 25),
        text: String(x && x.text || '').slice(0, 300),
        on: !!(x && x.on)
      })).filter(x => x.cmd.length > 1);
      return true;
    }
    if (k === 'cp_rewards') {
      if (!Array.isArray(v)) return true;
      this.cfg.cp_rewards = v.slice(0, 20).map(x => ({
        title: String(x && x.title || '').slice(0, 45).trim() || 'Belohnung',
        cost: Math.max(1, Math.min(1e7, Math.round(Number(x && x.cost) || 1))),
        sec: Math.max(0, Math.min(86400, Number(x && x.sec) || 0)),
        max: Math.max(0, Math.round(Number(x && x.max) || 0)),
        cool: Math.max(0, Math.min(604800, Math.round(Number(x && x.cool) || 0))),
        color: /^#[0-9a-f]{6}$/i.test(x && x.color) ? x.color.toUpperCase() : '#9146FF',
        on: !!(x && x.on),
        id: /^[\w-]{0,64}$/.test(x && x.id || '') ? (x.id || '') : ''
      }));
      return true;
    }
    if (k === 'priceRows') {
      if (!Array.isArray(v)) return true;
      const KEYS = ['follow', 't1', 't2', 't3', 'prime', 'gift', 'bits', 'raid', 'points', 'yt', 'tiktok', ''];
      this.cfg.priceRows = v.slice(0, 30).map(x => ({
        k: KEYS.includes(x && x.k) ? x.k : '',
        ic: String(x && x.ic || '').slice(0, 8), n: String(x && x.n || '').slice(0, 40), v: String(x && x.v || '').slice(0, 40), on: !!(x && x.on)
      }));
      return true;
    }
    if (k === 'popX' || k === 'popY') { const n = Number(v); if (isFinite(n)) this.cfg[k] = Math.max(-1500, Math.min(1500, Math.round(n))); return true; }
    if (k === 'popPos') { this.cfg.popPos = ['below', 'left', 'right', 'tl', 'tr', 'center'].includes(v) ? v : 'below'; return true; }
    if (k === 'priceFmt') { this.cfg.priceFmt = ['min', 'sec', 'clock'].includes(v) ? v : 'min'; return true; }
    if (k === 'priceColor') { if (/^#[0-9a-f]{6}$/i.test(v)) this.cfg.priceColor = v; return true; }
    if (k === 'updChannel') { this.cfg.updChannel = v === 'beta' ? 'beta' : 'stable'; return true; }
    if (k === 'nightFrom' || k === 'nightTo') { if (hm(v) !== null) this.cfg[k] = String(v).trim(); return true; }
    if (k === 'yt_key' || k === 'yt_channel') { this.cfg[k] = String(v).trim().slice(0, 200); return true; }
    if (k === 'noticeSeen') { this.cfg.noticeSeen = String(v || '').slice(0, 64); return true; }
    if (k === 'ghToken') { this.cfg.ghToken = String(v || '').trim().slice(0, 255); return true; }
    return false;
  };
  P.afterConfig = function (old) {
    const c = this.cfg;
    c.nightPct = Math.max(0, Math.min(100, c.nightPct));
    c.cmdCooldown = Math.max(0, Math.min(600, c.cmdCooldown));
    c.yt_poll = Math.max(20, Math.min(600, c.yt_poll || 45));
    if (c.cp_on !== old.cp_on || JSON.stringify(c.cp_rewards) !== JSON.stringify(old.cp_rewards)) this.syncReward(old.cp_rewards);
    if (['yt_on', 'yt_key', 'yt_channel'].some(k => c[k] !== old[k])) this.connYouTube();
    if (c.updChannel !== old.updChannel) { this._apiAt = 0; this.checkLatest(); }
  };

  // ================= Admin =================
  P.isAdmin = function () { return !!this.adminUntil && (this.adminUntil === Infinity || Date.now() < this.adminUntil); };
  P.adminTouch = function () { if (this.isAdmin()) this.adminUntil = this.cfg.adminAutoLock ? Date.now() + 10 * 60000 : Infinity; };
  P.cleanName = function (name) {
    if (!name) return name;
    return list(this.cfg.fNames).includes(String(name).toLowerCase()) ? 'Anonym' : name;
  };
  P.cleanText = function (text) {
    text = String(text);
    for (const n of list(this.cfg.fNames)) text = text.replace(new RegExp('\\b' + n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'gi'), 'Anonym');
    for (const w of list(this.cfg.fWords)) text = text.replace(new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), '***');
    return text;
  };
  P.applyRules = function (sec, opt) {
    const c = this.cfg;
    if (!(sec > 0) || opt.manual || opt.test) return sec;
    if (c.nightOn) {
      const f = hm(c.nightFrom), t = hm(c.nightTo), d = new Date(), now = d.getHours() * 60 + d.getMinutes();
      const inNight = f <= t ? (now >= f && now < t) : (now >= f || now < t);
      if (inNight) sec = sec * c.nightPct / 100;
    }
    if (c.hourCap > 0) {
      const k = hourKey(Date.now()), used = (this.state.hourly[k] || 0);
      sec = Math.max(0, Math.min(sec, c.hourCap * 60 - used));
    }
    return sec;
  };
  P.recordStat = function (ms, opt) {
    if (!(ms > 0) || opt.test || opt.kind === 'manual') return;
    const k = hourKey(Date.now());
    this.state.hourly[k] = (this.state.hourly[k] || 0) + ms / 1000;
    this.state.evCount = (this.state.evCount || 0) + 1;
    const keys = Object.keys(this.state.hourly).sort();
    if (keys.length > 24 * 14) for (const x of keys.slice(0, keys.length - 24 * 14)) delete this.state.hourly[x];
  };
  P.setPanic = function (mode) {
    const was = this.state.panic;
    this.state.panic = this.state.panic === mode ? '' : mode;
    // Pause-Bildschirm: Timer anhalten und danach weiterlaufen lassen
    if (this.cfg.pauseTimer) {
      if (this.state.panic === 'pause' && this.state.running) { this.pause(); this.state.pausedByScreen = true; }
      else if (was === 'pause' && this.state.panic !== 'pause' && this.state.pausedByScreen) { this.state.pausedByScreen = false; this.start(); }
    }
    this.pushLog(this.state.panic === 'pause' ? '☕ Pause-Bildschirm an' : '👁 Overlays wieder sichtbar', 0, 'info');
    this.addDiag('Pause-Bildschirm: ' + (this.state.panic === 'pause' ? 'an' : 'aus'));
    this.changed();
  };

  // ================= Backup =================
  P.backupData = function () {
    const { token, ...cfg } = this.cfg;
    return { app: 'Stream Marathon', version: this.version, date: new Date().toISOString(), cfg, profiles: this.prof, state: this.state };
  };
  P.writeBackup = function () {
    try {
      fs.mkdirSync(this.backupDir, { recursive: true });
      const d = new Date(), name = `backup-${d.toISOString().slice(0, 16).replace(/[:T]/g, '-')}.json`;
      fs.writeFileSync(path.join(this.backupDir, name), JSON.stringify(this.backupData()));
      const files = fs.readdirSync(this.backupDir).filter(f => f.startsWith('backup-')).sort();
      for (const f of files.slice(0, Math.max(0, files.length - 48))) fs.rmSync(path.join(this.backupDir, f), { force: true });
      this.lastBackup = Date.now();
      return name;
    } catch (e) { this.addDiag('Backup fehlgeschlagen: ' + e.message); return null; }
  };
  P.restoreBackup = function (data) {
    if (!data || data.app !== 'Stream Marathon' || !data.cfg) throw new Error('Keine gültige Backup-Datei');
    this.writeBackup();
    const keep = { token: this.cfg.token, adminHash: this.cfg.adminHash, installId: this.cfg.installId, port: this.cfg.port };
    const old = Object.assign({}, this.cfg);
    Object.assign(this.cfg, DEF, data.cfg, keep);
    if (data.profiles && data.profiles.list) this.prof = data.profiles;
    if (data.state && data.state.n) { this.state = Object.assign(this.newState(), data.state, { running: false }); }
    H.writeJson(this.cfgFile, this.cfg); H.writeJson(this.profFile, this.prof);
    this.pushLog('💾 Backup wiederhergestellt', 0, 'info');
    this.afterConfig(old); this.emit('config'); this.connIrc(); this.changed();
  };
  P.csv = function () {
    const q = s => '"' + String(s).replace(/"/g, '""') + '"';
    return '﻿Zeit;Ereignis;Sekunden\n' + this.log.map(l => [new Date(l.t).toLocaleString('de-DE'), q(l.text), Math.round(l.sec || 0)].join(';')).join('\n');
  };

  // ================= Update-Kanal =================
  P.updUrl = function () { return this.cfg.updChannel === 'beta' ? H.UPDATE_URL.replace(/app\/$/, 'beta/') : H.UPDATE_URL; };
  P.bootFile = function () { return path.join(this.dir, '..', 'updates', 'boot.json'); };
  P.isBadVersion = function (v) { const b = H.readJson(this.bootFile(), {}); return Array.isArray(b.bad) && b.bad.includes(v); };
  P.rollback = function () {
    const f = this.bootFile(), b = H.readJson(f, {});
    b.bad = Array.isArray(b.bad) ? b.bad : [];
    if (!b.bad.includes(this.version)) b.bad.push(this.version);
    H.writeJson(f, b);
    this.pushLog(`↺ Zurück zur vorherigen Version (statt ${this.version})`, 0, 'info'); this.changed();
    setTimeout(() => this.emit('restart'), 1500);
  };

  // ================= Chat-Befehle =================
  P.fill = function (text, user) {
    const c = this.cfg, s = this.state;
    const pad = n => String(n).padStart(2, '0');
    const t = Math.ceil(this.rem() / 1000);
    const zeit = Math.floor(t / 3600) + ':' + pad(Math.floor(t % 3600 / 60)) + ':' + pad(t % 60);
    const len = (c.startMin * 60 + s.added / 1000) / 3600;
    const val = { subs: s.n.s, bits: s.n.b, follows: s.n.f, raids: s.n.r || 0, hours: Math.floor(len) };
    const g = (c.goals || []).find(g => (val[g.type] || 0) < g.target) || (c.goals || [])[0] || { label: '–', type: 'subs', target: 0 };
    const top = Object.entries(s.top || {}).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([n, sec]) => `${n} (+${H.fmtDur(sec)})`).join(', ') || 'noch niemand';
    const hhLeft = Math.max(0, Math.ceil(((s.hh && s.hh.until) || 0) - Date.now()) / 60000);
    const map = {
      zeit, user: user || '', ziel: g.label, stand: H.num(val[g.type] || 0), zielwert: H.num(g.target), top3: top,
      subs: H.num(s.n.s), follows: H.num(s.n.f), bits: H.fmtDur(c.bits), t1: H.fmtDur(c.t1), follow: H.fmtDur(c.follow),
      faktor: (s.hh && s.hh.mult) || c.hhMult, rest: Math.round(hhLeft) + ' Min', laenge: H.fmtDur(len * 3600)
    };
    return String(text).replace(/\{(\w+)\}/g, (m, k) => k in map ? map[k] : m);
  };
  P.onChat = function (tags, text) {
    const c = this.cfg;
    if (!c.cmd_on || !text || text[0] !== '!') return;
    const word = text.trim().split(/\s+/)[0].toLowerCase();
    const cmd = (c.cmds || []).find(x => x.on && x.cmd === word);
    if (!cmd) return;
    if (word === '!happyhour' && !this.hhActive()) return;
    const now = Date.now();
    if (now - (this.cmdLast[word] || 0) < c.cmdCooldown * 1000) return;
    this.cmdLast[word] = now;
    this.sendChat(this.fill(cmd.text, tags['display-name'] || tags.login));
  };
  P.sendChat = async function (message) {
    const c = this.cfg;
    if (!c.token || !this.uid || !this.bid) return this.setStatus('chat', 'Twitch-Login fehlt (Bereich „Twitch“)');
    if (!(this.scopes || []).includes('user:write:chat')) return this.setStatus('chat', 'Berechtigung fehlt – im Bereich „Twitch“ neu verbinden');
    try {
      const r = await fetch('https://api.twitch.tv/helix/chat/messages', {
        method: 'POST', headers: { Authorization: 'Bearer ' + c.token, 'Client-Id': c.clientId, 'Content-Type': 'application/json' },
        body: JSON.stringify({ broadcaster_id: this.bid, sender_id: this.uid, message: message.slice(0, 480) })
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.data && j.data[0] && j.data[0].is_sent) this.setStatus('chat', 'aktiv – antwortet als ' + c.login);
      else this.setStatus('chat', 'Nachricht nicht gesendet: ' + ((j.data && j.data[0] && j.data[0].drop_reason && j.data[0].drop_reason.message) || j.message || r.status));
    } catch (e) { this.setStatus('chat', 'Fehler beim Senden'); }
  };

  // ================= Kanalpunkte =================
  P.helix = async function (method, url, body) {
    const c = this.cfg;
    const r = await fetch('https://api.twitch.tv/helix/' + url, {
      method, headers: { Authorization: 'Bearer ' + c.token, 'Client-Id': c.clientId, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined
    });
    const j = r.status === 204 ? {} : await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, j };
  };
  P.syncReward = async function (oldList) {
    const c = this.cfg, L = c.cp_rewards || [];
    const anyOn = c.cp_on && L.some(r => r.on);
    if (!c.token || !this.uid) { if (anyOn) this.setStatus('points', 'Twitch-Login fehlt (Bereich „Twitch“)'); return; }
    if (!(this.scopes || []).includes('channel:manage:redemptions')) { if (anyOn) this.setStatus('points', 'Berechtigung fehlt – im Bereich „Twitch“ neu verbinden'); return; }
    if (this.bid !== this.uid) { if (anyOn) this.setStatus('points', 'Geht nur mit dem Account des Kanals selbst'); return; }
    if (this._cpBusy) { this._cpAgain = true; return; }
    this._cpBusy = true;
    let err = null, active = 0, changedIds = false;
    try {
      // Gelöschte Belohnungen auch auf Twitch entfernen
      const keep = new Set(L.map(r => r.id).filter(Boolean));
      for (const o of oldList || []) {
        if (o.id && !keep.has(o.id)) await this.helix('DELETE', `channel_points/custom_rewards?broadcaster_id=${this.uid}&id=${o.id}`).catch(() => {});
      }
      for (const r of L) {
        const enabled = !!(c.cp_on && r.on);
        const body = {
          title: r.title, cost: r.cost, is_enabled: enabled, background_color: r.color,
          is_max_per_user_per_stream_enabled: r.max > 0, max_per_user_per_stream: Math.max(1, r.max),
          is_global_cooldown_enabled: r.cool > 0, global_cooldown_seconds: Math.max(1, r.cool),
          should_redemptions_skip_request_queue: true,
          prompt: 'Verlängert den Subathon-Timer um ' + H.fmtDur(r.sec) + '.'
        };
        let res = null;
        if (r.id) {
          res = await this.helix('PATCH', `channel_points/custom_rewards?broadcaster_id=${this.uid}&id=${r.id}`, body);
          if (res.status === 404) { r.id = ''; changedIds = true; }
        }
        if (!r.id && enabled) {
          res = await this.helix('POST', `channel_points/custom_rewards?broadcaster_id=${this.uid}`, body);
          if (res.ok && res.j.data && res.j.data[0]) { r.id = res.j.data[0].id; changedIds = true; }
        }
        if (res && !res.ok && !err) err = res.status === 403 ? 'Kanalpunkte gibt es nur für Twitch Affiliates und Partner'
          : res.status === 400 && /duplicate|title/i.test(res.j.message || '') ? `Name „${r.title}“ gibt es auf deinem Kanal schon`
          : 'Fehler: ' + (res.j.message || res.status);
        if (enabled && r.id) active++;
      }
      if (changedIds) { H.writeJson(this.cfgFile, this.cfg); this.broadcast(); }
      this.setStatus('points', err || (c.cp_on ? `aktiv – ${active} Belohnung${active === 1 ? '' : 'en'}` : 'aus (Belohnungen auf Twitch deaktiviert)'));
      if (this.esSession) this.subPoints(this.esSession);
    } catch (e) { this.setStatus('points', 'keine Verbindung zu Twitch'); }
    this._cpBusy = false;
    if (this._cpAgain) { this._cpAgain = false; this.syncReward(); }
  };
  P.subPoints = async function (session) {
    if (this.bid !== this.uid || this._cpSubFor === session || !(this.cfg.cp_rewards || []).some(r => r.id)) return;
    this._cpSubFor = session;
    const r = await this.helix('POST', 'eventsub/subscriptions', {
      type: 'channel.channel_points_custom_reward_redemption.add', version: '1',
      condition: { broadcaster_user_id: this.uid },
      transport: { method: 'websocket', session_id: session }
    }).catch(() => ({ ok: false }));
    if (!r.ok) { this._cpSubFor = null; this.addDiag('Kanalpunkte-Abo fehlgeschlagen: ' + ((r.j && r.j.message) || r.status)); }
  };
  P.extraSubscribe = function (session) {
    this.esSession = session;
    const sc = this.scopes || [];
    if (sc.includes('user:write:chat')) this.setStatus('chat', this.cfg.cmd_on ? 'aktiv – antwortet als ' + this.cfg.login : 'aus');
    else this.setStatus('chat', 'Berechtigung fehlt – im Bereich „Twitch“ neu verbinden');
    this.syncReward();
  };
  P.onEventSub = function (type, ev) {
    const rw = type === 'channel.channel_points_custom_reward_redemption.add' && ev.reward && (this.cfg.cp_rewards || []).find(r => r.id === ev.reward.id);
    if (rw && this.cfg.cp_on && rw.on) {
      this.addTime(rw.sec, `💠 ${ev.user_name} löst „${ev.reward.title}“ ein`, null, { kind: 'points', user: ev.user_name });
    }
  };

  // ================= YouTube =================
  P.connYouTube = function () {
    clearTimeout(this._yt);
    const c = this.cfg, gen = this._ytGen = (this._ytGen || 0) + 1;
    this.ytChat = null; this.ytPage = null;
    if (!c.yt_on) return this.setStatus('yt', 'aus');
    if (!c.yt_key || !c.yt_channel) return this.setStatus('yt', 'API-Schlüssel und Kanal eintragen');
    const api = (p) => fetch('https://www.googleapis.com/youtube/v3/' + p + '&key=' + encodeURIComponent(c.yt_key)).then(async r => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((j.error && j.error.message) || 'HTTP ' + r.status);
      return j;
    });
    const loop = async () => {
      if (gen !== this._ytGen) return;
      let wait = 5 * 60000;
      try {
        if (!this.ytChannelId) {
          const ch = c.yt_channel.replace(/^https?:\/\/(www\.)?youtube\.com\//, '').replace(/\/.*$/, '');
          if (/^UC[\w-]{20,}$/.test(ch)) this.ytChannelId = ch;
          else {
            const j = await api('channels?part=id&forHandle=' + encodeURIComponent(ch.startsWith('@') ? ch : '@' + ch));
            if (!j.items || !j.items[0]) throw new Error('Kanal nicht gefunden');
            this.ytChannelId = j.items[0].id;
          }
        }
        if (!this.ytChat) {
          const s = await api('search?part=id&type=video&eventType=live&channelId=' + this.ytChannelId);
          const vid = s.items && s.items[0] && s.items[0].id.videoId;
          if (!vid) { this.setStatus('yt', 'verbunden – wartet, bis du live bist'); }
          else {
            const v = await api('videos?part=liveStreamingDetails&id=' + vid);
            this.ytChat = v.items && v.items[0] && v.items[0].liveStreamingDetails && v.items[0].liveStreamingDetails.activeLiveChatId;
            this.ytPage = null; this.ytFirst = true;
          }
        }
        if (this.ytChat) {
          const j = await api('liveChat/messages?part=snippet,authorDetails&liveChatId=' + encodeURIComponent(this.ytChat) + (this.ytPage ? '&pageToken=' + this.ytPage : ''));
          this.ytPage = j.nextPageToken;
          if (!this.ytFirst) for (const m of j.items || []) this.onYouTube(m);
          this.ytFirst = false;
          this.setStatus('yt', 'live verbunden');
          wait = Math.max(c.yt_poll * 1000, j.pollingIntervalMillis || 0);
        }
      } catch (e) {
        if (/liveChat|ended|not found/i.test(e.message) && this.ytChat) { this.ytChat = null; wait = 60000; }
        this.setStatus('yt', 'Fehler: ' + e.message.slice(0, 120));
        if (/quota/i.test(e.message)) wait = 30 * 60000;
      }
      if (gen === this._ytGen) this._yt = setTimeout(loop, wait);
    };
    this.setStatus('yt', 'verbinde…');
    loop();
  };
  P.onYouTube = function (m) {
    const c = this.cfg, s = m.snippet || {}, who = (m.authorDetails && m.authorDetails.displayName) || 'Jemand';
    if (s.type === 'superChatEvent' || s.type === 'superStickerEvent') {
      const d = s.superChatDetails || s.superStickerDetails || {};
      const amount = (Number(d.amountMicros) || 0) / 1e6;
      if (amount > 0) this.addTime(amount * c.yt_sc, `▶ ${who}: Super ${s.type === 'superChatEvent' ? 'Chat' : 'Sticker'} ${d.amountDisplayString || amount}`, null, { kind: 'yt', user: who });
    } else if (s.type === 'newSponsorEvent') {
      this.addTime(c.yt_member, `▶ ${who} ist jetzt Mitglied`, null, { kind: 'yt', user: who });
    } else if (s.type === 'membershipGiftingEvent') {
      const n = (s.membershipGiftingDetails && s.membershipGiftingDetails.giftMembershipsCount) || 1;
      this.addTime(c.yt_member * n, `▶ ${who} verschenkt ${n} Mitgliedschaften`, null, { kind: 'yt', user: who });
    }
  };

  // ================= Event-Eingang (TikTok über Zusatz-Tools) =================
  P.onHook = function (q) {
    const c = this.cfg;
    const type = String(q.get('type') || 'gift').toLowerCase(), user = String(q.get('user') || 'Jemand').slice(0, 40);
    const src = String(q.get('source') || 'TikTok').slice(0, 20);
    let sec = Number(q.get('sec')) || 0, text = '';
    if (!sec) {
      if (type === 'gift') { const coins = Number(q.get('coins')) || 0, n = Number(q.get('count')) || 1; sec = coins * n * c.tt_coin; text = `${user} schickt ${q.get('gift') ? n + '× ' + q.get('gift') : coins * n + ' Coins'}`; }
      else if (type === 'follow') { sec = c.tt_follow; text = `${user} folgt`; }
      else if (type === 'share') { sec = c.tt_share; text = `${user} teilt den Stream`; }
      else if (type === 'sub') { sec = c.tt_sub; text = `${user} abonniert`; }
    }
    if (!c.tt_on || !(sec > 0)) return false;
    this.addTime(sec, `♪ ${src}: ${text || user}`, null, { kind: 'tiktok', user });
    return true;
  };

  // ================= Aktionen & HTTP =================
  P.extraAction = function (a, b) {
    if (a === 'pauseScreen') return this.setPanic('pause');
    if (a === 'dismissNews') { this.status.justUpdated = null; return this.broadcast(); }
    if (a.startsWith('profile.')) return this.profileAction(a.slice(8), b);
  };
  P.deck = function (cmd, q) {
    const map = {
      toggle: () => this.toggle(), start: () => this.start(), pause: () => this.pause(),
      add: () => this.addTime((Number(q.get('min')) || 5) * 60, 'Stream Deck', null, { manual: true, kind: 'manual' }),
      sub: () => this.addTime(-(Number(q.get('min')) || 5) * 60, 'Stream Deck', null, { manual: true, kind: 'manual' }),
      happyhour: () => this.hhToggle(), pausescreen: () => this.setPanic('pause'),
      profile: () => q.get('n') ? this.profileAction('nth', { n: q.get('n') }) : this.profileAction('next', {}),
      wheel: () => this.test('wheel')
    };
    if (!map[cmd]) return false;
    map[cmd](); return true;
  };
  P.deckStatus = function () {
    const t = Math.ceil(this.rem() / 1000), pad = n => String(n).padStart(2, '0');
    return { time: Math.floor(t / 3600) + ':' + pad(Math.floor(t % 3600 / 60)) + ':' + pad(t % 60), running: this.state.running,
      ended: this.state.ended, happyHour: this.hhActive(), panic: this.state.panic, profile: this.prof.active };
  };
  P.extraGet = async function (p, u, req, res, json) {
    const q = u.searchParams;
    if (p.startsWith('/deck/') || p === '/hook') {
      // Geheimer Schlüssel: verhindert, dass fremde Webseiten über deinen Browser Zeit hinzufügen
      if (q.get('k') !== this.cfg.deckKey) { json(403, { ok: false, error: 'Falscher Schlüssel (k=...)' }); return true; }
      res.setHeader('Access-Control-Allow-Origin', '*');
      if (p === '/deck/status') { json(200, this.deckStatus()); return true; }
      if (p === '/hook') { json(200, { ok: this.onHook(q) }); return true; }
      json(this.deck(p.slice(6), q) ? 200 : 404, this.deckStatus()); return true;
    }
    if (p === '/deck-plugin') {
      try {
        const buf = fs.readFileSync(path.join(__dirname, 'ui', 'StreamMarathon.streamDeckPlugin'));
        res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="StreamMarathon.streamDeckPlugin"' });
        res.end(buf);
      } catch (e) { json(404, { ok: false }); }
      return true;
    }
    if (p === '/api/admin/backup' || p === '/api/admin/csv') {
      if (!this.adminTokens.has(q.get('t')) || !this.isAdmin()) { json(403, { ok: false }); return true; }
      const csv = p.endsWith('csv'), d = new Date().toISOString().slice(0, 10);
      res.writeHead(200, { 'Content-Type': csv ? 'text/csv; charset=utf-8' : 'application/json',
        'Content-Disposition': `attachment; filename="StreamMarathon-${csv ? 'Verlauf' : 'Backup'}-${d}.${csv ? 'csv' : 'json'}"` });
      res.end(csv ? this.csv() : JSON.stringify(this.backupData(), null, 2));
      return true;
    }
    return false;
  };
  P.extraPost = async function (p, b, json) {
    if (p === '/api/admin/unlock') {
      if (!checkPw(b.pw || '', this.cfg.adminHash)) { this.addDiag('Admin: falsches Passwort'); json(200, { ok: false }); return true; }
      this.adminUntil = this.cfg.adminAutoLock ? Date.now() + 10 * 60000 : Infinity;
      const t = crypto.randomBytes(16).toString('hex'); this.adminTokens.add(t);
      this.addDiag('Admin entsperrt'); this.broadcast(); json(200, { ok: true, t }); return true;
    }
    if (p === '/api/support') {
      const msg = String(b.msg || '').trim();
      if (!msg) { json(200, { ok: false, error: 'Bitte eine Nachricht eingeben' }); return true; }
      if (!this.webhook) { json(200, { ok: false, error: 'Senden ist nicht eingerichtet' }); return true; }
      this.reportSupport({ cat: b.cat, msg, contact: b.contact }); this.addDiag('Support-Nachricht gesendet');
      json(200, { ok: true }); return true;
    }
    if (!p.startsWith('/api/admin/')) return false;
    if (!this.isAdmin()) { json(403, { ok: false, error: 'gesperrt' }); return true; }
    this.adminTouch();
    const a = p.slice(11);
    if (a === 'lock') { this.adminUntil = 0; this.adminTokens.clear(); this.broadcast(); json(200, { ok: true }); return true; }
    if (a === 'password') {
      if (!checkPw(b.old || '', this.cfg.adminHash)) { json(200, { ok: false, error: 'Altes Passwort falsch' }); return true; }
      if (String(b.pw || '').length < 4) { json(200, { ok: false, error: 'Mindestens 4 Zeichen' }); return true; }
      this.cfg.adminHash = hashPw(b.pw); H.writeJson(this.cfgFile, this.cfg); this.addDiag('Admin-Passwort geändert'); this.broadcast();
      json(200, { ok: true }); return true;
    }
    if (a === 'backupNow') { const n = this.writeBackup(); json(200, { ok: !!n, name: n }); return true; }
    if (a === 'restore') { try { this.restoreBackup(b.data); json(200, { ok: true }); } catch (e) { json(200, { ok: false, error: e.message }); } return true; }
    if (a === 'forceUpdate') { this._installing = false; this._apiAt = 0; await this.checkLatest(); const L = this.status.latest || {}; if (L.cdn && !this._installing) this.installUpdate(L.cdn); json(200, { ok: true }); return true; }
    if (a === 'rollback') { this.rollback(); json(200, { ok: true }); return true; }
    if (a === 'clearStats') { this.state.hourly = {}; this.state.evCount = 0; this.changed(); json(200, { ok: true }); return true; }
    if (a === 'publishNotice') {
      const id = 'n' + Date.now();
      const notice = { id, title: String(b.title || '').slice(0, 120), text: String(b.text || '').slice(0, 1000), level: b.level === 'warn' ? 'warn' : 'info', date: new Date().toISOString() };
      if (!notice.title && !notice.text) { json(200, { ok: false, error: 'Titel oder Text fehlt' }); return true; }
      const r = await this.ghWriteNotice(notice);
      // Sender sieht seine eigene Ankündigung jetzt auch sofort (zur Bestätigung) – nicht mehr automatisch ausblenden
      if (r.ok) { this.cfg.noticeSeen = ''; H.writeJson(this.cfgFile, this.cfg); this.setStatus('notice', notice); this.addDiag('Ankündigung gesendet'); this.broadcast(); }
      else this.addDiag('Ankündigung fehlgeschlagen: ' + r.error);
      json(200, r); return true;
    }
    if (a === 'clearNotice') {
      const r = await this.ghWriteNotice({ id: '', title: '', text: '', cleared: true, date: new Date().toISOString() });
      if (r.ok) { this.setStatus('notice', null); this.addDiag('Ankündigung entfernt'); this.broadcast(); }
      json(200, r); return true;
    }
    json(404, { ok: false }); return true;
  };

  // Schreibt notice.json ins GitHub-Repo (nur mit dem lokal hinterlegten Token des Entwicklers)
  P.ghWriteNotice = async function (notice) {
    const tok = this.cfg.ghToken;
    if (!tok) return { ok: false, error: 'Kein GitHub-Token hinterlegt' };
    if (typeof fetch === 'undefined') return { ok: false, error: 'Nicht verfügbar' };
    const m = /raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\//.exec(H.UPDATE_URL || '');
    if (!m) return { ok: false, error: 'Repo nicht erkannt' };
    const [, owner, repo, branch] = m;
    const api = `https://api.github.com/repos/${owner}/${repo}/contents/notice.json`;
    const head = { Authorization: 'Bearer ' + tok, Accept: 'application/vnd.github+json', 'User-Agent': 'StreamMarathon', 'X-GitHub-Api-Version': '2022-11-28' };
    let sha;
    try {
      const g = await fetch(api + '?ref=' + branch, { headers: head, cache: 'no-store' });
      if (g.ok) { sha = (await g.json()).sha; }
      else if (g.status === 401) return { ok: false, error: 'Token ungültig' };
      else if (g.status !== 404) return { ok: false, error: 'GitHub ' + g.status };
    } catch (e) { return { ok: false, error: 'Keine Verbindung zu GitHub' }; }
    const content = Buffer.from(JSON.stringify(notice, null, 2)).toString('base64');
    try {
      const r = await fetch(api, { method: 'PUT', headers: Object.assign({ 'Content-Type': 'application/json' }, head),
        body: JSON.stringify({ message: 'notice: ' + (notice.id || 'entfernt'), content, sha, branch }) });
      if (r.ok) return { ok: true };
      const j = await r.json().catch(() => ({}));
      if (r.status === 401) return { ok: false, error: 'Token ungültig' };
      if (r.status === 403 || r.status === 404) return { ok: false, error: 'Token fehlt das Recht „Contents: Read and write“' };
      return { ok: false, error: 'GitHub: ' + (j.message || r.status) };
    } catch (e) { return { ok: false, error: 'Keine Verbindung zu GitHub' }; }
  };
}

module.exports = { install, GLOBAL_KEYS, ADMIN_KEYS };
