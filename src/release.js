'use strict';
// Erzeugt den Update-Ordner (app/) mit manifest.json für GitHub.
// Aufruf: node release.js <Zielordner>
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const SRC = __dirname;
const OUT = path.resolve(process.argv[2] || path.join(SRC, 'dist', 'app'));
const FILES = ['shell.js', 'core.js', 'extras.js', 'changelog.json', 'package.json', 'icon.png', 'ui/StreamMarathon.streamDeckPlugin',
  ...fs.readdirSync(path.join(SRC, 'ui')).filter(f => f.endsWith('.html')).map(f => 'ui/' + f)];

const version = require(path.join(SRC, 'package.json')).version;
fs.rmSync(OUT, { recursive: true, force: true });
const files = {};
for (const f of FILES) {
  const buf = fs.readFileSync(path.join(SRC, f));
  fs.mkdirSync(path.dirname(path.join(OUT, f)), { recursive: true });
  fs.writeFileSync(path.join(OUT, f), buf);
  files[f] = crypto.createHash('sha256').update(buf).digest('hex');
}
fs.writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify({ version, date: new Date().toISOString(), files }, null, 2));
console.log(`Update ${version} → ${OUT} (${FILES.length} Dateien)`);
