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
PLAYLIST_HOURS = 50
UPDATE_URL = "https://raw.githubusercontent.com/ximjxstinn/stream-marathon/stream24/server.py"  # Twitch trennt nach 48 h sowieso, danach startet der Stream neu

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
    "server": "rtmp://ingest.global-contribute.live-video.net/app",
    "quality": "720p30",
    "shuffle": True,
    # Modus: "videos" (Playlist) oder "countdown" (Live-Timer)
    "mode": "countdown",
    "cd_theme": "release",            # "release", "konsole" oder "standard"
    "cd_title": "Grand Theft Auto VI",
    "cd_target": "2026-11-19T00:00",  # Ortszeit (Europe/Berlin)
    "cd_done": "GTA 6 IST DA!",
    "cd_color": "#ffc400",
    "cd_tile": "GTA VI",
    "cd_badge": "RELEASE-UHRZEIT · PS5-COUNTDOWN",
    "cd_head1": "GTA 6 ab ",
    "cd_hl": "0 Uhr",
    "cd_head2": "spielbar",
    "cd_sub": "Freischaltung am 19. November: Uhrzeit für Deutschland\\nund weltweit",
    "cd_game": "Grand Theft Auto VI",
    "cd_boxlabel": "Kann gespielt werden in:",
    "cd_volume": 60,
    "cd_shuffle_music": True,
    "cfg_version": 2,
}
TZ_NAME = "Europe/Berlin"
CD_DIR = os.path.join(BASE, "countdown")
os.makedirs(CD_DIR, exist_ok=True)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
FF_ENV = dict(os.environ, TZ=TZ_NAME)
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def get_cfg():
    cfg = dict(DEFAULT_CFG)
    stored = load_json(CFG_PATH, {})
    if stored and stored.get("cfg_version", 1) < 2:
        # Update auf Version 2: neues Release-Design + globaler Twitch-Ingest
        for k in ("cd_theme", "cd_color", "cd_title"):
            stored.pop(k, None)
        if stored.get("server") == "rtmp://live.twitch.tv/app":
            stored.pop("server")
        stored["cfg_version"] = 2
        save_json(CFG_PATH, {**cfg, **stored})
    cfg.update(stored)
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
# Alles, was sich im Countdown ändern kann (Texte, Zahlen, Hintergrundbild, Musik),
# wird LIVE ausgetauscht, ohne ffmpeg neu zu starten:
#   * Texte  -> Dateien, die ffmpeg jede Frame neu liest (drawtext reload=1)
#   * Bild   -> wird 1x pro Sekunde über eine Pipe nachgeschoben
#   * Musik  -> wird als Rohton über eine Pipe eingespeist (Songwechsel ohne Neustart)
RUN_DIR = os.path.join(CD_DIR, "run")
MUSIC_DIR = os.path.join(CD_DIR, "music")
BG_LAYER = os.path.join(CD_DIR, "bg_layer.png")
EMPTY_LAYER = os.path.join(RUN_DIR, "empty.png")
AUDIO_FIFO = os.path.join(RUN_DIR, "audio.pcm")
IMAGE_FIFO = os.path.join(RUN_DIR, "image.pipe")
for _d in (RUN_DIR, MUSIC_DIR):
    os.makedirs(_d, exist_ok=True)
for _f in (AUDIO_FIFO, IMAGE_FIFO):
    if not os.path.exists(_f):
        os.mkfifo(_f)

THEMES = {"release": "PS5-Release", "konsole": "Konsole", "standard": "Standard"}
MUSIC_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus")
# Einstellungen, bei denen der Stream wirklich neu starten muss
RESTART_KEYS = {"stream_key", "server", "mode", "cd_theme", "cd_color", "quality"}
SPINNER = "◐◓◑◒"


def font(kind="bold"):
    bold = next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)
    if not bold:
        raise RuntimeError("Schrift fehlt – bitte 'apt install fonts-dejavu-core' ausführen")
    alt = {"bold": bold, "regular": bold.replace("-Bold", ""),
           "light": bold.replace("-Bold", "-ExtraLight"),
           "mono": bold.replace("Sans-Bold", "SansMono")}[kind]
    if os.path.exists(alt):
        return alt
    reg = bold.replace("-Bold", "")
    return reg if os.path.exists(reg) else bold


