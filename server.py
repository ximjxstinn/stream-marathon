#!/usr/bin/env python3
"""xImJxstinn 24/7 Stream – streamt Videos in Dauerschleife zu Twitch.

Läuft auf einem Linux-Server (z. B. Hetzner Cloud). Steuerung per Web-Panel
(handyfreundlich). Keine externen Python-Pakete nötig, nur ffmpeg.
"""
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import queue
import time
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.environ.get("STREAM24_DIR", "/opt/stream24")
CFG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
UPLOAD_DIR = os.path.join(BASE, "uploads")
TMP_DIR = os.path.join(BASE, "tmp")
VIDEO_DIR = os.path.join(BASE, "videos")
LOG_PATH = os.path.join(BASE, "stream.log")
PLAYLIST_PATH = os.path.join(BASE, "playlist.txt")
PORT = int(os.environ.get("STREAM24_PORT", "8090"))

# Name: (Breite, Höhe, FPS, Video-Bitrate)
QUALITIES = {
    "720p30": (1280, 720, 30, "3000k"),
    "720p60": (1280, 720, 60, "4500k"),
    "1080p30": (1920, 1080, 30, "4500k"),
    "1080p60": (1920, 1080, 60, "6000k"),
}
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".flv", ".avi", ".ts", ".m4v")
PLAYLIST_HOURS = 50  # Twitch trennt nach 48 h sowieso, danach startet der Stream neu

for d in (BASE, UPLOAD_DIR, TMP_DIR, VIDEO_DIR):
    os.makedirs(d, exist_ok=True)

lock = threading.RLock()


# ---------------------------------------------------------------- Config ----
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


DEFAULT_CFG = {
    "password_salt": "",
    "password_hash": "",
    "stream_key": "",
    "server": "rtmp://live.twitch.tv/app",
    "quality": "720p30",
    "shuffle": True,
    # Modus: "videos" (Playlist) oder "countdown" (Live-Timer)
    "mode": "countdown",
    "cd_title": "GTA 6 COUNTDOWN",
    "cd_target": "2026-11-19T00:00",   # Ortszeit (Europe/Berlin)
    "cd_done": "GTA 6 IST DA!",
    "cd_color": "#ff3cac",
}
TZ_NAME = "Europe/Berlin"
CD_DIR = os.path.join(BASE, "countdown")
os.makedirs(CD_DIR, exist_ok=True)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def get_cfg():
    cfg = dict(DEFAULT_CFG)
    cfg.update(load_json(CFG_PATH, {}))
    return cfg


def set_cfg(**kw):
    with lock:
        cfg = get_cfg()
        cfg.update(kw)
        save_json(CFG_PATH, cfg)
        return cfg


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()


def check_pw(pw):
    cfg = get_cfg()
    if not cfg["password_hash"]:
        return False
    return hmac.compare_digest(hash_pw(pw, cfg["password_salt"]), cfg["password_hash"])


# ---------------------------------------------------------------- Videos ----
def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60).stdout
        data = json.loads(out or "{}")
        dur = float(data.get("format", {}).get("duration") or 0)
        types = [s.get("codec_type") for s in data.get("streams", [])]
        return dur, "video" in types, "audio" in types
    except Exception:
        return 0.0, False, False


def safe_name(name):
    base = os.path.splitext(os.path.basename(name))[0]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "video"
    base = base[:60]
    cand, i = base, 2
    while (os.path.exists(os.path.join(VIDEO_DIR, cand + ".mp4"))
           or cand in converter.jobs):
        cand = f"{base}_{i}"
        i += 1
    return cand


def list_videos():
    vids = []
    for fn in sorted(os.listdir(VIDEO_DIR)):
        if not fn.endswith(".mp4"):
            continue
        name = fn[:-4]
        meta = load_json(os.path.join(VIDEO_DIR, name + ".json"), {})
        path = os.path.join(VIDEO_DIR, fn)
        vids.append({
            "name": name,
            "title": meta.get("title", name),
            "duration": meta.get("duration", 0),
            "quality": meta.get("quality", ""),
            "size": os.path.getsize(path),
            "path": path,
        })
    return vids


