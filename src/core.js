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
  showStats: true, st_f: true, st_s: true, st_b: true, st_r: false,
  pops: true, popDur: 3.2, popManual: false, sound: false, volume: 50,
  // Twitch-Login
  clientId: '', token: '', login: '',
  // Hotkeys
  hk_toggle: 'F9', hk_plus: 'F10', hk_minus: '', hkSec: 300,
  // Nutzungsmeldung: 'ask' | 'channel' | 'off'
  telemetry: 'ask', installId: '', lastPing: 0
};
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
  if (ti >= 0) line = line.slice(0, ti);
  const parts = line.split(' ');
  return { tags, command: parts[0], params: parts.slice(1) };
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
    // „anonym“ gibt es nicht mehr → wer das gewählt hatte, wird neu gefragt
    if (this.cfg.telemetry === 'anon') { this.cfg.telemetry = 'ask'; writeJson(this.cfgFile, this.cfg); }
    if (!this.cfg.installId) { this.cfg.installId = require('crypto').randomUUID(); writeJson(this.cfgFile, this.cfg); }
    const saved = readJson(this.stateFile, null);
    this.state = saved && saved.state && saved.state.n ? saved.state : this.newState();
    this.log = (saved && saved.log) || [];
    this.seen = new Set((saved && saved.seen) || []);
    this.logId = this.log.reduce((m, x) => Math.max(m, x.id), 0);
    this.status = { irc: 'aus', follow: 'aus', hotkeys: {} };
    this.clients = new Set();
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
    return { running: false, started: false, endsAt: 0, remaining: this.cfg.startMin * 60000, added: 0, ended: false, n: { f: 0, s: 0, b: 0, r: 0 } };
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
  saveCfg() { writeJson(this.cfgFile, this.cfg); }
  changed() { this.saveState(); this.broadcast(); }

  snapshot() {
    const { token, ...pub } = this.cfg;
    return { now: Date.now(), cfg: pub, hasToken: !!token, version: this.version, state: this.state, status: this.status, log: this.log.slice(-100) };
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
    if (s.ended && !opt.manual) {
      if (c.ignoreAfterEnd) { this.pushLog(text + ' (nach Ende ignoriert)', 0, 'info'); this.changed(); return; }
      s.running = true; s.endsAt = Date.now();
    }
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
    let label = text;
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
    this.state = this.newState(); this.seen.clear(); this.log = [];
    this.pushLog('Timer zurückgesetzt', 0, 'info'); this.changed();
  }
  tick() {
    const s = this.state;
    if (s.running && this.rem() <= 0) {
      s.running = false; s.remaining = 0; s.ended = true;
      this.pushLog('🏁 Marathon vorbei!', 0, 'info'); this.changed();
      this.report('marathon_end');
    }
  }

  // ---------- Nutzungsmeldung an den Entwickler (nur mit Zustimmung) ----------
  report(kind) {
    const c = this.cfg, s = this.state;
    if (!this.webhook || c.telemetry !== 'channel') return;
    if (typeof fetch === 'undefined') return;
    if (kind === 'start') {
      if (Date.now() - (c.lastPing || 0) < 6 * 3600000) return;
      c.lastPing = Date.now(); this.saveCfg();
    }
    const who = c.telemetry === 'channel' && c.channel ? c.channel : '';
    const link = who ? `[${who}](https://twitch.tv/${encodeURIComponent(who)})` : '*noch kein Kanal eingetragen*';
    const titles = {
      consent: ['🆕 Neue Installation', 0x00f593],
      start: ['▶️ App gestartet', 0x9146ff],
      marathon_start: ['⏱️ Marathon gestartet', 0xffb31a],
      marathon_end: ['🏁 Marathon beendet', 0xff4d6d]
    };
    const [title, color] = titles[kind] || [kind, 0x9146ff];
    const fields = [
      { name: 'Kanal', value: link, inline: true },
      { name: 'Version', value: VERSION, inline: true },
      { name: 'ID', value: '`' + c.installId.slice(0, 8) + '`', inline: true }
    ];
    const ev = [];
    if (c.en_follow) ev.push(`Follow ${fmtDur(c.follow)}`);
    if (c.en_sub) ev.push(`Sub T1 ${fmtDur(c.t1)} · T2 ${fmtDur(c.t2)} · T3 ${fmtDur(c.t3)} · Prime ${fmtDur(c.prime)}`);
    if (c.en_gift) ev.push(`Gift ${c.giftPct} %`);
    if (c.en_bits) ev.push(`100 Bits ${fmtDur(c.bits)}`);
    if (c.en_raid) ev.push(`Raid ${fmtDur(c.raid)}/Zuschauer`);
    if (kind !== 'marathon_end') {
      fields.push({ name: 'Startzeit', value: fmtDur(c.startMin * 60), inline: true });
      fields.push({ name: 'Maximal', value: c.capH > 0 ? c.capH + ' h' : 'unbegrenzt', inline: true });
      fields.push({ name: 'Design', value: `${c.font} · ${c.color} / ${c.accent}`, inline: true });
      fields.push({ name: 'Events', value: ev.join('\n') || 'alle aus' });
    } else {
      fields.push({ name: 'Länge', value: fmtDur(c.startMin * 60 + s.added / 1000), inline: true });
      fields.push({ name: 'Statistik', value: `❤️ ${num(s.n.f)} · ⭐ ${num(s.n.s)} · 💎 ${num(s.n.b)} · 🚀 ${num(s.n.r || 0)}`, inline: true });
    }
    fetch(this.webhook, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'Stream Marathon', embeds: [{ title, color, fields, timestamp: new Date().toISOString() }] })
    }).catch(() => {});
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
        this.addTime(this.tierSec(plan), `${tierName(plan)}-Sub von ${who}` + (id === 'resub' && months > 1 ? ` (${months} Monate)` : ''), { s: 1 }, { kind: 'sub' });
      } else if ((id === 'subgift' || id === 'anonsubgift') && c.en_gift) {
        const giver = id === 'anonsubgift' || t.login === 'ananonymousgifter' ? 'Anonym' : who;
        const to = t['msg-param-recipient-display-name'] || 'jemandem';
        this.addTime(this.tierSec(plan) * c.giftPct / 100, `${giver} schenkt ${to} einen ${tierName(plan)}-Sub`, { s: 1 }, { kind: 'gift' });
      } else if (id === 'raid' && c.en_raid) {
        const v = parseInt(t['msg-param-viewerCount'], 10) || 0;
        const from = t['msg-param-displayName'] || who;
        if (v >= c.raidMin) this.addTime(v * c.raid, `Raid von ${from} mit ${num(v)} Zuschauern`, { r: 1 }, { kind: 'raid' });
        else { this.pushLog(`Raid von ${from} mit ${v} Zuschauern (unter Minimum)`, 0, 'info'); this.changed(); }
      }
    }
    if (m.command === 'PRIVMSG' && t.bits && c.en_bits) {
      const b = parseInt(t.bits, 10) || 0;
      if (b >= Math.max(1, c.bitsMin)) this.addTime(b / 100 * c.bits, `${num(b)} Bits von ${who}`, { b }, { kind: 'bits' });
    }
  }

  onFollow(ev) {
    const c = this.cfg;
    if (!c.en_follow) return;
    const login = ev.user_login || ev.user_name;
    if (c.dedupe) {
      if (this.seen.has(login)) { this.pushLog(`Follow von ${ev.user_name} (schon gezählt)`, 0, 'info'); this.changed(); return; }
      this.seen.add(login);
    }
    this.addTime(c.follow, `Follow von ${ev.user_name}`, { f: 1 }, { kind: 'follow' });
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
    if (!c.en_follow) return this.setStatus('follow', 'deaktiviert');
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
          if (r.ok) this.setStatus('follow', 'verbunden als ' + this.cfg.login);
          else {
            const j = await r.json().catch(() => ({}));
            this.setStatus('follow', r.status === 403 ? 'Kein Zugriff – du musst Streamer oder Mod im Kanal sein' : 'Fehler: ' + (j.message || r.status));
          }
        } catch (err) { this.setStatus('follow', 'Fehler beim Anmelden der Follows'); }
      } else if (type === 'notification' && m.payload.subscription.type === 'channel.follow') {
        this.onFollow(m.payload.event);
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
      if (!(k in DEF) || ['token', 'login', 'installId', 'lastPing'].includes(k)) continue;
      if (k === 'telemetry' && !['channel', 'off'].includes(patch[k])) continue;
      const d = DEF[k], v = patch[k];
      if (typeof d === 'number') { const n = Number(v); if (isFinite(n)) this.cfg[k] = Math.max(0, n); }
      else if (typeof d === 'boolean') this.cfg[k] = !!v;
      else this.cfg[k] = String(v).slice(0, 300).trim();
    }
    this.cfg.channel = this.cfg.channel.toLowerCase().replace(/^#/, '').replace(/^https?:\/\/(www\.)?twitch\.tv\//, '').replace(/\/.*$/, '');
    if (!this.cfg.port || this.cfg.port < 1024 || this.cfg.port > 65535) this.cfg.port = old.port;
    this.cfg.port = Math.round(this.cfg.port);
    this.saveCfg();
    const s = this.state;
    if (this.cfg.startMin !== old.startMin && !s.started && !s.running && s.added === 0) s.remaining = this.cfg.startMin * 60000;
    const on = t => t === 'channel';
    if (on(this.cfg.telemetry) && !on(old.telemetry)) this.report('consent');
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
      '&response_type=token&scope=moderator%3Aread%3Afollowers&force_verify=true';
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
      if (p === '/auth') return html('auth.html');
      if (p === '/api/snap') return json(200, this.snapshot());
      if (p === '/api/authurl') return json(200, { url: this.cfg.clientId ? this.authUrl() : '' });
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
        this._ka = setInterval(() => { for (const c of this.clients) { try { c.write(': ka\n\n'); } catch (e) {} } }, 15000);
        this.connIrc();
        this.connFollow();
        setTimeout(() => this.report('start'), 5000);
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
    clearInterval(this._tick); clearInterval(this._save); clearInterval(this._ka);
    try { this.irc && this.irc.close(); } catch (e) {}
    try { this.es && this.es.close(); } catch (e) {}
    for (const c of this.clients) { try { c.end(); } catch (e) {} }
    (this.servers || []).forEach(s => { try { s.close(); } catch (e) {} });
  }
}

module.exports = { Core, DEF };