def find_asset(kind):
    if kind == "bg":
        return BG_LAYER if os.path.exists(BG_LAYER) else None
    return list_music()[0] if list_music() else None


def list_music():
    return sorted(os.path.join(MUSIC_DIR, f) for f in os.listdir(MUSIC_DIR)
                  if f.lower().endswith(MUSIC_EXT))


def target_epoch(cfg):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    dt = datetime.strptime(cfg["cd_target"], "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(TZ_NAME))
    return dt.timestamp(), dt


_write_lock = threading.Lock()


def atomic_write(path, text, _cache={}):
    text = text if text.strip() else " "   # leere Datei mag drawtext nicht
    with _write_lock:
        if _cache.get(path) == text and os.path.exists(path):
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        _cache[path] = text


def color_hex(cfg):
    return "0x" + (re.sub(r"[^0-9a-fA-F]", "", cfg.get("cd_color", "")) + "ffc400")[:6]


class Ticker(threading.Thread):
    """Schreibt jede Viertelsekunde alle Texte des Countdowns in Dateien."""
    BAR_LEN = 60

    def __init__(self):
        super().__init__(daemon=True)
        self.n = 0

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa
                print("Ticker-Fehler:", e, flush=True)
            time.sleep(0.25)

    def tick(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        cfg = get_cfg()
        try:
            tgt, dt = target_epoch(cfg)
        except Exception:  # noqa
            tgt, dt = time.time(), datetime.now(ZoneInfo(TZ_NAME))
        now = time.time()
        rem = max(0, int(tgt - now))
        done = tgt - now <= 0
        d, h, m, s = rem // 86400, rem // 3600 % 24, rem // 60 % 60, rem % 60
        P = 14 * 86400
        frac = 1.0 if done else min(1.0, max(0.0, 1 - (tgt - now) / P))
        release = f"{WEEKDAYS[dt.weekday()]}, {dt.strftime('%d.%m.%Y')} · {dt.strftime('%H:%M')} Uhr"
        hide = lambda txt: " " if done else txt   # noqa
        show = lambda txt: txt if done else " "   # noqa
        files = {
            # Zahlen + Beschriftungen
            "nd": hide(f"{d:02d}"), "nh": hide(f"{h:02d}"), "nm": hide(f"{m:02d}"), "ns": hide(f"{s:02d}"),
            "colon": hide(":"),
            "l_tage": hide("TAGE"), "l_std": hide("STD"), "l_min": hide("MIN"), "l_sek": hide("SEK"),
            "l_stunden": hide("STUNDEN"), "l_minuten": hide("MINUTEN"), "l_sekunden": hide("SEKUNDEN"),
            "r_tage": hide("Tage"), "r_std": hide("Stunden"), "r_min": hide("Minuten"), "r_sek": hide("Sekunden"),
            "spin": hide(SPINNER[int(now) % 4]),
            # Texte aus dem Panel
            "title": cfg["cd_title"], "done": show(cfg["cd_done"]), "tile": cfg["cd_tile"],
            "release": "Release: " + release, "release_short": release,
            "badge": "●  " + cfg["cd_badge"], "badge_dot": "●",
            "head_full": cfg["cd_head1"] + cfg["cd_hl"], "head1": cfg["cd_head1"] or " ",
            "head2": cfg["cd_head2"], "sub": cfg["cd_sub"].replace("\\n", "\n"),
            "game": cfg["cd_game"], "boxlabel": hide(cfg["cd_boxlabel"]),
            # Konsolen-Design
            "k_games": "Spiele", "k_media": "Medien", "k_user": "xImJxstinn",
            "clock": datetime.now(ZoneInfo(TZ_NAME)).strftime("%H:%M"),
            "k_sub": hide("Vorgeladen · Spielbar in"),
            "k_btn": "Spielen" if done else "Vorbestellt",
            "pct": f"{int(frac * 100)} %",
            "bar_bg": "━" * self.BAR_LEN,
            "bar": "━" * max(1, int(round(frac * self.BAR_LEN))),
        }
        for name, text in files.items():
            atomic_write(os.path.join(RUN_DIR, name + ".txt"), text)
        self.n += 1


class ImageFeeder(threading.Thread):
    """Schiebt 1x pro Sekunde das aktuelle Hintergrundbild (oder ein leeres) in ffmpeg."""

    def __init__(self):
        super().__init__(daemon=True)
        self._cache = (None, None, b"")

    def _image(self):
        path = BG_LAYER if os.path.exists(BG_LAYER) else EMPTY_LAYER
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return self._cache[2]
        if self._cache[:2] != (path, mt):
            with open(path, "rb") as fh:
                self._cache = (path, mt, fh.read())
        return self._cache[2]

    def run(self):
        while True:
            try:
                with open(IMAGE_FIFO, "wb", buffering=0) as fd:   # wartet, bis ffmpeg liest
                    start, i = time.monotonic(), 0
                    while True:
                        ahead = start + i - time.monotonic()
                        if ahead > 1.5:
                            time.sleep(ahead - 1.5)
                        fd.write(self._image())
                        i += 1
            except (BrokenPipeError, OSError):
                time.sleep(0.2)


class AudioFeeder(threading.Thread):
    """Spielt die Musik-Playlist als Rohton in ffmpeg. Songwechsel/Upload/Lautstärke ohne Neustart."""
    RATE = 44100 * 2 * 2   # Bytes pro Sekunde (s16le, Stereo)
    CHUNK = 17640          # 0,1 s

    def __init__(self):
        super().__init__(daemon=True)
        self.skip = False      # aktuellen Song abbrechen (z. B. gelöscht)
        self.reload = False    # Lautstärke geändert -> gleicher Song, neue Lautstärke
        self.current = None

    def _decoder(self, song, pos=0.0):
        vol = max(0, min(100, int(get_cfg().get("cd_volume", 60)))) / 100
        return subprocess.Popen(
            ["ffmpeg", "-v", "error", "-ss", f"{pos:.2f}", "-i", song, "-vn",
             "-af", f"volume={vol}", "-f", "s16le", "-ar", "44100", "-ac", "2", "pipe:1"],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self):
        while True:
            try:
                with open(AUDIO_FIFO, "wb", buffering=0) as fd:
                    self._feed(fd)
            except (BrokenPipeError, OSError):
                time.sleep(0.2)

    def _feed(self, fd):
        start, sent = time.monotonic(), 0
        dec, order, pos = None, [], 0
        try:
            while True:
                if dec and (self.skip or self.reload):
                    dec.kill()
                    dec.wait()
                    if self.reload and self.current and os.path.exists(self.current):
                        dec = self._decoder(self.current, pos / self.RATE)
                    else:
                        dec = None
                    self.skip = self.reload = False
                if dec is None:
                    songs = list_music()
                    if songs:
                        order = [s for s in order if s in songs]
                        if not order:
                            order = songs[:]
                            if get_cfg().get("cd_shuffle_music", True):
                                random.shuffle(order)
                        self.current = order.pop(0)
                        pos = 0
                        dec = self._decoder(self.current)
                    else:
                        self.current = None
                data = dec.stdout.read(self.CHUNK) if dec else b"\0" * self.CHUNK
                if dec and len(data) < self.CHUNK:          # Song zu Ende
                    data += b"\0" * (-len(data) % 4)
                    dec.wait()
                    dec = None
                    if not data:
                        continue
                pos += len(data)
                ahead = start + sent / self.RATE - time.monotonic()
                if ahead > 0.5:
                    time.sleep(ahead - 0.5)
                fd.write(data)
                sent += len(data)
        finally:
            if dec:
                dec.kill()


def make_empty_layer():
    if not os.path.exists(EMPTY_LAYER):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black@0.0:s=16x16,format=rgba",
                        "-frames:v", "1", "-update", "1", EMPTY_LAYER], check=False)