class Converter:
    """Wandelt hochgeladene Videos einmalig in ein einheitliches Format um.
    Dadurch kann der Stream danach mit '-c copy' laufen (fast keine CPU-Last)."""

    def __init__(self):
        self.q = queue.Queue()
        self.jobs = {}  # name -> {state, progress, title, error}
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, src, title):
        name = safe_name(title)
        self.jobs[name] = {"state": "wartet", "progress": 0, "title": title, "error": ""}
        self.q.put((name, src, title))
        return name

    def busy(self):
        return any(j["state"] in ("wartet", "konvertiert") for j in self.jobs.values())

    def _worker(self):
        while True:
            name, src, title = self.q.get()
            job = self.jobs[name]
            try:
                job["state"] = "konvertiert"
                self._convert(name, src, title, job)
                job["state"] = "fertig"
                job["progress"] = 100
                streamer.playlist_changed()
            except Exception as e:  # noqa
                job["state"] = "fehler"
                job["error"] = str(e)[:300]
            finally:
                try:
                    os.remove(src)
                except OSError:
                    pass

    def _convert(self, name, src, title, job):
        cfg = get_cfg()
        w, h, fps, vb = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
        dur, has_video, has_audio = probe(src)
        if not has_video:
            raise RuntimeError("Keine Videospur gefunden – ist das wirklich ein Video?")
        tmp_out = os.path.join(TMP_DIR, name + ".mp4")
        kbit = int(vb[:-1])
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
               "-progress", "pipe:1", "-i", src,
               "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
               "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                       f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1"),
               "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
               "-b:v", vb, "-maxrate", vb, "-bufsize", f"{kbit * 2}k",
               "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
               "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
               "-shortest", "-movflags", "+faststart", tmp_out]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in p.stdout:
            if line.startswith("out_time_us=") and dur > 0:
                try:
                    t = int(line.split("=")[1]) / 1_000_000
                    job["progress"] = max(0, min(99, int(t / dur * 100)))
                except ValueError:
                    pass
        err = p.stderr.read()
        if p.wait() != 0:
            raise RuntimeError("ffmpeg-Fehler: " + (err.strip().splitlines() or ["?"])[-1])
        out_dur, _, _ = probe(tmp_out)
        final = os.path.join(VIDEO_DIR, name + ".mp4")
        os.replace(tmp_out, final)
        save_json(os.path.join(VIDEO_DIR, name + ".json"),
                  {"title": title, "duration": out_dur or dur, "quality": cfg["quality"]})


# ------------------------------------------------------------- Countdown ----
def find_asset(kind):
    for fn in os.listdir(CD_DIR):
        if fn.startswith(kind + "."):
            return os.path.join(CD_DIR, fn)
    return None


def target_epoch(cfg):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    dt = datetime.strptime(cfg["cd_target"], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(TZ_NAME))
    return dt.timestamp(), dt