def convert_bg(src, cfg):
    """Bereitet ein hochgeladenes Bild auf (Größe + abdunkeln) und tauscht es live aus."""
    w, h, _, _ = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    tmp = os.path.join(RUN_DIR, "bg_new.png")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf",
                        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                        f"drawbox=x=0:y=0:w=iw:h=ih:color=0x05060b@0.55:t=fill,format=rgb24",
                        "-frames:v", "1", "-update", "1", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Bild konnte nicht gelesen werden")
    os.replace(tmp, BG_LAYER)


def theme_filters(cfg, w, h):
    q = lambda p: "'" + p.replace("'", "") + "'"   # noqa
    B, R, L, M = (q(font(k)) for k in ("bold", "regular", "light", "mono"))
    col = color_hex(cfg)

    def T(name, fnt, size, color, x, y, extra=""):
        return (f"drawtext=fontfile={fnt}:textfile={q(os.path.join(RUN_DIR, name + '.txt'))}:reload=1:"
                f"expansion=none:fontsize={size}:fontcolor={color}:x={x}:y={y}{extra}")

    theme = cfg.get("cd_theme", "release")
    f = []
    if theme == "standard":
        f.append(f"drawbox=x=0:y=ih*0.30:w=iw:h=ih*0.40:color={col}@0.08:t=fill")
        f.append(T("title", B, "h*0.085", "white", "(w-text_w)/2", "h*0.13"))
        for k, lab, cx in (("nd", "l_tage", 0.2), ("nh", "l_stunden", 0.4), ("nm", "l_minuten", 0.6), ("ns", "l_sekunden", 0.8)):
            f.append(T(k, B, "h*0.17", col, f"w*{cx}-text_w/2", "h*0.36", ":shadowcolor=black@0.6:shadowx=4:shadowy=4"))
            f.append(T(lab, B, "h*0.032", "white@0.8", f"w*{cx}-text_w/2", "h*0.56"))
        f.append(T("release", B, "h*0.04", "white@0.85", "(w-text_w)/2", "h*0.76"))
        f.append(T("done", B, "h*0.13", col, "(w-text_w)/2", "h*0.40", ":shadowcolor=black@0.6:shadowx=5:shadowy=5"))

    elif theme == "konsole":
        f.append("vignette=PI/5")
        f.append(T("k_games", B, "h*0.034", "white", "w*0.05", "h*0.06"))
        f.append(T("k_media", R, "h*0.034", "white@0.55", "w*0.05+h*0.17", "h*0.06"))
        f.append(T("clock", B, "h*0.034", "white", "w*0.95-text_w", "h*0.06"))
        f.append(T("k_user", R, "h*0.03", "white@0.75", "w*0.95-text_w-h*0.17", "h*0.065"))
        f.append("drawbox=x=iw*0.05-4:y=ih*0.15-4:w=ih*0.22+8:h=ih*0.22+8:color=white:t=4")
        f.append("drawbox=x=iw*0.05:y=ih*0.15:w=ih*0.22:h=ih*0.22:color=0x0a2a7a@0.85:t=fill")
        f.append(T("tile", B, "h*0.05", "white", "w*0.05+(h*0.22-text_w)/2", "h*0.15+(h*0.22-text_h)/2"))
        for i in range(6):
            f.append(f"drawbox=x=iw*0.05+ih*0.25+{i}*ih*0.165:y=ih*0.185:w=ih*0.145:h=ih*0.145:color=white@0.10:t=fill")
        f.append(T("title", B, "h*0.07", "white", "w*0.05", "h*0.43"))
        f.append(T("k_sub", R, "h*0.032", "white@0.75", "w*0.05", "h*0.525"))
        for i, (k, lab) in enumerate((("nd", "l_tage"), ("nh", "l_std"), ("nm", "l_min"), ("ns", "l_sek"))):
            f.append(T(k, L, "h*0.13", "white", f"w*0.05+{i}*h*0.24", "h*0.585"))
            f.append(T(lab, B, "h*0.026", "white@0.7", f"w*0.05+{i}*h*0.24+4", "h*0.72"))
        f.append(T("done", L, "h*0.11", "white", "w*0.05", "h*0.56"))
        f.append("drawbox=x=iw*0.95-ih*0.30:y=ih*0.43:w=ih*0.30:h=ih*0.075:color=white:t=fill")
        f.append(T("k_btn", B, "h*0.03", "0x111111", "w*0.95-h*0.15-text_w/2", "h*0.43+(h*0.075-text_h)/2"))
        f.append(T("release_short", R, "h*0.03", "white@0.8", "w*0.05", "h*0.83"))
        f.append(T("pct", B, "h*0.03", "white", "w*0.95-text_w", "h*0.83"))
        bar = f"w*0.9/{Ticker.BAR_LEN}/0.602"
        f.append(T("bar_bg", M, bar, "white@0.22", "w*0.05", "h*0.865"))
        f.append(T("bar", M, bar, "white", "w*0.05", "h*0.865"))

    else:  # release (PS5-Release-Stil)
        # Badge (Pille mit Punkt)
        f.append(T("badge", B, "h*0.026", "white@0.92", "w*0.058+h*0.017", "h*0.085",
                   f":box=1:boxcolor=white@0.10:boxborderw={int(h * 0.017)}"))
        f.append(T("badge_dot", B, "h*0.026", col, "w*0.058+h*0.017", "h*0.085"))
        # Schlagzeile: ganze Zeile in Akzentfarbe, Anfang in Weiß darüber
        f.append(T("head_full", B, "h*0.09", col, "w*0.056", "h*0.16"))
        f.append(T("head1", B, "h*0.09", "white", "w*0.056", "h*0.16"))
        f.append(T("head2", B, "h*0.09", "white", "w*0.056", "h*0.265"))
        f.append("drawbox=x=iw*0.058:y=ih*0.395:w=ih*0.075:h=ih*0.009:color=" + col + ":t=fill")
        f.append(T("sub", B, "h*0.033", "white@0.93", "w*0.058", "h*0.445", f":line_spacing={int(h * 0.012)}"))
        # untere Leiste
        f.append("drawbox=x=0:y=ih*0.655:w=iw:h=ih*0.345:color=0x05060b@0.93:t=fill")
        f.append("drawbox=x=iw*0.045:y=ih*0.715:w=ih*0.2:h=ih*0.2:color=0x1c2244:t=fill")
        f.append("drawbox=x=iw*0.045:y=ih*0.715:w=ih*0.2:h=ih*0.2:color=white@0.25:t=2")
        f.append(T("tile", B, "h*0.04", "white", "w*0.045+(h*0.2-text_w)/2", "h*0.715+(h*0.2-text_h)/2"))
        f.append(T("game", R, "h*0.034", "white@0.88", "w*0.045+h*0.25", "h*0.795"))
        # Countdown-Box
        f.append("drawbox=x=iw*0.52:y=ih*0.715:w=iw*0.435:h=ih*0.2:color=black@0.35:t=fill")
        f.append(f"drawbox=x=iw*0.52:y=ih*0.715:w=iw*0.435:h=ih*0.2:color={col}:t=4")
        f.append(T("boxlabel", R, "h*0.027", "white@0.9", "w*0.528", "h*0.728"))
        f.append(T("spin", R, "h*0.06", "white@0.85", "w*0.548-text_w/2", "h*0.825"))
        for k, lab, cx in (("nd", "r_tage", 0.612), ("nh", "r_std", 0.694), ("nm", "r_min", 0.806), ("ns", "r_sek", 0.908)):
            f.append(T(lab, R, "h*0.024", "white@0.7", f"w*{cx}-text_w/2", "h*0.775"))
            f.append(T(k, L, "h*0.075", "white", f"w*{cx}-text_w/2", "h*0.815"))
        for cx in (0.75, 0.857):
            f.append(T("colon", R, "h*0.06", "white@0.85", f"w*{cx}-text_w/2", "h*0.825"))
        f.append(T("done", B, "h*0.06", col, "w*0.52+(w*0.435-text_w)/2", "h*0.715+(h*0.2-text_h)/2"))
    return f


def build_countdown_cmd(cfg, output, preview=False):
    """Baut den ffmpeg-Befehl, der den Countdown live rendert."""
    w, h, fps, vb = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    make_empty_layer()
    ticker.tick()
    theme = cfg.get("cd_theme", "release")
    re_ = [] if preview else ["-re"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    # Eingang 0: Grundfläche
    if theme == "konsole":
        cmd += re_ + ["-f", "lavfi", "-i",
                      f"gradients=s={w // 4}x{h // 4}:r={fps}:c0=0x020b2e:c1=0x0b3fb8:c2=0x041a5c:"
                      f"c3=0x1d6cf0:nb_colors=4:speed=0.0035:type=linear:seed=7"]
    else:
        base = "0x0e0e10" if theme == "standard" else "0x0b0e1a"
        cmd += re_ + ["-f", "lavfi", "-i", f"color=c={base}:s={w}x{h}:r={fps}"]
    # Eingang 1: Hintergrundbild (live austauschbar)
    if preview:
        cmd += ["-loop", "1", "-framerate", "1", "-i", BG_LAYER if os.path.exists(BG_LAYER) else EMPTY_LAYER]
    else:
        cmd += ["-thread_queue_size", "64", "-f", "image2pipe", "-framerate", "1", "-c:v", "png", "-i", IMAGE_FIFO]
    graph = (f"[0:v]scale={w}:{h},setsar=1,fps={fps},format=yuv420p[base];"
             f"[1:v]scale={w}:{h},format=rgba[img];"
             f"[base][img]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p,"
             + ",\n".join(theme_filters(cfg, w, h)) + "[v]")
    script = os.path.join(RUN_DIR, "filter_preview.txt" if preview else "filter.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(graph + "\n")
    if preview:
        return cmd + ["-filter_complex_script", script, "-map", "[v]", "-frames:v", "1", "-update", "1", output]
    # Eingang 2: Musik (live austauschbar)
    cmd += ["-thread_queue_size", "4096", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", AUDIO_FIFO]
    cmd += ["-filter_complex_script", script, "-map", "[v]", "-map", "2:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", vb, "-maxrate", vb, "-bufsize", vb, "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-f", "flv", "-flvflags", "no_duration_filesize", output]
    return cmd


ticker = Ticker()
image_feeder = ImageFeeder()
audio_feeder = AudioFeeder()


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
                    cmd = build_countdown_cmd(cfg, url)
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
                                             stdout=subprocess.DEVNULL, stderr=log, env=FF_ENV)
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
                time.sleep(2)


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
SESSIONS_PATH = os.path.join(BASE, "sessions.json")
SESSIONS = set(load_json(SESSIONS_PATH, []))

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
  <label>Design</label><select id="cd_theme"></select>
  <div class="hint">Design und Farbe wechseln = Stream startet einmal kurz neu. Alles andere wird live übernommen.</div>
  <label>Countdown bis (deutsche Zeit)</label><input type="datetime-local" id="cd_target">
  <label>Akzentfarbe</label><input type="color" id="cd_color" style="width:64px;height:40px;border:0;background:none">
  <div data-th="release">
   <label>Badge oben</label><input type="text" id="cd_badge" maxlength="60">
   <label>Schlagzeile Zeile 1 (weiß) + farbiger Teil</label>
   <div style="display:flex;gap:8px"><input type="text" id="cd_head1" maxlength="40"><input type="text" id="cd_hl" maxlength="30"></div>
   <label>Schlagzeile Zeile 2</label><input type="text" id="cd_head2" maxlength="40">
   <label>Untertitel (\n = neue Zeile)</label><input type="text" id="cd_sub" maxlength="200">
   <label>Spielname unten</label><input type="text" id="cd_game" maxlength="60">
   <label>Text über dem Countdown</label><input type="text" id="cd_boxlabel" maxlength="60">
  </div>
  <div data-th="konsole standard"><label>Titel</label><input type="text" id="cd_title" maxlength="80"></div>
  <div data-th="release konsole"><label>Text auf der Spielkachel</label><input type="text" id="cd_tile" maxlength="20"></div>
  <label>Text, wenn die Zeit abgelaufen ist</label><input type="text" id="cd_done" maxlength="60">
  <div class="btns"><button class="go" id="bCdSave">Speichern (live)</button><button id="bPrev">👁 Vorschau</button></div>
  <img id="prev" style="width:100%;border-radius:10px;margin-top:14px;display:none">
  <label>Hintergrundbild <span id="bgInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">Bild wählen<input type="file" id="bgFile" accept="image/png,image/jpeg,image/webp"></label><button id="bgDel">Entfernen</button></div>
  <label>Musik-Playlist <span id="muInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">＋ Songs hinzufügen<input type="file" id="muFile" accept="audio/*" multiple></label><button id="muSkip">⏭ Nächster Song</button></div>
  <div id="muList" style="margin-top:8px"></div>
  <label>Lautstärke: <span id="volV"></span> %</label><input type="range" id="cd_volume" min="0" max="100" style="width:100%">
  <label class="chk"><input type="checkbox" id="cd_shuffle_music"> Songs zufällig mischen</label>
  <div class="hint">Songs, Bild und Lautstärke werden live getauscht – der Stream läuft weiter. Nur lizenzfreie Musik verwenden (z. B. StreamBeats, NCS), sonst drohen DMCA-Strikes.</div>
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
 <label>Update</label>
 <div class="btns" style="margin-top:0"><button id="bUpd">⬆ Neueste Version installieren</button></div>
 <div class="hint">Lädt die neueste Version von GitHub. Der Stream startet dabei einmal kurz neu.</div>
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
 $('bgInfo').textContent=s.cd_bg?'✅':'(keins)';$('muInfo').textContent=s.music.length?`(${s.music.length} Songs)`:'(leer – Stille)';
 $('muList').innerHTML=s.music.map(m=>`<div class="vid"><div class="t">${m==s.now_playing?'🔊 ':''}${esc(m)}</div><button onclick="delSong(this.dataset.n)" data-n="${esc(m)}">Löschen</button></div>`).join('');
 if(!cfgLoaded){$('cd_theme').innerHTML=Object.entries(s.themes).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');
  for(const k of CDF)$(k).value=s.cfg[k];$('cd_shuffle_music').checked=s.cfg.cd_shuffle_music;$('cd_volume').value=s.cfg.cd_volume;$('volV').textContent=s.cfg.cd_volume;showTh()}
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
const CDF=['cd_theme','cd_target','cd_color','cd_badge','cd_head1','cd_hl','cd_head2','cd_sub','cd_game','cd_boxlabel','cd_title','cd_tile','cd_done'];
function showTh(){const t=$('cd_theme').value;document.querySelectorAll('[data-th]').forEach(e=>e.style.display=e.dataset.th.split(' ').includes(t)?'':'none')}
$('cd_theme').onchange=showTh;
$('bCdSave').onclick=async()=>{const b={};for(const k of CDF)b[k]=$(k).value;b.cd_shuffle_music=$('cd_shuffle_music').checked;
 const j=await api('/api/config',b);if(j.ok){setTimeout(showPrev,400);if(j.restarted)alert('Design geändert – Stream startet kurz neu.')}};
function showPrev(){$('prev').style.display='block';$('prev').src='/api/preview.png?'+Date.now()}
$('bPrev').onclick=showPrev;
let volT;$('cd_volume').oninput=e=>{$('volV').textContent=e.target.value;clearTimeout(volT);volT=setTimeout(()=>api('/api/config',{cd_volume:+e.target.value}),400)};
$('cd_shuffle_music').onchange=e=>api('/api/config',{cd_shuffle_music:e.target.checked});
function asset(kind,f){return new Promise(res=>{const x=new XMLHttpRequest();x.open('PUT',`/api/asset?kind=${kind}&name=${encodeURIComponent(f.name)}`);
 x.onload=()=>{if(x.status!=200)alert('Upload fehlgeschlagen: '+x.responseText);refresh();if(kind=='bg')showPrev();res()};x.send(f)})}
$('bgFile').onchange=e=>{if(e.target.files[0])asset('bg',e.target.files[0]);e.target.value=''};
$('muFile').onchange=async e=>{const fs=[...e.target.files];e.target.value='';$('muInfo').textContent='(lädt hoch …)';for(const f of fs)await asset('music',f)};
$('bgDel').onclick=async()=>{await api('/api/asset_delete',{kind:'bg'});refresh();showPrev()};
async function delSong(n){if(confirm('Song löschen?')){await api('/api/asset_delete',{kind:'music',name:n});refresh()}}
$('muSkip').onclick=async()=>{await api('/api/music_skip');setTimeout(refresh,800)};
$('bUpd').onclick=async()=>{if(!confirm('Neueste Version installieren? Der Stream startet einmal kurz neu.'))return;
 const j=await api('/api/update');if(j.ok){alert('Update installiert – Panel lädt in ein paar Sekunden neu.');setTimeout(()=>location.reload(),5000)}};
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
                cmd = build_countdown_cmd(get_cfg(), out, preview=True)
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=FF_ENV)
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
                save_json(SESSIONS_PATH, list(SESSIONS)[-50:])
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
            if data.get("cd_theme") in THEMES:
                upd["cd_theme"] = data["cd_theme"]
            for k, n in (("cd_tile", 20), ("cd_title", 80), ("cd_done", 60), ("cd_badge", 60),
                         ("cd_head1", 40), ("cd_hl", 30), ("cd_head2", 40), ("cd_sub", 200),
                         ("cd_game", 60), ("cd_boxlabel", 60)):
                if k in data:
                    upd[k] = str(data[k])[:n]
            if "cd_color" in data and re.fullmatch(r"#[0-9a-fA-F]{6}", str(data["cd_color"])):
                upd["cd_color"] = data["cd_color"]
            if "cd_volume" in data:
                try:
                    upd["cd_volume"] = max(0, min(100, int(data["cd_volume"])))
                except (TypeError, ValueError):
                    pass
            if "cd_shuffle_music" in data:
                upd["cd_shuffle_music"] = bool(data["cd_shuffle_music"])
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
            changed = {k for k, v in upd.items() if old.get(k) != v}
            if "cd_volume" in changed:
                audio_feeder.reload = True           # Lautstärke live ändern
            restart = bool(changed & RESTART_KEYS)
            if streamer.want and restart:
                streamer.restart()
            return self._send(200, {"ok": True, "restarted": restart and streamer.want})
        elif path == "/api/asset_delete":
            if data.get("kind") == "bg":
                if os.path.exists(BG_LAYER):
                    os.remove(BG_LAYER)              # wird live entfernt
            else:
                name = os.path.basename(str(data.get("name", "")))
                p = os.path.join(MUSIC_DIR, name)
                if name and os.path.exists(p):
                    if audio_feeder.current == p:
                        audio_feeder.skip = True
                    os.remove(p)
        elif path == "/api/music_skip":
            audio_feeder.skip = True
        elif path == "/api/update":
            return self._update()
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
        name = os.path.basename(unquote((qs.get("name") or [""])[0]))
        ext = os.path.splitext(name)[1].lower()
        allowed = {"bg": (".png", ".jpg", ".jpeg", ".webp"), "music": MUSIC_EXT}
        if kind not in allowed or ext not in allowed[kind]:
            return self._send(400, {"error": "Dateityp nicht erlaubt"})
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size < 300_000_000:
            return self._send(400, {"error": "Datei zu groß oder leer"})
        tmp = os.path.join(RUN_DIR, "upload_" + secrets.token_hex(6) + ext)
        remaining = size
        with open(tmp, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    os.remove(tmp)
                    return self._send(400, {"error": "Upload abgebrochen"})
                fh.write(chunk)
                remaining -= len(chunk)
        try:
            if kind == "bg":
                convert_bg(tmp, get_cfg())           # wird live ausgetauscht
                os.remove(tmp)
            else:
                base = re.sub(r"[^\w .()&-]+", "_", os.path.splitext(name)[0]).strip() or "song"
                dst, i = os.path.join(MUSIC_DIR, base + ext), 2
                while os.path.exists(dst):
                    dst, i = os.path.join(MUSIC_DIR, f"{base} ({i}){ext}"), i + 1
                os.replace(tmp, dst)                 # kommt automatisch in die Playlist
        except Exception as e:  # noqa
            if os.path.exists(tmp):
                os.remove(tmp)
            return self._send(400, {"error": str(e)})
        self._send(200, {"ok": True})

    def _update(self):
        """Lädt die neueste server.py von GitHub und startet das Panel neu."""
        import urllib.request
        try:
            with urllib.request.urlopen(UPDATE_URL, timeout=30) as r:
                code = r.read()
            compile(code, "server.py", "exec")
        except Exception as e:  # noqa
            return self._send(200, {"error": f"Update fehlgeschlagen: {e}"})
        me = os.path.abspath(__file__)
        with open(me + ".new", "wb") as fh:
            fh.write(code)
        os.replace(me + ".new", me)
        self._send(200, {"ok": True})
        threading.Timer(1.0, lambda: os._exit(0)).start()   # systemd startet neu

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
        "cfg": {k: v for k, v in cfg.items() if k.startswith("cd_")},
        "themes": THEMES,
        "cd_bg": os.path.exists(BG_LAYER),
        "music": [os.path.basename(m) for m in list_music()],
        "now_playing": os.path.basename(audio_feeder.current) if audio_feeder.current else "",
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
    make_empty_layer()
    ticker.tick()
    for t in (ticker, image_feeder, audio_feeder):
        t.start()
    # alte Einzel-Musikdatei (frühere Version) in die Playlist übernehmen
    for fn in os.listdir(CD_DIR):
        if fn.startswith("music.") and fn.lower().endswith(MUSIC_EXT):
            os.replace(os.path.join(CD_DIR, fn), os.path.join(MUSIC_DIR, "Song" + os.path.splitext(fn)[1]))
        if fn.startswith("bg.") and not os.path.exists(BG_LAYER):
            try:
                convert_bg(os.path.join(CD_DIR, fn), get_cfg())
                os.remove(os.path.join(CD_DIR, fn))
            except Exception:  # noqa
                pass
    streamer = Streamer()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"24/7 Stream Panel läuft auf Port {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