def build_countdown_cmd(cfg, start_epoch, output, preview=False):
    """Baut den ffmpeg-Befehl, der den Countdown live rendert."""
    w, h, fps, vb = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    font = next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)
    if not font:
        raise RuntimeError("Schrift fehlt – bitte 'apt install fonts-dejavu-core' ausführen")
    tgt, dt = target_epoch(cfg)
    T = round(tgt - start_epoch, 3)          # Sekunden bis zum Ziel, ab Stream-Start
    color = "0x" + (re.sub(r"[^0-9a-fA-F]", "", cfg["cd_color"]) + "ff3cac")[:6]
    run = os.path.join(CD_DIR, "run")
    os.makedirs(run, exist_ok=True)

    def tf(name, text):
        p = os.path.join(run, name + ".txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    R = f"({T}-t)"
    parts = {
        "d": f"%{{eif:max(0,floor({R}/86400)):d:2}}",
        "h": f"%{{eif:max(0,mod(floor({R}/3600),24)):d:2}}",
        "m": f"%{{eif:max(0,mod(floor({R}/60),60)):d:2}}",
        "s": f"%{{eif:max(0,mod(floor({R}),60)):d:2}}",
    }
    labels = {"d": "TAGE", "h": "STUNDEN", "m": "MINUTEN", "s": "SEKUNDEN"}
    release = f"{WEEKDAYS[dt.weekday()]}, {dt.strftime('%d.%m.%Y')} · {dt.strftime('%H:%M')} Uhr"
    q = lambda p: "'" + p.replace("'", "") + "'"  # noqa
    base = f"fontfile={q(font)}"
    on, off = f"enable='lt(t,{T})'", f"enable='gte(t,{T})'"
    f = []
    bg = find_asset("bg")
    f.append(f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps},format=yuv420p")
    if bg:
        f.append("drawbox=x=0:y=0:w=iw:h=ih:color=black@0.5:t=fill")
    else:
        f.append(f"drawbox=x=0:y=ih*0.30:w=iw:h=ih*0.40:color={color}@0.08:t=fill")
    f.append(f"drawtext={base}:textfile={q(tf('title', cfg['cd_title']))}:expansion=none:"
             f"fontsize=h*0.085:fontcolor=white:x=(w-text_w)/2:y=h*0.13")
    cols = {"d": 0.2, "h": 0.4, "m": 0.6, "s": 0.8}
    for k, cx in cols.items():
        f.append(f"drawtext={base}:textfile={q(tf('n' + k, parts[k]))}:fontsize=h*0.17:"
                 f"fontcolor={color}:shadowcolor=black@0.6:shadowx=4:shadowy=4:"
                 f"x=w*{cx}-text_w/2:y=h*0.36:{on}")
        f.append(f"drawtext={base}:textfile={q(tf('l' + k, labels[k]))}:expansion=none:"
                 f"fontsize=h*0.032:fontcolor=white@0.8:x=w*{cx}-text_w/2:y=h*0.56:{on}")
    f.append(f"drawtext={base}:textfile={q(tf('rel', 'Release: ' + release))}:expansion=none:"
             f"fontsize=h*0.04:fontcolor=white@0.85:x=(w-text_w)/2:y=h*0.76")
    f.append(f"drawtext={base}:textfile={q(tf('done', cfg['cd_done']))}:expansion=none:"
             f"fontsize=h*0.13:fontcolor={color}:shadowcolor=black@0.6:shadowx=5:shadowy=5:"
             f"x=(w-text_w)/2:y=h*0.40:{off}")
    script = os.path.join(run, "filter.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(",\n".join(f) + "[v]\n")

    re_ = [] if preview else ["-re"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    if bg:
        cmd += re_ + ["-loop", "1", "-framerate", str(fps), "-i", bg]
    else:
        cmd += re_ + ["-f", "lavfi", "-i", f"color=c=0x0e0e10:s={w}x{h}:r={fps}"]
    music = find_asset("music")
    if preview:
        return cmd + ["-filter_complex_script", script, "-map", "[v]", "-frames:v", "1", "-update", "1", output]
    if music:
        cmd += ["-re", "-stream_loop", "-1", "-i", music]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    kbit = int(vb[:-1])
    cmd += ["-filter_complex_script", script, "-map", "[v]", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", vb, "-maxrate", vb, "-bufsize", f"{kbit * 2}k",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-f", "flv", "-flvflags", "no_duration_filesize", output]
    return cmd


# -------------------------------------------------------------- Streamer ----
class Streamer:
    def __init__(self):
        self.want = False
        self.proc = None
        self.started_at = 0
        self.restarts = 0
        self.status_msg = "Gestoppt"
        self.thread = None
        self._dirty = False
        st = load_json(STATE_PATH, {})
        if st.get("want"):
            self.start()  # nach Server-Neustart automatisch weitermachen

    def _save_state(self):
        save_json(STATE_PATH, {"want": self.want})

    def start(self):
        with lock:
            self.want = True
            self._save_state()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()

    def stop(self):
        with lock:
            self.want = False
            self._save_state()
            self._kill()
            self.status_msg = "Gestoppt"

    def restart(self):
        self._kill()  # Loop startet automatisch neu

    def playlist_changed(self):
        self._dirty = True

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def live(self):
        return bool(self.proc and self.proc.poll() is None and time.time() - self.started_at > 8)

    def _write_playlist(self, vids, shuffle):
        total = sum(v["duration"] for v in vids) or 1
        lines, acc = ["ffconcat version 1.0"], 0
        while acc < PLAYLIST_HOURS * 3600 and len(lines) < 20000:
            order = vids[:]
            if shuffle:
                random.shuffle(order)
            for v in order:
                p = v["path"].replace("'", "'\\''")
                lines.append(f"file '{p}'")
                acc += v["duration"] or total
        with open(PLAYLIST_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _loop(self):
        while self.want:
            cfg = get_cfg()
            if not cfg["stream_key"]:
                self.status_msg = "Kein Stream-Key eingetragen"
                time.sleep(3)
                continue
            url = cfg["server"].rstrip("/") + "/" + cfg["stream_key"]
            if cfg["mode"] == "countdown":
                try:
                    cmd = build_countdown_cmd(cfg, time.time(), url)
                except Exception as e:  # noqa
                    self.status_msg = f"Fehler: {e}"
                    time.sleep(5)
                    continue
                info = "Countdown"
            else:
                vids = [v for v in list_videos() if v["quality"] in ("", cfg["quality"])]
                if not vids:
                    self.status_msg = "Keine fertigen Videos vorhanden"
                    time.sleep(3)
                    continue
                self._write_playlist(vids, cfg["shuffle"])
                self._dirty = False
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",
                       "-f", "concat", "-safe", "0", "-i", PLAYLIST_PATH,
                       "-c", "copy", "-f", "flv", "-flvflags", "no_duration_filesize", url]
                info = f"{len(vids)} Videos"
            try:
                if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5_000_000:
                    os.remove(LOG_PATH)
                log = open(LOG_PATH, "a", encoding="utf-8")
                log.write(time.strftime("\n[%Y-%m-%d %H:%M:%S] Stream startet "
                                        f"({info})\n"))
                log.flush()
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=log)
                self.started_at = time.time()
                self.status_msg = "Live"
                self.proc.wait()
                log.write(time.strftime("[%Y-%m-%d %H:%M:%S] ffmpeg beendet "
                                        f"(Code {self.proc.returncode})\n"))
                log.close()
            except Exception as e:  # noqa
                self.status_msg = f"Fehler: {e}"
            if self.want:
                self.restarts += 1
                self.status_msg = "Verbindet neu …"
                time.sleep(5)


def log_tail(n=25):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        key = get_cfg()["stream_key"]
        text = "".join(lines)
        return text.replace(key, "***") if key else text
    except OSError:
        return ""


converter = Converter()
streamer = None  # wird in main() erstellt


# ------------------------------------------------------------------- Web ----
SESSIONS = set()

LOGIN_HTML = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>24/7 Stream – Login</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e0e10;color:#efeff1;font-family:system-ui,sans-serif}
form{background:#18181b;padding:28px;border-radius:14px;width:min(320px,88vw)}
h1{font-size:20px;margin:0 0 16px}input,button{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #333;font-size:16px}
input{background:#0e0e10;color:#fff;margin-bottom:12px}button{background:#9147ff;color:#fff;border:0;font-weight:600}
.err{color:#ff6b6b;font-size:14px;margin-bottom:10px}</style></head><body>
<form method="post" action="/login"><h1>📺 24/7 Stream</h1>__ERR__
<input type="password" name="password" placeholder="Passwort" autofocus>
<button>Einloggen</button></form></body></html>"""

PANEL_HTML = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>24/7 Stream</title><style>
:root{--bg:#0e0e10;--card:#18181b;--line:#2f2f35;--txt:#efeff1;--mut:#adadb8;--acc:#9147ff;--red:#eb0400;--ok:#00c853}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,sans-serif;font-size:15px}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0}header a{color:var(--mut);font-size:14px}
main{max-width:720px;margin:0 auto;padding:16px;display:grid;gap:16px}
.card{background:var(--card);border-radius:14px;padding:18px}.card h2{font-size:15px;margin:0 0 14px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
.status{display:flex;align-items:center;gap:12px;font-size:22px;font-weight:700}
.dot{width:14px;height:14px;border-radius:50%;background:#555}.dot.live{background:var(--red);box-shadow:0 0 12px var(--red);animation:p 1.6s infinite}
@keyframes p{50%{opacity:.4}}.sub{color:var(--mut);font-size:14px;margin-top:6px}
.btns{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
button,.filebtn{padding:12px 16px;border-radius:9px;border:0;font-size:15px;font-weight:600;cursor:pointer;color:#fff;background:#3a3a3d}
.go{background:var(--acc)}.stop{background:var(--red)}button:disabled{opacity:.4}
.filebtn{display:inline-block;background:var(--acc)}input[type=file]{display:none}
.vid{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.vid:first-child{border-top:0}.vid .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vid small{color:var(--mut)}
.vid button{padding:7px 11px;font-size:13px;background:#3a3a3d}
.bar{height:6px;background:#333;border-radius:3px;margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
label{display:block;font-size:13px;color:var(--mut);margin:12px 0 6px}
input[type=text],input[type=password],select{width:100%;padding:11px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--txt);font-size:15px}
.chk{display:flex;align-items:center;gap:8px;margin-top:14px;color:var(--txt);font-size:15px}
pre{background:#000;color:#9f9;padding:12px;border-radius:8px;font-size:12px;max-height:220px;overflow:auto;white-space:pre-wrap;margin:0}
.hint{color:var(--mut);font-size:13px;margin-top:8px}.empty{color:var(--mut)}
</style></head><body>
<header><h1>📺 24/7 Stream</h1><a href="/logout">Logout</a></header>
<main>
<section class="card"><h2>Status</h2>
 <div class="status"><span class="dot" id="dot"></span><span id="st">…</span></div>
 <div class="sub" id="sub"></div>
 <div class="btns"><button class="go" id="bStart">▶ Starten</button><button class="stop" id="bStop">■ Stoppen</button><button id="bRe">↻ Neu starten</button></div>
</section>
<section class="card"><h2>Modus</h2>
 <div class="btns" style="margin-top:0"><button id="mCd">⏱ Countdown</button><button id="mVid">🎞 Videos</button></div>
 <div id="cdBox">
  <label>Überschrift</label><input type="text" id="cdTitle" maxlength="80">
  <label>Countdown bis (deutsche Zeit)</label><input type="datetime-local" id="cdTarget">
  <label>Text, wenn die Zeit abgelaufen ist</label><input type="text" id="cdDone" maxlength="80">
  <label>Farbe</label><input type="color" id="cdColor" style="width:64px;height:40px;border:0;background:none">
  <div class="btns"><button class="go" id="bCdSave">Speichern</button><button id="bPrev">👁 Vorschau</button></div>
  <label>Hintergrundbild <span id="bgInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">Bild wählen<input type="file" id="bgFile" accept="image/png,image/jpeg,image/webp"></label><button id="bgDel">Entfernen</button></div>
  <label>Hintergrundmusik <span id="muInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">Musik wählen<input type="file" id="muFile" accept="audio/*"></label><button id="muDel">Entfernen</button></div>
  <div class="hint">Nur lizenzfreie Musik verwenden, sonst gibt es DMCA-Strikes auf Twitch.</div>
  <img id="prev" style="width:100%;border-radius:10px;margin-top:14px;display:none">
 </div>
</section>
<section class="card" id="vidCard"><h2>Videos</h2>
 <label class="filebtn">＋ Videos hochladen<input type="file" id="file" accept="video/*" multiple></label>
 <div class="hint">Videos werden nach dem Upload einmal umgewandelt, danach landen sie automatisch in der Playlist.</div>
 <div id="uploads"></div><div id="jobs"></div><div id="vids" style="margin-top:10px"></div>
</section>
<section class="card"><h2>Einstellungen</h2>
 <label>Twitch Stream-Key <span id="keyinfo"></span></label>
 <input type="password" id="key" placeholder="live_xxxxxxxx (leer lassen = nicht ändern)">
 <label>Ingest-Server</label><input type="text" id="server">
 <label>Qualität</label><select id="quality"></select>
 <div class="hint" id="qhint"></div>
 <label class="chk"><input type="checkbox" id="shuffle"> Videos zufällig mischen</label>
 <div class="btns"><button class="go" id="bSave">Speichern</button></div>
</section>
<section class="card"><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
const fmt=s=>{s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h} h ${m} min`:`${m} min ${s%60} s`};
const mb=b=>(b/1048576).toFixed(0)+' MB';
let cfgLoaded=false;
async function api(p,body){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 if(r.status==401)location.reload();const j=await r.json();if(j.error)alert(j.error);return j}
async function refresh(){
 const r=await fetch('/api/status');if(r.status==401)return location.reload();const s=await r.json();
 $('dot').className='dot'+(s.live?' live':'');$('st').textContent=s.live?'LIVE':(s.running?s.message:'Gestoppt');
 $('sub').textContent=(s.live?`Läuft seit ${fmt(s.uptime)} · `:'')+`${s.videos.length} Videos (${fmt(s.total_duration)}) · Reconnects: ${s.restarts} · Speicher frei: ${s.disk_free_gb} GB`;
 $('bStart').disabled=s.running;$('bStop').disabled=!s.running;$('bRe').disabled=!s.running;
 $('vids').innerHTML=s.videos.length?s.videos.map(v=>`<div class="vid"><div class="t">${esc(v.title)}<br><small>${fmt(v.duration)} · ${mb(v.size)}${v.quality&&v.quality!=s.quality?' · ⚠ andere Qualität, wird übersprungen':''}</small></div><button onclick="del('${v.name}')">Löschen</button></div>`).join(''):'<div class="empty">Noch keine Videos.</div>';
 $('jobs').innerHTML=Object.entries(s.jobs).filter(([n,j])=>j.state!='fertig').map(([n,j])=>`<div class="vid"><div class="t" style="flex:1">${esc(j.title)}<br><small>${j.state}${j.error?': '+esc(j.error):''} ${j.state=='konvertiert'?j.progress+'%':''}</small><div class="bar"><i style="width:${j.progress}%"></i></div></div>${j.state=='fehler'?`<button onclick="dismiss('${n}')">OK</button>`:''}</div>`).join('');
 $('log').textContent=s.log||'(noch leer)';
 $('keyinfo').textContent=s.key_set?'✅ gesetzt':'❌ fehlt';
 const cd=s.mode=='countdown';$('mCd').className=cd?'go':'';$('mVid').className=cd?'':'go';
 $('cdBox').style.display=cd?'':'none';$('vidCard').style.opacity=cd?'.45':'1';
 $('bgInfo').textContent=s.cd_bg?'✅':'(Standard: dunkel)';$('muInfo').textContent=s.cd_music?'✅':'(Standard: still)';
 if(!cfgLoaded){$('cdTitle').value=s.cd_title;$('cdTarget').value=s.cd_target;$('cdDone').value=s.cd_done;$('cdColor').value=s.cd_color}
 if(!cfgLoaded){cfgLoaded=true;$('server').value=s.server;$('shuffle').checked=s.shuffle;
  $('quality').innerHTML=s.qualities.map(q=>`<option ${q==s.quality?'selected':''}>${q}</option>`).join('')}
 $('quality').disabled=s.quality_locked;$('qhint').textContent=s.quality_locked?'Qualität kann nur geändert werden, wenn keine Videos vorhanden sind (alle Videos müssen das gleiche Format haben).':'';
}
const esc=t=>String(t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function del(n){if(confirm('Video löschen?')){await api('/api/delete',{name:n});refresh()}}
async function dismiss(n){await api('/api/dismiss',{name:n});refresh()}
$('bStart').onclick=async()=>{await api('/api/start');refresh()};
$('bStop').onclick=async()=>{if(confirm('Stream wirklich stoppen?')){await api('/api/stop');refresh()}};
$('bRe').onclick=async()=>{await api('/api/restart');refresh()};
$('bSave').onclick=async()=>{const b={server:$('server').value,shuffle:$('shuffle').checked,quality:$('quality').value};
 if($('key').value.trim())b.stream_key=$('key').value.trim();const j=await api('/api/config',b);if(j.ok){$('key').value='';alert('Gespeichert ✅')}refresh()};
$('file').onchange=()=>{for(const f of $('file').files)upload(f);$('file').value=''};
function upload(f){const id='u'+Math.random().toString(36).slice(2);const el=document.createElement('div');el.className='vid';el.id=id;
 el.innerHTML=`<div class="t" style="flex:1">${esc(f.name)}<br><small>Upload 0%</small><div class="bar"><i style="width:0"></i></div></div>`;$('uploads').appendChild(el);
 const x=new XMLHttpRequest();x.open('PUT','/api/upload?name='+encodeURIComponent(f.name));
 x.upload.onprogress=e=>{const p=Math.round(e.loaded/e.total*100);el.querySelector('small').textContent=`Upload ${p}% (${mb(e.loaded)} / ${mb(e.total)})`;el.querySelector('i').style.width=p+'%'};
 x.onload=()=>{el.remove();if(x.status!=200)alert('Upload fehlgeschlagen: '+x.responseText);refresh()};
 x.onerror=()=>{el.querySelector('small').textContent='Upload fehlgeschlagen'};x.send(f)}
$('mCd').onclick=async()=>{await api('/api/config',{mode:'countdown'});refresh()};
$('mVid').onclick=async()=>{await api('/api/config',{mode:'videos'});refresh()};
$('bCdSave').onclick=async()=>{const j=await api('/api/config',{cd_title:$('cdTitle').value,cd_target:$('cdTarget').value,cd_done:$('cdDone').value,cd_color:$('cdColor').value});if(j.ok)showPrev()};
function showPrev(){$('prev').style.display='block';$('prev').src='/api/preview.png?'+Date.now()}
$('bPrev').onclick=showPrev;
function asset(kind,f){const x=new XMLHttpRequest();x.open('PUT',`/api/asset?kind=${kind}&name=${encodeURIComponent(f.name)}`);
 x.onload=()=>{if(x.status!=200)alert('Upload fehlgeschlagen: '+x.responseText);refresh();if(kind=='bg')showPrev()};x.send(f)}
$('bgFile').onchange=e=>{if(e.target.files[0])asset('bg',e.target.files[0]);e.target.value=''};
$('muFile').onchange=e=>{if(e.target.files[0])asset('music',e.target.files[0]);e.target.value=''};
$('bgDel').onclick=async()=>{await api('/api/asset_delete',{kind:'bg'});refresh();showPrev()};
$('muDel').onclick=async()=>{await api('/api/asset_delete',{kind:'music'});refresh()};
refresh();setInterval(refresh,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "Stream24"

    def log_message(self, *a):
        pass

    # helpers
    def _authed(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "s24" in c and c["s24"].value in SESSIONS

    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to, cookie=None):
        self.send_response(303)
        self.send_header("Location", to)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/logout":
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if "s24" in c:
                SESSIONS.discard(c["s24"].value)
            return self._redirect("/", "s24=; Max-Age=0; Path=/")
        if not self._authed():
            if path.startswith("/api/"):
                return self._send(401, {"error": "Nicht eingeloggt"})
            return self._send(200, LOGIN_HTML.replace("__ERR__", ""), "text/html")
        if path == "/":
            return self._send(200, PANEL_HTML, "text/html")
        if path == "/api/status":
            return self._send(200, status())
        if path == "/api/preview.png":
            out = os.path.join(CD_DIR, "preview.png")
            try:
                cmd = build_countdown_cmd(get_cfg(), time.time(), out, preview=True)
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip()[-300:])
                with open(out, "rb") as fh:
                    img = fh.read()
            except Exception as e:  # noqa
                return self._send(500, {"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(img)
        self._send(404, {"error": "Nicht gefunden"})

    # POST
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            form = parse_qs(self._body().decode(errors="replace"))
            pw = (form.get("password") or [""])[0]
            if check_pw(pw):
                tok = secrets.token_urlsafe(32)
                SESSIONS.add(tok)
                return self._redirect("/", f"s24={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
            time.sleep(1)
            return self._send(200, LOGIN_HTML.replace("__ERR__", '<div class="err">Falsches Passwort</div>'), "text/html")
        if not self._authed():
            return self._send(401, {"error": "Nicht eingeloggt"})
        try:
            data = json.loads(self._body() or b"{}")
        except ValueError:
            data = {}
        if path == "/api/start":
            streamer.start()
        elif path == "/api/stop":
            streamer.stop()
        elif path == "/api/restart":
            streamer.restart()
        elif path == "/api/config":
            upd = {}
            if "stream_key" in data:
                upd["stream_key"] = str(data["stream_key"]).strip()
            if "server" in data and str(data["server"]).startswith(("rtmp://", "rtmps://")):
                upd["server"] = str(data["server"]).strip()
            if "shuffle" in data:
                upd["shuffle"] = bool(data["shuffle"])
            if data.get("mode") in ("videos", "countdown"):
                upd["mode"] = data["mode"]
            for k in ("cd_title", "cd_done"):
                if k in data:
                    upd[k] = str(data[k])[:80]
            if "cd_color" in data and re.fullmatch(r"#[0-9a-fA-F]{6}", str(data["cd_color"])):
                upd["cd_color"] = data["cd_color"]
            if "cd_target" in data:
                try:
                    target_epoch({"cd_target": str(data["cd_target"])[:16]})
                    upd["cd_target"] = str(data["cd_target"])[:16]
                except Exception:  # noqa
                    return self._send(200, {"error": "Ungültiges Datum"})
            q = data.get("quality")
            if q and q in QUALITIES and q != get_cfg()["quality"]:
                if list_videos() or converter.busy():
                    return self._send(200, {"error": "Qualität kann nur ohne Videos geändert werden."})
                upd["quality"] = q
            old = get_cfg()
            set_cfg(**upd)
            if streamer.want and any(old.get(k) != v for k, v in upd.items() if k != "shuffle"):
                streamer.restart()
        elif path == "/api/asset_delete":
            p = find_asset("bg" if data.get("kind") == "bg" else "music")
            if p:
                os.remove(p)
            if streamer.want and get_cfg()["mode"] == "countdown":
                streamer.restart()
        elif path == "/api/delete":
            name = re.sub(r"[^A-Za-z0-9._-]", "", str(data.get("name", "")))
            for ext in (".mp4", ".json"):
                try:
                    os.remove(os.path.join(VIDEO_DIR, name + ext))
                except OSError:
                    pass
            if streamer.want:
                streamer.restart()
        elif path == "/api/dismiss":
            converter.jobs.pop(str(data.get("name", "")), None)
        else:
            return self._send(404, {"error": "Nicht gefunden"})
        self._send(200, {"ok": True})

    # PUT (Upload, roh im Body – funktioniert auch mit großen Dateien)
    def do_PUT(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "Nicht eingeloggt"})
        if u.path == "/api/asset":
            return self._asset_upload(u)
        if u.path != "/api/upload":
            return self._send(404, {"error": "Nicht gefunden"})
        name = unquote((parse_qs(u.query).get("name") or ["video.mp4"])[0])
        if not name.lower().endswith(VIDEO_EXT):
            return self._send(400, {"error": "Dateityp nicht unterstützt"})
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0:
            return self._send(400, {"error": "Leere Datei"})
        free = shutil.disk_usage(BASE).free
        if size * 2.2 > free:
            return self._send(400, {"error": "Nicht genug Speicherplatz auf dem Server"})
        dst = os.path.join(UPLOAD_DIR, secrets.token_hex(8) + os.path.splitext(name)[1].lower())
        remaining = size
        try:
            with open(dst, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise IOError("Upload abgebrochen")
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:  # noqa
            try:
                os.remove(dst)
            except OSError:
                pass
            return self._send(400, {"error": str(e)})
        title = os.path.splitext(os.path.basename(name))[0]
        job = converter.add(dst, title)
        self._send(200, {"ok": True, "job": job})

    def _asset_upload(self, u):
        qs = parse_qs(u.query)
        kind = (qs.get("kind") or [""])[0]
        name = unquote((qs.get("name") or [""])[0]).lower()
        ext = os.path.splitext(name)[1]
        allowed = {"bg": (".png", ".jpg", ".jpeg", ".webp"),
                   "music": (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac")}
        if kind not in allowed or ext not in allowed[kind]:
            return self._send(400, {"error": "Dateityp nicht erlaubt"})
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size < 300_000_000:
            return self._send(400, {"error": "Datei zu groß oder leer"})
        data = b""
        while len(data) < size:
            chunk = self.rfile.read(min(1 << 20, size - len(data)))
            if not chunk:
                return self._send(400, {"error": "Upload abgebrochen"})
            data += chunk
        old = find_asset(kind)
        if old:
            os.remove(old)
        with open(os.path.join(CD_DIR, kind + ext), "wb") as fh:
            fh.write(data)
        if streamer.want and get_cfg()["mode"] == "countdown":
            streamer.restart()
        self._send(200, {"ok": True})


def status():
    cfg = get_cfg()
    vids = list_videos()
    return {
        "running": streamer.want,
        "live": streamer.live(),
        "message": streamer.status_msg,
        "uptime": time.time() - streamer.started_at if streamer.live() else 0,
        "restarts": streamer.restarts,
        "videos": [{k: v[k] for k in ("name", "title", "duration", "size", "quality")} for v in vids],
        "total_duration": sum(v["duration"] for v in vids),
        "jobs": converter.jobs,
        "key_set": bool(cfg["stream_key"]),
        "server": cfg["server"],
        "quality": cfg["quality"],
        "qualities": list(QUALITIES),
        "quality_locked": bool(vids) or converter.busy(),
        "shuffle": cfg["shuffle"],
        "mode": cfg["mode"],
        "cd_title": cfg["cd_title"],
        "cd_target": cfg["cd_target"],
        "cd_done": cfg["cd_done"],
        "cd_color": cfg["cd_color"],
        "cd_bg": bool(find_asset("bg")),
        "cd_music": bool(find_asset("music")),
        "disk_free_gb": round(shutil.disk_usage(BASE).free / 1e9, 1),
        "log": log_tail(),
    }


def main():
    global streamer
    if len(sys.argv) >= 3 and sys.argv[1] == "--set-password":
        salt = secrets.token_hex(16)
        set_cfg(password_salt=salt, password_hash=hash_pw(sys.argv[2], salt))
        print("Passwort gesetzt.")
        return
    if not get_cfg()["password_hash"]:
        print("Kein Passwort gesetzt! Erst: python3 server.py --set-password DEINPASSWORT")
        sys.exit(1)
    # alte, halbfertige Uploads aufräumen
    for d in (UPLOAD_DIR, TMP_DIR):
        for fn in os.listdir(d):
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass
    streamer = Streamer()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"24/7 Stream Panel läuft auf Port {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
