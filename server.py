#!/usr/bin/env python3
"""xImJxstinn 24/7 Stream – streamt Videos in Dauerschleife zu Twitch.

Läuft auf einem Linux-Server (z. B. Hetzner Cloud). Steuerung per Web-Panel
(handyfreundlich). Keine externen Python-Pakete nötig, nur ffmpeg.
"""
import hashlib
import io
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
WATCHDOG_SECONDS = 15  # so lange darf der Stream "hängen", bevor neu verbunden wird
UPDATE_URL = "https://raw.githubusercontent.com/ximjxstinn/stream-marathon/stream24/server.py"
SENDER_URL = "https://raw.githubusercontent.com/ximjxstinn/stream-marathon/stream24/sender.py"  # Twitch trennt nach 48 h sowieso, danach startet der Stream neu

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
    "tw_enabled": True,
    "yt_enabled": False,
    "yt_key": "",
    "yt_server": "rtmp://a.rtmp.youtube.com/live2",
    "tw_max_hours": 47.5,             # Twitch beendet Streams nach 48 h
    "yt_max_hours": 11.83,            # YouTube speichert nur 12 h – vorher neuen Livestream starten
    "quality": "720p30",
    "shuffle": True,
    # Modus: "videos" (Playlist) oder "countdown" (Live-Timer)
    "mode": "countdown",
    "cd_theme": "ps5",                # "ps5", "release", "konsole" oder "standard"
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
    "cfg_version": 4,
    "vod_channel": "xImJxstinn",
    "show_runtime": True,             # "● LIVE seit …" im Stream einblenden
    "auto_update": True,              # neue Versionen automatisch installieren
    "title_auto": False,              # Stream-Titel automatisch setzen
    "title_countdown": "{game} Countdown ⏳ 24/7 LIVE bis zum Release",
    "title_done": "{done} 🎉 24/7 LIVE",
    "title_vod": "(VOD) {title}",
    "title_fallback": "Wiederholung",
    "cat_countdown": "{game}",        # Twitch-Kategorie beim Countdown
    "cat_vod": "Just Chatting",       # Twitch-Kategorie bei VODs, wenn das Spiel unbekannt ist
    "cat_vod_auto": True,             # bei VODs automatisch das Spiel aus dem VOD nehmen
    "yt_category": "20",              # YouTube-Kategorie (20 = Gaming)
    "vod_audio": "music",             # Ton bei VODs: orig | mute | music | mix
    "vod_mix_volume": 30,             # bei "mix": Lautstärke des VOD-Tons in %
    "tw_client_id": "",
    "yt_client_id": "",
    "yt_client_secret": "",
    "game_auto": True,                # nach dem Release automatisch zum nächsten Spiel wechseln
    "game_done_hours": 12,            # so lange "… IST DA!" zeigen, dann nächstes Spiel
    "game_current": "gta6",
    "game_next": "",                  # leer = automatisch das nächste nach Datum
    "games_custom": [],
    "chat_enabled": True,             # Twitch-Chat mitlesen
    "chat_on_stream": True,           # Chat im Stream anzeigen
    "chat_channel": "ximjxstinn",
    "chat_blocklist": "",
    "chat_ignore": "nightbot,streamelements,moobot,streamlabs,fossabot",
    "chat_keep_min": 10,
    "discord_link": "",
    "tiktok_link": "",
    "perf_preset": "veryfast",        # Encoder-Einstellung für Live-Bild (wird bei Überlastung automatisch schneller)
    "perf_auto": True,                # bei Überlastung automatisch sparsamer werden
    "runtime_label": "LIVE seit",
    "runtime_mode": "start",          # "start" = seit Start, "detail" = Twitch · YouTube · Gesamt
    "runtime_pos": "tr",              # Position der Einblendung (tl, tc, tr, bl, bc, br)
    "tw_playlist": [],                # Twitch-VODs, die direkt (ohne Speichern) abgespielt werden
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
    if stored and stored.get("cfg_version", 1) < 3:
        stored["cd_theme"] = "ps5"           # Update auf Version 3: PS5-Startseite als Design
        stored["cfg_version"] = 3
        save_json(CFG_PATH, {**cfg, **stored})
    if stored and stored.get("cfg_version", 1) < 4:
        if float(stored.get("yt_max_hours", 23.5) or 0) > 11.84:
            stored["yt_max_hours"] = 11.83   # Update auf Version 4: YouTube-Limit 12 h (11:50)
        stored["cfg_version"] = 4
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
        cmd = ["nice", "-n", "19", "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
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
BG_RAW = os.path.join(CD_DIR, "bg_layer.rgba")
BG_ORIG = os.path.join(CD_DIR, "bg_orig.png")        # hochgeladenes Hintergrundbild (unverändert)
COVER_ORIG = os.path.join(CD_DIR, "cover_orig.png")  # hochgeladenes Cover (quadratisch)
EMPTY_LAYER = os.path.join(RUN_DIR, "empty.png")
AUDIO_FIFO = os.path.join(RUN_DIR, "audio.pcm")
IMAGE_FIFO = os.path.join(RUN_DIR, "image.pipe")
for _d in (RUN_DIR, MUSIC_DIR):
    os.makedirs(_d, exist_ok=True)
for _f in (AUDIO_FIFO, IMAGE_FIFO):
    if not os.path.exists(_f):
        os.mkfifo(_f)

THEMES = {"ps5": "PS5-Startseite (Bild + Countdown-Leiste)", "release": "PS5-Release (Schlagzeile)",
          "konsole": "Konsole (blau, Kacheln)", "standard": "Standard"}
MUSIC_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus", ".mka")
MUSIC_UPLOAD_EXT = MUSIC_EXT + (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".wma", ".aiff", ".aif")  # Videos: nur der Ton wird genutzt
# Einstellungen, bei denen der Stream wirklich neu starten muss
RESTART_KEYS = {"mode", "cd_theme", "cd_color", "quality", "perf_preset"}   # Bild-Quelle neu
RELAY_KEYS = {"twitch": {"stream_key", "server", "tw_enabled"}, "youtube": {"yt_key", "yt_server", "yt_enabled"}}
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


PRESETS = ["veryfast", "superfast", "ultrafast"]    # von "beste Qualität" bis "am sparsamsten"


def live_preset(cfg):
    p = cfg.get("perf_preset", "veryfast")
    return p if p in PRESETS else "veryfast"


RUNTIME_POS = {"tl": "oben links", "tc": "oben Mitte", "tr": "oben rechts",
               "bl": "unten links", "bc": "unten Mitte", "br": "unten rechts"}


class Ticker(threading.Thread):
    """Schreibt jede Viertelsekunde alle Texte des Countdowns in Dateien."""
    BAR_LEN = 60

    def _chat_files(self, cfg):
        out = {}
        on = cfg.get("chat_enabled", True) and cfg.get("chat_on_stream", True)
        lay = CHAT_LAYOUT.get(cfg.get("cd_theme", "ps5"), CHAT_LAYOUT["ps5"])
        lines = twitch_chat.lines(lay[3]) if on else []
        lines = lines + [("", "")] * (CHAT_LINES - len(lines))        # direkt unter der Überschrift, neueste unten
        for i, (full, name) in enumerate(lines):
            out[f"chat_{i}"] = full or " "
            out[f"chat_n_{i}"] = name or " "
        n = len(twitch_chat.chatters)
        out["chat_head"] = (f"CHAT  ·  {n} {'Person zählt' if n == 1 else 'Leute zählen'} mit runter"
                            if on and twitch_chat.connected else " ")
        txt, until = twitch_chat.banner
        out["chat_banner"] = txt if on and txt and time.time() < until else " "
        if self.n % 40 == 0:
            twitch_chat.save()
        if streamer and streamer.session_started:
            twitch_chat.reset_session(int(streamer.session_started))
        return out

    def _runtime_files(self, cfg):
        txt = self._runtime(cfg)
        rest = txt.replace("●  ", "", 1) if txt.strip() else " "
        pos = cfg.get("runtime_pos", "tr")
        out = {}
        for p in RUNTIME_POS:
            out[f"runtime_{p}"] = txt if p == pos else " "
            out[f"runtime_rest_{p}"] = rest if p == pos else " "
        return out

    @staticmethod
    def _runtime(cfg):
        if not cfg.get("show_runtime", True) or not streamer or not streamer.session_started:
            return " "
        if cfg.get("runtime_mode") == "detail":
            parts, total = [], 0
            for k, d in DESTINATIONS.items():
                if cfg.get(d["enabled"]) and cfg.get(d["key"]):
                    sec = stats.d(k)["session"]
                    total += sec
                    parts.append(f"{d['name']} {fmt_dur(sec, long=True)}")
            if len(parts) > 1:
                parts.append(f"Gesamt {fmt_dur(total, long=True)}")
            return "●  LIVE  " + "  ·  ".join(parts) if parts else " "
        return f"●  {cfg.get('runtime_label', 'LIVE seit')} {fmt_dur(time.time() - streamer.session_started, long=True)}"

    def __init__(self):
        super().__init__(daemon=True)
        self.n = 0

    def run(self):
        while True:
            try:
                self._yt_ensure_live()
            except Exception as e:  # noqa
                print("YouTube-Livestream:", e, flush=True)
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
            "title": cfg["cd_title"], "done": show(cfg["cd_done"]), "tile": " " if os.path.exists(COVER_ORIG) and cfg["cd_theme"] in ("ps5", "release", "konsole") else cfg["cd_tile"],
            "release": "Release: " + release, "release_short": release,
            "badge": "●  " + cfg["cd_badge"], "badge_dot": "●",
            "head_full": cfg["cd_head1"] + cfg["cd_hl"], "head1": cfg["cd_head1"] or " ",
            "head2": cfg["cd_head2"], "sub": cfg["cd_sub"].replace("\\n", "\n"),
            "game": cfg["cd_game"], "boxlabel": hide(cfg["cd_boxlabel"]),
            # Konsolen-Design
            "k_games": "Spiele", "k_media": "Medien", "k_user": "xImJxstinn",
            "clock": datetime.now(ZoneInfo(TZ_NAME)).strftime("%H:%M"),
            **self._runtime_files(cfg),
            **self._chat_files(cfg),
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
    """Schiebt 1x pro Sekunde das aktuelle Hintergrundbild als Rohbild (RGBA) in ffmpeg.
    Rohbilder haben eine feste Größe -> ffmpeg muss nichts analysieren und startet sofort."""

    def __init__(self):
        super().__init__(daemon=True)
        self._cache = (None, None, b"")

    def _image(self):
        w, h, _, _ = QUALITIES.get(get_cfg()["quality"], QUALITIES["720p30"])
        size = w * h * 4
        try:
            mt = os.path.getmtime(BG_RAW)
        except OSError:
            mt = None
        if self._cache[:2] != (size, mt):
            data = b""
            if mt is not None:
                with open(BG_RAW, "rb") as fh:
                    data = fh.read()
            if len(data) != size:            # kein Bild (oder andere Qualität) -> durchsichtig
                data = bytes(size)
            self._cache = (size, mt, data)
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
        self.bad = {}           # Song -> Zeitpunkt, bis wann er übersprungen wird (kaputte Datei)

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
                    now = time.time()
                    songs = [m for m in list_music() if self.bad.get(m, 0) < now]
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
                    if pos + len(data) < self.RATE * 2 and self.current:   # < 2 s Ton = kaputt
                        self.bad[self.current] = time.time() + 300
                        log_line(f"Musik: '{os.path.basename(self.current)}' lässt sich nicht abspielen – übersprungen")
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


def tile_box(theme, w, h):
    """Position + Größe der Cover-Kachel (x, y, Kante) je Design."""
    if theme == "ps5":
        return int(w * 0.045), int(h * 0.69), int(h * 0.215)
    if theme == "konsole":
        return int(w * 0.05), int(h * 0.15), int(h * 0.22)
    return int(w * 0.045), int(h * 0.715), int(h * 0.2)


def save_upload_image(src, kind):
    """Speichert ein hochgeladenes Bild normiert (Hintergrund 1920x1080, Cover 600x600)."""
    vf = ("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080" if kind == "bg"
          else "scale=600:600:force_original_aspect_ratio=increase,crop=600:600")
    dst = BG_ORIG if kind == "bg" else COVER_ORIG
    tmp = os.path.join(RUN_DIR, kind + "_orig_new.png")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf + ",format=rgb24",
                        "-frames:v", "1", "-update", "1", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Bild konnte nicht gelesen werden")
    os.replace(tmp, dst)


def rebuild_layers(cfg=None):
    """Baut aus Hintergrund + Cover die Bild-Ebene für das aktuelle Design. Wird live getauscht."""
    cfg = cfg or get_cfg()
    w, h, _, _ = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    theme = cfg.get("cd_theme", "ps5")
    has_bg, has_cover = os.path.exists(BG_ORIG), os.path.exists(COVER_ORIG)
    needs_strip = theme in ("ps5", "release", "konsole")   # Kachel (und ggf. Leiste) liegt in der Bild-Ebene
    if not has_bg and not has_cover and not needs_strip:
        for fp in (BG_RAW, BG_LAYER):
            if os.path.exists(fp):
                os.remove(fp)
        return
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=black@0.0:s={w}x{h},format=rgba"]
    graph, last, n = [], "[0:v]", 1
    if has_bg:
        cmd += ["-i", BG_ORIG]
        dark = "" if theme == "ps5" else ",drawbox=x=0:y=0:w=iw:h=ih:color=0x05060b@0.55:t=fill"
        graph.append(f"[{n}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}{dark},format=rgba[bg]")
        graph.append(f"{last}[bg]overlay=0:0[l{n}]")
        last, n = f"[l{n}]", n + 1
    if needs_strip:
        x, y, size = tile_box(theme, w, h)
        tile_col = "0x0a2a7a@0.85" if theme == "konsole" else "0x1c2244"
        graph.append(f"color=c={tile_col}:s={size}x{size},format=rgba[tf]")
        if theme != "konsole":
            top, scol = (int(h * 0.645), "0x0c1120@0.90") if theme == "ps5" else (int(h * 0.655), "0x05060b@0.93")
            graph.append(f"color=c={scol}:s={w}x{h - top},format=rgba[st]")
            graph.append(f"{last}[st]overlay=0:{top}:shortest=1[s0]")
            last = "[s0]"
        graph.append(f"{last}[tf]overlay={x}:{y}:shortest=1[s1]")
        last = "[s1]"
    if has_cover and needs_strip:
        x, y, size = tile_box(theme, w, h)
        cmd += ["-i", COVER_ORIG]
        graph.append(f"[{n}:v]scale={size}:{size},format=rgba[cv]")
        graph.append(f"{last}[cv]overlay={x}:{y}[l{n}]")
        last, n = f"[l{n}]", n + 1
    graph.append(f"{last}format=rgba[out]")
    tmp_png = os.path.join(RUN_DIR, "layer_new.png")
    tmp_raw = os.path.join(RUN_DIR, "layer_new.rgba")
    r = subprocess.run(cmd + ["-filter_complex", ";".join(graph), "-map", "[out]", "-frames:v", "1",
                              "-update", "1", tmp_png], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Bild-Ebene konnte nicht gebaut werden: " + r.stderr.strip()[-200:])
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp_png, "-f", "rawvideo", "-pix_fmt", "rgba",
                    "-frames:v", "1", tmp_raw], check=True)
    os.replace(tmp_png, BG_LAYER)
    os.replace(tmp_raw, BG_RAW)


def convert_bg(src, cfg):
    save_upload_image(src, "bg")
    rebuild_layers(cfg)


def theme_filters(cfg, w, h):
    q = lambda p: "'" + p.replace("'", "") + "'"   # noqa
    B, R, L, M = (q(font(k)) for k in ("bold", "regular", "light", "mono"))
    col = color_hex(cfg)

    def T(name, fnt, size, color, x, y, extra=""):
        return (f"drawtext=fontfile={fnt}:textfile={q(os.path.join(RUN_DIR, name + '.txt'))}:reload=1:"
                f"expansion=none:fontsize={size}:fontcolor={color}:x={x}:y={y}{extra}")

    theme = cfg.get("cd_theme", "release")
    f = []
    ry = "h*0.115" if theme == "konsole" else "h*0.04"
    pad = int(h * 0.012)
    # Zeit-Einblendung an allen Positionen vorbereitet; nur die gewählte bekommt Text.
    # Ganze Zeile rot (mit Kasten), dann alles außer dem Punkt rechtsbündig in Weiß darüber.
    by = "h*0.955-h*0.026*1.25"   # feste Höhe, damit Rot und Weiß deckungsgleich liegen
    pos_xy = {"tl": ("w*0.035", ry), "tc": ("(w-text_w)/2", ry), "tr": ("w*0.965-text_w", ry),
              "bl": ("w*0.035", by), "bc": ("(w-text_w)/2", by), "br": ("w*0.965-text_w", by)}
    pre = "h*0.026*1.56884"   # Breite von "●  " in DejaVu Sans Bold (ausgemessen)
    rest_x = {"l": f"w*0.035+{pre}", "c": f"(w-text_w)/2+{pre}/2", "r": "w*0.965-text_w"}
    runtime = []
    for p, (x, y) in pos_xy.items():
        runtime.append(T(f"runtime_{p}", B, "h*0.026", "0xff3b3b", x, y,
                         f":box=1:boxcolor=black@0.45:boxborderw={pad}:alpha='if(lt(text_w,h*0.05),0,1)'"))
        runtime.append(T(f"runtime_rest_{p}", B, "h*0.026", "white", rest_x[p[1]], y))
    if theme == "standard":
        f.append(f"drawbox=x=0:y=ih*0.30:w=iw:h=ih*0.40:color={col}@0.08:t=fill")
        f.append(T("title", B, "h*0.085", "white", "(w-text_w)/2", "h*0.13"))
        for k, lab, cx in (("nd", "l_tage", 0.2), ("nh", "l_stunden", 0.4), ("nm", "l_minuten", 0.6), ("ns", "l_sekunden", 0.8)):
            f.append(T(k, B, "h*0.17", col, f"w*{cx}-text_w/2", "h*0.36", ":shadowcolor=black@0.6:shadowx=4:shadowy=4"))
            f.append(T(lab, B, "h*0.032", "white@0.8", f"w*{cx}-text_w/2", "h*0.56"))
        f.append(T("release", B, "h*0.04", "white@0.85", "(w-text_w)/2", "h*0.76"))
        f.append(T("done", B, "h*0.13", col, "(w-text_w)/2", "h*0.40", ":shadowcolor=black@0.6:shadowx=5:shadowy=5"))

    elif theme == "konsole":
        f.append(T("k_games", B, "h*0.034", "white", "w*0.05", "h*0.06"))
        f.append(T("k_media", R, "h*0.034", "white@0.55", "w*0.05+h*0.17", "h*0.06"))
        f.append(T("clock", B, "h*0.034", "white", "w*0.95-text_w", "h*0.06"))
        f.append(T("k_user", R, "h*0.03", "white@0.75", "w*0.95-text_w-h*0.17", "h*0.065"))
        f.append("drawbox=x=iw*0.05-4:y=ih*0.15-4:w=ih*0.22+8:h=ih*0.22+8:color=white:t=4")
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

    elif theme == "ps5":   # PS5-Startseite: Bild oben, Leiste mit Cover + Countdown unten
        x, y, size = tile_box("ps5", w, h)
        f.append(T("tile", B, "h*0.04", "white", f"{x}+({size}-text_w)/2", f"{y}+({size}-text_h)/2"))
        f.append(f"drawbox=x={x}:y={y}:w={size}:h={size}:color=white@0.35:t=2")
        f.append(T("game", R, "h*0.042", "white", f"{x}+{size}+h*0.07", "h*0.765"))
        f.append(T("boxlabel", R, "h*0.036", "white", "w*0.515", "h*0.695"))
        f.append(T("spin", R, "h*0.07", "white@0.9", "w*0.55-text_w/2", "h*0.795"))
        for k, lab, cx in (("nd", "r_tage", 0.625), ("nh", "r_std", 0.715), ("nm", "r_min", 0.825), ("ns", "r_sek", 0.935)):
            f.append(T(lab, R, "h*0.029", "white@0.85", f"w*{cx}-text_w/2", "h*0.748"))
            f.append(T(k, L, "h*0.085", "white", f"w*{cx}-text_w/2", "h*0.79"))
        for cx in (0.77, 0.88):
            f.append(T("colon", R, "h*0.065", "white", f"w*{cx}-text_w/2", "h*0.797"))
        f.append(T("done", B, "h*0.07", "white", "w*0.515+(w*0.44-text_w)/2", "h*0.78"))

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
    # Twitch-Chat
    cx, cy, cfs, _ = CHAT_LAYOUT.get(theme, CHAT_LAYOUT["ps5"])
    lh = cfs * 1.75
    cpad = int(h * cfs * 0.35)
    hide = "alpha='if(lt(text_w,h*0.05),0,1)'"
    chat = [T("chat_head", B, f"h*{cfs:.3f}", "white", cx, f"h*{cy:.3f}",
              f":box=1:boxcolor={col}@0.85:boxborderw={cpad}:{hide}")]
    for i in range(CHAT_LINES):
        y = f"h*{cy + lh * (i + 1.25):.4f}"
        chat.append(T(f"chat_{i}", R, f"h*{cfs:.3f}", "white", cx, y,
                      f":box=1:boxcolor=black@0.5:boxborderw={cpad}:{hide}"))
        chat.append(T(f"chat_n_{i}", R, f"h*{cfs:.3f}", col, cx, y,
                      f":borderw=1:bordercolor={col}"))                      # Name farbig + leicht fett, deckungsgleich
    bx_, by_ = CHAT_BANNER.get(theme) or (cx, f"h*{cy + lh * (CHAT_LINES + 1.6):.4f}")
    chat.append(T("chat_banner", B, f"h*{max(cfs, 0.024) * 1.35:.3f}", "white", bx_, by_,
                  f":box=1:boxcolor={col}@0.92:boxborderw={int(h * cfs * 0.6)}:alpha='if(lt(text_w,h*0.1),0,1)'"))
    return f + runtime + chat


def build_countdown_cmd(cfg, output, preview=False, fmt="flv"):
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
        cmd += ["-thread_queue_size", "8", "-f", "rawvideo", "-pix_fmt", "rgba", "-video_size", f"{w}x{h}",
                "-framerate", "1", "-i", IMAGE_FIFO]
    vig = ",vignette=PI/5" if theme == "konsole" else ""     # Vignette nur auf den Hintergrund, nicht aufs Cover
    graph = (f"[0:v]scale={w}:{h},setsar=1,fps={fps},format=yuv420p{vig}[base];"
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
            "-c:v", "libx264", "-preset", live_preset(cfg), "-pix_fmt", "yuv420p",
            "-b:v", vb, "-maxrate", vb, "-bufsize", vb, "-x264-params", "nal-hrd=cbr:force-cfr=1:repeat-headers=1",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-progress", "pipe:2", "-stats_period", "2",
            *(["-f", "flv", "-flvflags", "no_duration_filesize"] if fmt == "flv" else
              ["-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0"]), output]
    return cmd



# ------------------------------------------------------------- Statistik ----
STATS_PATH = os.path.join(BASE, "stats.json")


def fmt_dur(sec, long=False):
    sec = int(max(0, sec))
    d, h, m, s_ = sec // 86400, sec // 3600 % 24, sec // 60 % 60, sec % 60
    if long:
        return (f"{d} Tag{'e' if d != 1 else ''} " if d else "") + f"{h:02d}:{m:02d}:{s_:02d}"
    return (f"{d} T " if d else "") + f"{h} h {m:02d} min"


class StatsTracker(threading.Thread):
    """Zählt jede Sekunde, die eine Plattform wirklich live ist – dauerhaft gespeichert."""

    def __init__(self):
        super().__init__(daemon=True)
        self.data = load_json(STATS_PATH, {})
        self.data.setdefault("dest", {})
        self._was_live = {}

    def d(self, k):
        return self.data["dest"].setdefault(k, {"total": 0, "session": 0, "drops": 0, "session_drops": 0,
                                                "longest": 0, "current": 0})

    def new_session(self):
        for k in DESTINATIONS:
            x = self.d(k)
            x.update(session=0, session_drops=0, current=0)
        self.save()

    def reset_all(self):
        self.data = {"dest": {}}
        self.new_session()

    def save(self):
        save_json(STATS_PATH, self.data)

    def run(self):
        n = 0
        while True:
            time.sleep(1)
            try:
                st = sender_status()
                want = streamer is not None and streamer.want
                # Zähler darf nie weniger zeigen als die echte Verbindung (z. B. Twitch-Uptime)
                if want and st.get("ok") and streamer.session_started:
                    up = max((d.get("uptime", 0) for d in st["dest"].values()), default=0)
                    if up > time.time() - streamer.session_started + 30:
                        streamer.session_started = time.time() - up
                        streamer._save_state()
                        for k, dd in st["dest"].items():
                            x = self.d(k)
                            u = int(dd.get("uptime", 0))
                            x["session"] = max(x["session"], u)
                            x["current"] = max(x["current"], u)
                            x["longest"] = max(x["longest"], x["current"])
                            x["total"] = max(x["total"], x["session"])
                for k, dd in st["dest"].items():
                    x = self.d(k)
                    live = bool(dd.get("live")) and want
                    if live:
                        x["total"] += 1
                        x["session"] += 1
                        x["current"] += 1
                        x["longest"] = max(x["longest"], x["current"])
                    elif self._was_live.get(k) and want:
                        x["drops"] += 1                    # war live, ist es plötzlich nicht mehr
                        x["session_drops"] += 1
                        x["current"] = 0
                    self._was_live[k] = live
                n += 1
                if n % 15 == 0:
                    self.save()
            except Exception as e:  # noqa
                print("Statistik-Fehler:", e, flush=True)


stats = StatsTracker()
streamer = None


# ------------------------------------------------ "Neu verbinden"-Animation ----
# Wird EINMAL vom Panel als kurze, nahtlose 3-Sekunden-Schleife gerendert.
# Der Sender spielt sie nur ab (kaum CPU) – Design-Änderungen brauchen so kein Sender-Update.
SLATE_DESIGN = 3


def slate_path(cfg):
    w, h, _, _ = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    return os.path.join(RUN_DIR, f"slate_{w}x{h}.mp4")


def build_slate_loop(cfg=None, force=False):
    cfg = cfg or get_cfg()
    w, h, fps, vb = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    out = slate_path(cfg)
    meta_p = out + ".json"
    if not force and os.path.exists(out) and load_json(meta_p, {}).get("design") == SLATE_DESIGN:
        return out
    q = lambda p: "'" + p.replace("'", "") + "'"   # noqa
    B, R = q(font("bold")), q(font("regular"))
    for name, text in (("sl_title", "Neu verbinden"), ("sl_t1", "Neu verbinden."), ("sl_t2", "Neu verbinden.."),
                       ("sl_t3", "Neu verbinden..."),
                       ("sl_sub", "Der Stream geht gleich weiter"), ("sl_brand", "xImJxstinn · 24/7")):
        atomic_write(os.path.join(RUN_DIR, name + ".txt"), text)
    tf = lambda n: q(os.path.join(RUN_DIR, n + ".txt"))   # noqa
    ring = int(h * 0.30) // 2 * 2          # Größe des Lade-Rings
    rc = ring / 2
    r_out, r_in = rc * 0.92, rc * 0.80
    P = 1.5                                 # Umdrehung / Puls in Sekunden (3 s Schleife = 2 Perioden)
    g = f"exp(-((X-W/2)^2+(Y-H*0.40)^2)/(2*(H*0.30)^2))"
    bg = (f"color=c=black:s={w}x{h}:r={fps}:d=3,format=rgb24,"
          f"geq=r='clip(10+38*{g},0,255)':g='clip(8+14*{g},0,255)':b='clip(24+80*{g},0,255)'")
    # Leuchten hinter dem Ring: pulsiert
    glow = (f"color=c=black@0:s={ring * 2}x{ring * 2}:r={fps}:d=3,format=rgba,"
            f"geq=r=155:g=92:b=246:a='clip(150*exp(-(hypot(X-W/2,Y-H/2)/(W*0.22))^2)*(0.55+0.45*sin(2*PI*T/{P})),0,255)'")
    # Lade-Ring: Bogen mit auslaufendem Schweif, dreht sich
    ang = f"mod(atan2(Y-{rc},X-{rc})+PI-2*PI*T/{P}+8*PI,2*PI)/(2*PI)"
    edge = f"clip(min(hypot(X-{rc},Y-{rc})-{r_in},{r_out}-hypot(X-{rc},Y-{rc}))/1.5,0,1)"
    spin = (f"color=c=black@0:s={ring}x{ring}:r={fps}:d=3,format=rgba,"
            f"geq=r='clip(155+100*{ang},0,255)':g='clip(92+150*{ang},0,255)':b=255:"
            f"a='clip(255*{edge}*pow({ang},1.6),0,255)'")
    track = (f"color=c=black@0:s={ring}x{ring}:r={fps}:d=3,format=rgba,"
             f"geq=r=255:g=255:b=255:a='clip(28*{edge},0,255)'")
    fs = h * 0.075
    x0 = f"(w-{fs * 9.414625:.2f})/2"     # zentriert für die volle Zeile mit drei Punkten
    vf = (f"[0:v][1:v]overlay=(W-w)/2:H*0.40-h/2[a];[a][3:v]overlay=(W-w)/2:H*0.40-h/2[b];"
          f"[b][2:v]overlay=(W-w)/2:H*0.40-h/2,format=yuv420p,"
          f"null")
    # Punkte erscheinen nacheinander: vier Varianten der Zeile, jeweils nur in ihrem Zeitfenster sichtbar
    for i, name in enumerate(("sl_title", "sl_t1", "sl_t2", "sl_t3")):
        lo, hi = 0.3 * i + (0.0 if i else -1), (0.3 * (i + 1) if i < 3 else 99)
        vf += (f",drawtext=fontfile={B}:textfile={tf(name)}:expansion=none:fontsize={fs:.2f}:fontcolor=white:"
               f"x={x0}:y=h*0.62:enable='between(mod(t,{P}),{max(lo, 0):.2f},{hi - 0.001:.3f})'")
    vf += (f",drawtext=fontfile={R}:textfile={tf('sl_sub')}:expansion=none:fontsize=h*0.034:fontcolor=white@0.62:"
           f"x=(w-text_w)/2:y=h*0.73"
           f",drawbox=x=iw*0.3:y=ih*0.83:w=iw*0.4:h=ih*0.004:color=white@0.12:t=fill"
           f",drawbox=x='iw*0.3+(0.5-0.5*cos(2*PI*t/3))*iw*0.32':y=ih*0.83:w=iw*0.08:h=ih*0.004:color=0xb98bff@0.95:t=fill"
           f",drawtext=fontfile={B}:textfile={tf('sl_brand')}:expansion=none:fontsize=h*0.024:fontcolor=white@0.35:"
           f"x=(w-text_w)/2:y=h*0.92[v]")
    tmp = out + ".new.mp4"
    kbit = int(vb[:-1])
    cmd = ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y", "-threads", "1", "-filter_threads", "1",
           "-filter_complex_threads", "1",
           "-f", "lavfi", "-i", bg, "-f", "lavfi", "-i", glow, "-f", "lavfi", "-i", spin, "-f", "lavfi", "-i", track,
           "-f", "lavfi", "-t", "3", "-i", "anullsrc=r=44100:cl=stereo",
           "-filter_complex", vf.replace("[b][2:v]", "[b][2:v]"), "-map", "[v]", "-map", "4:a", "-t", "3",
           "-c:v", "libx264", "-preset", "medium", "-threads", "1", "-pix_fmt", "yuv420p",
           "-b:v", vb, "-maxrate", vb, "-bufsize", vb, "-x264-params", "nal-hrd=cbr:force-cfr=1:repeat-headers=1",
           "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Neu-verbinden-Animation: " + r.stderr.strip()[-300:])
    os.replace(tmp, out)
    save_json(meta_p, {"design": SLATE_DESIGN})
    return out



# --------------------------------------------------------------- Updates ----
REPO_API = "https://api.github.com/repos/ximjxstinn/stream-marathon/commits/stream24"
RAW_BASE = "https://raw.githubusercontent.com/ximjxstinn/stream-marathon/"
VERSION_PATH = os.path.join(BASE, "version.json")


def latest_sha():
    """Neueste Commit-ID über das Git-Protokoll (wie 'git ls-remote') – ohne API-Abfragelimit."""
    import urllib.request
    url = "https://github.com/ximjxstinn/stream-marathon.git/info/refs?service=git-upload-pack"
    req = urllib.request.Request(url, headers={"User-Agent": "git/2.40 stream24"})
    data = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    m = re.search(r"([0-9a-f]{40}) refs/heads/stream24\b", data)
    if not m:
        raise RuntimeError("Branch stream24 nicht gefunden")
    return m.group(1)


def do_update(sha=None):
    """Lädt server.py + sender.py in genau der neuesten Version. Gibt (ok, sender_geändert | Fehler) zurück."""
    import urllib.request
    try:
        sha = sha or latest_sha()
        base_url = RAW_BASE + sha + "/"
        code = urllib.request.urlopen(base_url + "server.py", timeout=30).read()
        compile(code, "server.py", "exec")
        scode = urllib.request.urlopen(base_url + "sender.py", timeout=30).read()
        compile(scode, "sender.py", "exec")
    except Exception as e:  # noqa
        return False, f"Update fehlgeschlagen: {e}"
    here = os.path.dirname(os.path.abspath(__file__))
    sp = os.path.join(here, "sender.py")
    old = open(sp, "rb").read() if os.path.exists(sp) else b""
    sender_changed = old != scode
    for path, data in ((os.path.join(here, "server.py"), code), (sp, scode)):
        with open(path + ".new", "wb") as fh:
            fh.write(data)
        os.replace(path + ".new", path)
    v = load_json(VERSION_PATH, {})
    v.update(sha=sha, installed=time.time(), sender_pending=bool(v.get("sender_pending") or sender_changed))
    save_json(VERSION_PATH, v)
    log_line(f"Update installiert ({sha[:7]})" + (" – Sender-Update bereit" if sender_changed else ""))
    return True, sender_changed


def restart_panel():
    try:
        stats.save()
        streamer._kill()
    finally:
        os._exit(0)                                   # systemd startet das Panel neu (Stream bleibt online)


class AutoUpdater(threading.Thread):
    """Prüft alle 20 Sekunden, ob es eine neue Version gibt, und installiert sie automatisch.
    Das Panel startet dann neu – der Stream bleibt online (kurz "Neu verbinden …").
    Ein Sender-Update wird nur vorgemerkt, damit der Stream nicht abbricht."""

    def __init__(self):
        super().__init__(daemon=True)
        self.last_check = 0
        self.last_result = ""

    def run(self):
        time.sleep(30)                                # nach dem Start kurz in Ruhe laufen lassen
        while True:
            wait = 20
            try:
                self.check()
            except Exception as e:  # noqa
                self.last_result = f"Prüfen fehlgeschlagen: {e}"
                wait = 120                            # z. B. GitHub nicht erreichbar -> etwas länger warten
            time.sleep(wait)

    def check(self, force=False):
        self.last_check = time.time()
        if not force and not get_cfg().get("auto_update", True):
            self.last_result = "Auto-Update ist aus"
            return
        sha = latest_sha()
        if sha == load_json(VERSION_PATH, {}).get("sha"):
            self.last_result = "Aktuell"
            return
        if converter.busy() or any(f.startswith(("chunk_", "upload_")) for f in os.listdir(RUN_DIR)):
            self.last_result = "Neue Version da – wartet, bis Uploads/Umwandlungen fertig sind"
            return
        ok, info = do_update(sha)
        self.last_result = "Installiert" if ok else info
        if ok:
            time.sleep(1)
            restart_panel()


auto_updater = AutoUpdater()


# ------------------------------------------------------------ Twitch-Chat ----
# Liest den Chat anonym mit (kein Login nötig) und zeigt ihn im Stream.
import socket
import ssl
from collections import deque

CHATTERS_PATH = os.path.join(BASE, "chatters.json")
CHAT_LINES = 5
URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b\S+\.(?:com|de|net|org|tv|gg|io|ly|me|xyz|ru)\b\S*)", re.I)


def clean_text(t):
    t = "".join(ch for ch in t if ord(ch) < 0x2E80 and (ch.isprintable() or ch == " "))   # Emojis kann die Schrift nicht
    return re.sub(r"\s+", " ", t).strip()


class TwitchChat(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.msgs = deque(maxlen=50)          # dict(id, user, name, color, text, ts)
        self.connected = False
        self.channel = ""
        self.sock = None
        self.banner = ("", 0)                 # (Text, bis wann)
        self.chatters = set(load_json(CHATTERS_PATH, {}).get("names", []))
        self.session = load_json(CHATTERS_PATH, {}).get("session")
        self._dirty = False

    def reset_session(self, session_id):
        if session_id != self.session:
            self.session = session_id
            self.chatters = set()
            self._dirty = True

    def run(self):
        while True:
            cfg = get_cfg()
            ch = re.sub(r"[^a-z0-9_]", "", (cfg.get("chat_channel") or "").lower())
            if not cfg.get("chat_enabled", True) or not ch:
                self.connected = False
                time.sleep(3)
                continue
            try:
                self._session(ch)
            except Exception as e:  # noqa
                log_line(f"Twitch-Chat: Verbindung verloren ({e}) – verbinde neu")
            self.connected = False
            time.sleep(5)

    def _session(self, ch):
        test = os.environ.get("STREAM24_IRC_TEST")           # nur für Tests: "host:port" ohne TLS
        if test:
            host, port = test.split(":")
            self.sock = socket.create_connection((host, int(port)), timeout=360)
        else:
            raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=360)
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
        nick = f"justinfan{random.randint(10000, 99999)}"
        for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK {nick}", f"JOIN #{ch}"):
            self.sock.sendall((line + "\r\n").encode())
        self.channel, self.connected = ch, True
        log_line(f"Twitch-Chat: verbunden mit #{ch}")
        buf = b""
        while True:
            cfg = get_cfg()
            if re.sub(r"[^a-z0-9_]", "", (cfg.get("chat_channel") or "").lower()) != ch or not cfg.get("chat_enabled", True):
                self.sock.close()
                return
            data = self.sock.recv(65536)
            if not data:
                raise RuntimeError("Server hat getrennt")
            buf += data
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                self._on_line(line.decode("utf-8", "replace"))

    def _on_line(self, line):
        if line.startswith("PING"):
            self.sock.sendall(b"PONG :tmi.twitch.tv\r\n")
            return
        tags = {}
        if line.startswith("@"):
            tagstr, line = line[1:].split(" ", 1)
            for kv in tagstr.split(";"):
                k, _, v = kv.partition("=")
                tags[k] = v.replace("\\s", " ")
        parts = line.split(" ", 3)
        if len(parts) < 3:
            return
        cmd = parts[1]
        if cmd == "PRIVMSG" and len(parts) == 4:
            user = parts[0][1:].split("!", 1)[0]
            text = parts[3][1:] if parts[3].startswith(":") else parts[3]
            self._message(user, tags, text)
        elif cmd == "CLEARMSG":                         # Nachricht gelöscht
            mid = tags.get("target-msg-id")
            self.msgs = deque((m for m in self.msgs if m["id"] != mid), maxlen=50)
        elif cmd == "CLEARCHAT":                        # Bann/Timeout oder Chat geleert
            target = parts[3][1:] if len(parts) == 4 else None
            if target:
                self.msgs = deque((m for m in self.msgs if m["user"] != target.lower()), maxlen=50)
            else:
                self.msgs.clear()

    def _message(self, user, tags, text):
        cfg = get_cfg()
        name = clean_text(tags.get("display-name") or user) or user
        if text.startswith("\x01ACTION "):
            text = text[8:].rstrip("\x01")
        low = text.lower().strip()
        # Befehle
        if low.startswith("!discord") and cfg.get("discord_link"):
            self.banner = (f"Discord:  {cfg['discord_link']}", time.time() + 12)
            return
        if low.startswith("!tiktok") and cfg.get("tiktok_link"):
            self.banner = (f"TikTok:  {cfg['tiktok_link']}", time.time() + 12)
            return
        if user.lower() in [u.strip().lower() for u in (cfg.get("chat_ignore") or "").split(",") if u.strip()]:
            return                                      # z. B. Bots wie Nightbot ausblenden
        if low.startswith("!"):
            return
        bad = [w.strip().lower() for w in (cfg.get("chat_blocklist") or "").split(",") if w.strip()]
        if any(w in low for w in bad):
            return
        text = URL_RE.sub("[Link]", clean_text(text))
        if not text:
            return
        self.msgs.append({"id": tags.get("id", ""), "user": user.lower(), "name": name,
                          "color": tags.get("color") or "", "text": text, "ts": time.time()})
        if user.lower() not in self.chatters:
            self.chatters.add(user.lower())
            self._dirty = True

    def save(self):
        if self._dirty:
            save_json(CHATTERS_PATH, {"session": self.session, "names": sorted(self.chatters)})
            self._dirty = False

    def lines(self, max_chars):
        """Die neuesten Nachrichten (unten = neueste), gekürzt auf die Breite."""
        cfg = get_cfg()
        keep = float(cfg.get("chat_keep_min", 10)) * 60
        recent = [m for m in self.msgs if time.time() - m["ts"] < keep][-CHAT_LINES:]
        out = []
        for m in recent:
            full = f"{m['name']}: {m['text']}"
            if len(full) > max_chars:
                full = full[:max_chars - 1].rstrip() + "…"
            out.append((full, (m["name"] + ":")[:max_chars]))
        return out


twitch_chat = TwitchChat()

# Position des !discord/!tiktok-Banners je Design (x, y) – None = direkt unter dem Chat
CHAT_BANNER = {"release": ("w*0.965-text_w", "h*0.555"), "konsole": ("w*0.965-text_w", "h*0.555")}
# Position des Chats je Design: x, y (oben), Schriftgröße, max. Zeichen pro Zeile
CHAT_LAYOUT = {
    "ps5": ("w*0.035", 0.11, 0.026, 40),
    "release": ("w*0.62", 0.13, 0.025, 38),
    "konsole": ("w*0.765", 0.17, 0.021, 26),
    "standard": ("w*0.03", 0.06, 0.024, 40),
}


# ----------------------------------------------------- Spiele-Warteschlange ----
# Nach dem Release (+ einige Stunden "IST DA!") wechselt der Countdown automatisch zum nächsten Spiel.
GAMES_CACHE = os.path.join(BASE, "games.json")
GAMES_DIR = os.path.join(CD_DIR, "games")
os.makedirs(GAMES_DIR, exist_ok=True)
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]


def all_games():
    """Spiele aus der zentralen Liste (GitHub) + eigene Spiele aus dem Panel, nach Datum sortiert."""
    games = {}
    for g in load_json(GAMES_CACHE, []) + (get_cfg().get("games_custom") or []):
        try:
            target_epoch({"cd_target": g["target"]})
            games[g["id"]] = g
        except Exception:  # noqa
            continue
    return sorted(games.values(), key=lambda g: g["target"])


def game_texts(g):
    """Alle Countdown-Texte für ein Spiel."""
    _, dt = target_epoch({"cd_target": g["target"]})
    short = g.get("short") or g["name"]
    hl = f"{dt.hour} Uhr" if dt.minute == 0 else dt.strftime("%H:%M Uhr")
    return {
        "cd_target": g["target"], "cd_title": g["name"], "cd_game": g["name"],
        "cd_tile": (g.get("tile") or short)[:20], "cd_done": f"{short} IST DA!"[:60],
        "cd_head1": f"{short} ab ", "cd_hl": hl, "cd_head2": "spielbar",
        "cd_sub": f"Freischaltung am {dt.day}. {MONTHS[dt.month - 1]}: Uhrzeit für Deutschland\\nund weltweit",
        "cd_badge": "RELEASE-UHRZEIT · COUNTDOWN",
    }


def switch_game(g, reason=""):
    """Wechselt den Countdown live auf ein anderes Spiel – mit eigenem Cover/Hintergrund pro Spiel."""
    cfg = get_cfg()
    old = cfg.get("game_current") or "gta6"
    # Bilder des alten Spiels wegsichern, die des neuen holen
    for src, name in ((COVER_ORIG, "cover"), (BG_ORIG, "bg")):
        keep = os.path.join(GAMES_DIR, f"{old}_{name}.png")
        if os.path.exists(src):
            os.replace(src, keep)
        new = os.path.join(GAMES_DIR, f"{g['id']}_{name}.png")
        if os.path.exists(new):
            shutil.copy(new, src)
    set_cfg(game_current=g["id"], **game_texts(g))
    _safe(rebuild_layers)
    log_line(f"Countdown gewechselt: {g['name']} ({g['target'].replace('T', ' ')})" + (f" – {reason}" if reason else ""))


def next_game(after_epoch):
    for g in all_games():
        if target_epoch({"cd_target": g["target"]})[0] > after_epoch + 60:
            return g
    return None


class GameQueue(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.last_fetch = 0

    def fetch(self):
        """Zentrale Spieleliste von GitHub holen (alle 6 Stunden)."""
        import urllib.request
        try:
            data = urllib.request.urlopen(RAW_BASE + latest_sha() + "/games.json", timeout=20).read()
            games = json.loads(data)
            if isinstance(games, list):
                save_json(GAMES_CACHE, games)
            self.last_fetch = time.time()
        except Exception as e:  # noqa
            log_line(f"Spieleliste konnte nicht geladen werden: {e}")

    def run(self):
        time.sleep(20)
        while True:
            try:
                if time.time() - self.last_fetch > 6 * 3600:
                    self.fetch()
                self.check()
            except Exception as e:  # noqa
                print("Spiele-Warteschlange:", e, flush=True)
            time.sleep(60)

    def check(self):
        cfg = get_cfg()
        if not cfg.get("game_auto", True) or cfg.get("mode") != "countdown":
            return
        tgt, _ = target_epoch(cfg)
        hold = float(cfg.get("game_done_hours", 12)) * 3600
        if time.time() < tgt + hold:
            return
        g = next((x for x in all_games() if x["id"] == cfg.get("game_next")
                  and target_epoch({"cd_target": x["target"]})[0] > time.time()), None) or next_game(time.time())
        if g:
            set_cfg(game_next="")
            switch_game(g, f"automatisch, {cfg.get('cd_title')} ist seit {cfg.get('game_done_hours', 12)} h draußen")


game_queue = GameQueue()


# --------------------------------------------------------- Stream-Titel ----
# Setzt den Titel auf Twitch und YouTube automatisch, z. B. "(VOD) <Titel>" während VODs laufen.
# Anmeldung per Geräte-Code (wie beim Fernseher): kein Passwort im Panel, keine eigene Domain nötig.
TOKENS_PATH = os.path.join(BASE, "tokens.json")
TW_SCOPE = "channel:manage:broadcast"
YT_SCOPE = "https://www.googleapis.com/auth/youtube"


def _http(method, url, data=None, headers=None, form=False, timeout=20):
    import urllib.request
    import urllib.error
    from urllib.parse import urlencode
    body = None
    hd = dict(headers or {})
    if data is not None:
        if form:
            body = urlencode(data).encode()
            hd["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            hd["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hd, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa
            return e.code, {"message": raw.decode("utf-8", "replace")[:300]}


def title_for(cfg, vod_title=None):
    """Welcher Titel gerade passt."""
    mode = cfg.get("mode")
    if mode in ("twitch", "videos"):
        t = vod_title or cfg.get("title_fallback") or "Wiederholung"
        return cfg.get("title_vod", "(VOD) {title}").replace("{title}", t).strip()
    try:
        tgt, dt = target_epoch(cfg)
    except Exception:  # noqa
        tgt, dt = 0, None
    tpl = cfg.get("title_done") if time.time() >= tgt else cfg.get("title_countdown")
    out = (tpl or "{game}").replace("{game}", cfg.get("cd_title", "")).replace("{done}", cfg.get("cd_done", ""))
    return out.replace("{date}", dt.strftime("%d.%m.%Y") if dt else "").strip()


_vod_games = {}            # Video-ID -> {"game": Name, "chapters": [(Start s, Ende s, Name)], "t": Zeit}
_vod_games_lock = threading.Lock()
TW_GQL_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # öffentliche Client-ID der Twitch-Webseite (nutzt auch yt-dlp)


def _fetch_vod_game(vid):
    q = ('query{video(id:"%s"){game{displayName} moments(first:100,momentRequestType:VIDEO_CHAPTER_MARKERS)'
         '{edges{node{positionMilliseconds durationMilliseconds details{...on GameChangeMomentDetails'
         '{game{displayName}}}}}}}}' % vid)
    info = {"game": None, "chapters": [], "t": time.time()}
    try:
        code, j = _http("POST", "https://gql.twitch.tv/gql", {"query": q}, headers={"Client-Id": TW_GQL_ID})
        v = ((j or {}).get("data") or {}).get("video") or {} if code == 200 else {}
        if not v and code == 200:      # falls Kapitel nicht abgefragt werden dürfen: nur das Spiel
            code, j = _http("POST", "https://gql.twitch.tv/gql",
                            {"query": 'query{video(id:"%s"){game{displayName}}}' % vid}, headers={"Client-Id": TW_GQL_ID})
            v = ((j or {}).get("data") or {}).get("video") or {} if code == 200 else {}
        info["game"] = (v.get("game") or {}).get("displayName")
        for e in ((v.get("moments") or {}).get("edges") or []):
            n = e.get("node") or {}
            g = (((n.get("details") or {}).get("game")) or {}).get("displayName")
            if g:
                st = (n.get("positionMilliseconds") or 0) / 1000
                info["chapters"].append((st, st + (n.get("durationMilliseconds") or 0) / 1000, g))
    except Exception as e:  # noqa
        print("VOD-Kategorie:", e, flush=True)
    with _vod_games_lock:
        _vod_games[vid] = info
    if info["game"] or info["chapters"]:
        log_line(f"VOD {vid}: Kategorie {info['game'] or '?'}"
                 + (f" ({len(info['chapters'])} Kapitel)" if len(info["chapters"]) > 1 else ""))


def vod_game(url, pos):
    """Spiel des VODs an der aktuellen Stelle (Twitch-Kapitel) – None, solange unbekannt."""
    m = re.search(r"/videos/(\d+)", url or "")
    if not m:
        return None
    vid = m.group(1)
    with _vod_games_lock:
        info = _vod_games.get(vid)
        if info is None or (not info["game"] and time.time() - info["t"] > 600):
            _vod_games[vid] = {"game": None, "chapters": [], "t": time.time()}
            threading.Thread(target=_fetch_vod_game, args=(vid,), daemon=True).start()
            return None
    for st, en, g in info["chapters"]:
        if st <= pos < (en if en > st else st + 10 ** 9):
            return g
    return info["game"]


def category_for(cfg):
    """Twitch-Kategorie: beim Countdown das Spiel, bei VODs das Spiel aus dem VOD (sonst die eingestellte)."""
    if cfg.get("mode") in ("twitch", "videos"):
        if cfg.get("mode") == "twitch" and cfg.get("cat_vod_auto", True) and streamer:
            v = streamer.vod
            url = v.get("url") or v.get("last")
            pos = v["pos"] + (vod_progress_seconds() if v.get("url") and streamer.source_alive() else 0)
            g = vod_game(url, pos)
            if g:
                return g
        tpl = cfg.get("cat_vod")
    else:
        tpl = cfg.get("cat_countdown")
    return (tpl or "").replace("{game}", cfg.get("cd_game") or cfg.get("cd_title", "")).strip()


class TitleManager(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.tok = load_json(TOKENS_PATH, {})
        self.login = {}            # laufende Anmeldung je Plattform: user_code, url, ...
        self.state = {"twitch": {}, "youtube": {}}
        self.last = {"twitch": (None, 0), "youtube": (None, 0)}
        self.wake = threading.Event()

    def _save(self):
        save_json(TOKENS_PATH, self.tok)
        try:
            os.chmod(TOKENS_PATH, 0o600)
        except OSError:
            pass

    # --- Anmeldung -----------------------------------------------------------
    def start_login(self, plat):
        cfg = get_cfg()
        if plat == "twitch":
            cid = cfg.get("tw_client_id", "").strip()
            if not cid:
                return "Erst die Twitch Client-ID eintragen"
            code, j = _http("POST", "https://id.twitch.tv/oauth2/device",
                            {"client_id": cid, "scopes": TW_SCOPE}, form=True)
            if code != 200:
                return f"Twitch: {j.get('message', code)}"
            url = j["verification_uri"]
        else:
            cid = cfg.get("yt_client_id", "").strip()
            if not cid or not cfg.get("yt_client_secret", "").strip():
                return "Erst YouTube Client-ID und Client-Secret eintragen"
            code, j = _http("POST", "https://oauth2.googleapis.com/device/code",
                            {"client_id": cid, "scope": YT_SCOPE}, form=True)
            if code != 200:
                return f"YouTube: {j.get('error_description') or j.get('error') or code}"
            url = j.get("verification_url") or j.get("verification_uri")
        self.login[plat] = {"user_code": j["user_code"], "url": url, "device_code": j["device_code"],
                            "interval": int(j.get("interval", 5)), "until": time.time() + int(j.get("expires_in", 900))}
        threading.Thread(target=self._poll_login, args=(plat,), daemon=True).start()
        return None

    def _poll_login(self, plat):
        lg = self.login.get(plat)
        cfg = get_cfg()
        while lg and time.time() < lg["until"] and self.login.get(plat) is lg:
            time.sleep(lg["interval"])
            if plat == "twitch":
                code, j = _http("POST", "https://id.twitch.tv/oauth2/token", {
                    "client_id": cfg["tw_client_id"].strip(), "scopes": TW_SCOPE, "device_code": lg["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True)
            else:
                code, j = _http("POST", "https://oauth2.googleapis.com/token", {
                    "client_id": cfg["yt_client_id"].strip(), "client_secret": cfg["yt_client_secret"].strip(),
                    "device_code": lg["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                    form=True)
            if code == 200 and j.get("access_token"):
                self.tok[plat] = {"access": j["access_token"], "refresh": j.get("refresh_token"),
                                  "exp": time.time() + int(j.get("expires_in", 3600)) - 120}
                if plat == "twitch":
                    c2, u = _http("GET", "https://api.twitch.tv/helix/users", headers=self._tw_hd())
                    if c2 == 200 and u.get("data"):
                        self.tok[plat]["user_id"] = u["data"][0]["id"]
                        self.tok[plat]["name"] = u["data"][0]["display_name"]
                else:
                    c2, ch = _http("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                   headers=self._yt_hd())
                    if c2 == 200 and ch.get("items"):
                        self.tok[plat]["name"] = ch["items"][0]["snippet"]["title"]
                self._save()
                self.login.pop(plat, None)
                self.last[plat] = (None, 0)
                log_line(f"Titel: mit {plat.capitalize()} verbunden ({self.tok[plat].get('name', '?')})")
                self.wake.set()
                return
            err = str(j.get("message") or j.get("error") or "")
            if "slow_down" in err:
                lg["interval"] += 5
            elif not any(x in err for x in ("authorization_pending", "pending")):
                self.state[plat] = {"error": f"Anmeldung fehlgeschlagen: {err or code}"}
                self.login.pop(plat, None)
                return
        if self.login.get(plat) is lg:
            self.login.pop(plat, None)

    def logout(self, plat):
        self.tok.pop(plat, None)
        self.login.pop(plat, None)
        self.state[plat] = {}
        self._save()

    # --- Tokens ---------------------------------------------------------------
    def _refresh(self, plat, force=False):
        t = self.tok.get(plat)
        if not t:
            return False
        if not force and time.time() < t.get("exp", 0):
            return True
        if not t.get("refresh"):
            self.state[plat] = {"error": "Anmeldung abgelaufen – bitte neu verbinden", "expired": True}
            return False
        cfg = get_cfg()
        try:
            if plat == "twitch":
                data = {"client_id": cfg.get("tw_client_id", "").strip(), "grant_type": "refresh_token",
                        "refresh_token": t["refresh"]}
                if cfg.get("tw_client_secret", "").strip():      # App-Typ „Vertraulich“ braucht das Secret
                    data["client_secret"] = cfg["tw_client_secret"].strip()
                code, j = _http("POST", "https://id.twitch.tv/oauth2/token", data, form=True)
            else:
                data = {"client_id": cfg.get("yt_client_id", "").strip(),
                        "client_secret": cfg.get("yt_client_secret", "").strip(),
                        "grant_type": "refresh_token", "refresh_token": t["refresh"]}
                code, j = _http("POST", "https://oauth2.googleapis.com/token", data, form=True)
        except Exception as e:  # noqa – Internet kurz weg: Anmeldung behalten, später nochmal
            self.state[plat] = {"note": f"Keine Verbindung zu {plat.capitalize()} – versuche es gleich nochmal"}
            log_line(f"Titel: Token-Erneuerung {plat} fehlgeschlagen (Netz): {e}")
            return False
        if code == 200 and j.get("access_token"):
            t["access"] = j["access_token"]
            t["refresh"] = j.get("refresh_token") or t["refresh"]
            t["exp"] = time.time() + int(j.get("expires_in", 3600)) - 300
            t.pop("dead", None)
            self.__dict__.setdefault("_last_why", {}).pop(plat, None)
            self._save()
            return True
        why = str(j.get("error_description") or j.get("message") or j.get("error") or code)
        if self.__dict__.setdefault("_last_why", {}).get(plat) != why:   # gleiche Meldung nicht jede Minute
            self._last_why[plat] = why
            log_line(f"Titel: Token-Erneuerung {plat} abgelehnt ({code}): {why}")
        if "client secret" in why.lower():
            self.state[plat] = {"error": "Deine Twitch-App ist „Vertraulich“ – bitte das Client-Secret im Panel eintragen",
                                "expired": True}
            t["dead"] = True
            self._save()
            return False
        dead = code in (400, 401) and any(x in why.lower() for x in ("invalid", "expired", "revoked"))
        if dead:
            t["dead"] = True
            self._save()
            hint = (" (Google-App im Testmodus: Anmeldung hält nur 7 Tage)"
                    if plat == "youtube" and "expired or revoked" in why.lower() else "")
            self.state[plat] = {"error": f"Anmeldung abgelaufen – bitte neu verbinden{hint}", "expired": True}
        else:
            self.state[plat] = {"note": f"{plat.capitalize()} antwortet gerade nicht ({code}) – versuche es gleich nochmal"}
        return False

    def _call(self, plat, method, url, data=None):
        """API-Aufruf mit automatischer Token-Erneuerung bei 401."""
        for attempt in (0, 1):
            hd = self._tw_hd() if plat == "twitch" else self._yt_hd()
            code, j = _http(method, url, data, headers=hd)
            if code != 401 or attempt:
                return code, j
            if not self._refresh(plat, force=True):
                return code, j
        return code, j

    def _tw_hd(self):
        return {"Client-Id": get_cfg().get("tw_client_id", "").strip(), "Authorization": "Bearer " + self.tok["twitch"]["access"]}

    def _yt_hd(self):
        return {"Authorization": "Bearer " + self.tok["youtube"]["access"]}

    @staticmethod
    def _err(j, code):
        e = j.get("error")
        return (e.get("message") if isinstance(e, dict) else None) or j.get("message") or e or code

    # --- Titel + Kategorie setzen ------------------------------------------
    def _tw_game_id(self, name):
        if not name:
            return None
        cache = self.__dict__.setdefault("_games", {})
        if name.lower() in cache:
            return cache[name.lower()]
        from urllib.parse import quote
        gid = None
        c, j = self._call("twitch", "GET", "https://api.twitch.tv/helix/games?name=" + quote(name))
        if c == 200 and j.get("data"):
            gid = j["data"][0]["id"]
        else:
            c, j = self._call("twitch", "GET", "https://api.twitch.tv/helix/search/categories?first=1&query=" + quote(name))
            if c == 200 and j.get("data"):
                gid = j["data"][0]["id"]
        if gid:
            cache[name.lower()] = gid
        return gid

    def _set_twitch(self, title, category):
        t = self.tok["twitch"]
        body = {"title": title[:140]}
        gid = self._tw_game_id(category)
        if gid:
            body["game_id"] = gid
        code, j = self._call("twitch", "PATCH", "https://api.twitch.tv/helix/channels?broadcaster_id=" + t.get("user_id", ""), body)
        if code in (200, 204) and category and not gid:
            return True, f"Kategorie „{category}“ gibt es auf Twitch nicht"
        return code in (200, 204), self._err(j, code)

    def _yt_live(self):
        code, j = self._call("youtube", "GET", "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status"
                                               "&broadcastStatus=active&broadcastType=all&maxResults=5")
        if code == 200 and j.get("items"):
            self.yt_privacy = (j["items"][0].get("status") or {}).get("privacyStatus") or self.__dict__.get("yt_privacy")
        return code, j

    def _yt_ensure_live(self):
        """Wenn der Server an YouTube sendet, aber dort kein Livestream läuft (z. B. nach dem Beenden
        oder weil „Automatisch starten“ aus ist): neuen Livestream anlegen und mit dem Stream-Schlüssel verknüpfen."""
        cfg = get_cfg()
        t = self.tok.get("youtube")
        if not cfg.get("yt_enabled") or not cfg.get("yt_key") or not t or t.get("dead"):
            return
        if time.time() - self.__dict__.get("_yt_check", 0) < 60:
            return
        self._yt_check = time.time()
        d = (sender_status().get("dest") or {}).get("youtube") or {}
        if not d.get("live") or (d.get("uptime") or 0) < 45:
            self._yt_missing = 0
            return
        if not self._refresh("youtube"):
            return
        code, j = self._yt_live()
        if code != 200:
            return
        if j.get("items"):
            self._yt_missing = 0
            return
        self._yt_missing = self.__dict__.get("_yt_missing", 0) + 1
        if self._yt_missing < 2 or time.time() - self.__dict__.get("_yt_created", 0) < 600:
            return                                        # erst nach 2 Minuten ohne Livestream eingreifen
        self._yt_created = time.time()
        # passenden Stream (Stream-Schlüssel) finden
        c, js = self._call("youtube", "GET", "https://www.googleapis.com/youtube/v3/liveStreams?part=id,cdn&mine=true&maxResults=50")
        sid = next((x["id"] for x in (js.get("items") or []) if c == 200
                    and ((x.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName") == cfg["yt_key"]), None)
        if not sid:
            self.state["youtube"] = {"error": "Stream-Schlüssel nicht in deinem YouTube-Konto gefunden – ist es der richtige Kanal?"}
            return
        # Gibt es schon einen vorbereiteten Livestream („Stream wird vorbereitet“)? Dann den live schalten.
        c, ju = self._call("youtube", "GET", "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=id,status,contentDetails"
                                             "&broadcastStatus=upcoming&broadcastType=all&maxResults=20")
        for x in (ju.get("items") or []) if c == 200 else []:
            if (x.get("contentDetails") or {}).get("boundStreamId") != sid:
                continue
            life = (x.get("status") or {}).get("lifeCycleStatus")
            if life in ("ready", "testing", "testStarting", "liveStarting"):
                c3, j3 = self._call("youtube", "POST", "https://www.googleapis.com/youtube/v3/liveBroadcasts/transition"
                                                       f"?broadcastStatus=live&part=status&id={x['id']}")
                log_line("YouTube: vorbereiteten Livestream live geschaltet" if c3 == 200
                         else f"YouTube: Live-Schalten ging nicht ({life}): {self._err(j3, c3)}")
                if c3 == 200:
                    self.last["youtube"] = (None, 0)
                    return
        from datetime import datetime, timezone
        title, _ = self.current(cfg)
        body = {"snippet": {"title": (title or "24/7 LIVE")[:100],
                            "scheduledStartTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                "status": {"privacyStatus": self.__dict__.get("yt_privacy") or "public", "selfDeclaredMadeForKids": False},
                "contentDetails": {"enableAutoStart": True, "enableAutoStop": True, "enableDvr": True,
                                   "monitorStream": {"enableMonitorStream": False}}}
        c, b = self._call("youtube", "POST", "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status,contentDetails", body)
        if c != 200:
            self.state["youtube"] = {"error": f"Neuer YouTube-Livestream ging nicht: {self._err(b, c)}"}
            log_line(f"YouTube: neuer Livestream ging nicht: {self._err(b, c)}")
            return
        c2, j2 = self._call("youtube", "POST", "https://www.googleapis.com/youtube/v3/liveBroadcasts/bind"
                                               f"?id={b['id']}&part=id,contentDetails&streamId={sid}")
        if c2 != 200:
            log_line(f"YouTube: Livestream ließ sich nicht verknüpfen: {self._err(j2, c2)}")
            return
        log_line("YouTube: kein Livestream aktiv – neuen Livestream gestartet")
        self.last["youtube"] = (None, 0)

    def _set_youtube(self, title, category):
        code, j = self._yt_live()
        if code != 200:
            return False, self._err(j, code)
        items = j.get("items") or []
        if not items:
            return None, "Gerade kein laufender YouTube-Livestream"
        for b in items:
            err = self._yt_update(b, title[:100], str(get_cfg().get("yt_category", "20")))
            if err:
                return False, err
        return True, None

    def _yt_update(self, b, title, cat_id):
        """Titel + Kategorie eines laufenden YouTube-Livestreams setzen. None = hat geklappt."""
        c, j = self._call("youtube", "GET", "https://www.googleapis.com/youtube/v3/videos?part=snippet&id=" + b["id"])
        items = (j.get("items") or []) if c == 200 else []
        if items:
            vs = items[0]["snippet"]
            if vs.get("title") == title and vs.get("categoryId") == cat_id:
                return None
            body = {"id": b["id"], "snippet": {"title": title, "categoryId": cat_id,
                                                "description": vs.get("description", ""),
                                                **({"tags": vs["tags"]} if vs.get("tags") else {}),
                                                **({"defaultLanguage": vs["defaultLanguage"]} if vs.get("defaultLanguage") else {})}}
            c2, j2 = self._call("youtube", "PUT", "https://www.googleapis.com/youtube/v3/videos?part=snippet", body)
            if c2 == 200:
                return None
            err = self._err(j2, c2)
        else:
            err = self._err(j, c)
        sn = b["snippet"]       # Ersatzweg: nur der Titel über die Livestream-Schnittstelle
        snip = {"title": title, "description": sn.get("description", "")}
        if sn.get("scheduledStartTime"):
            snip["scheduledStartTime"] = sn["scheduledStartTime"]
        c3, j3 = self._call("youtube", "PUT", "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet",
                            {"id": b["id"], "snippet": snip})
        return None if c3 == 200 else err

    def _yt_end_before_restart(self):
        """Kurz vor dem geplanten Neuverbinden den YouTube-Livestream sauber beenden,
        damit YouTube danach einen NEUEN Livestream startet (sonst läuft der alte einfach weiter)."""
        if "youtube" not in self.tok or self.tok["youtube"].get("dead"):
            return
        d = (sender_status().get("dest") or {}).get("youtube") or {}
        nxt, up = d.get("next_planned"), d.get("uptime") or 0
        if nxt is None or nxt > 25 or up < 600:
            return
        key = int(time.time() - up)            # eine Session = ein Startzeitpunkt
        if self.__dict__.get("_ended") == key or not self._refresh("youtube"):
            return
        self._ended = key
        code, j = self._yt_live()
        for b in (j.get("items") or []) if code == 200 else []:
            c2, j2 = self._call("youtube", "POST", "https://www.googleapis.com/youtube/v3/liveBroadcasts/transition"
                                                   f"?broadcastStatus=complete&part=status&id={b['id']}")
            log_line("YouTube: Livestream vor dem geplanten Neuverbinden beendet – gleich startet ein neuer"
                     if c2 == 200 else f"YouTube: Livestream ließ sich nicht beenden: {self._err(j2, c2)}")
        self.last["youtube"] = (None, time.time() - 600 + 90)   # Titel gleich für den neuen Stream setzen

    def run(self):
        time.sleep(15)
        while True:
            try:
                self._yt_end_before_restart()
            except Exception as e:  # noqa
                print("YouTube-Neustart:", e, flush=True)
            try:
                self.tick()
            except Exception as e:  # noqa
                print("Titel:", e, flush=True)
            self.wake.wait(10)
            self.wake.clear()

    def current(self, cfg):
        vt = None
        if streamer and cfg.get("mode") == "twitch":
            vt = streamer.vod.get("title") or None     # auch zwischen zwei VODs den letzten Titel behalten
        return title_for(cfg, vt), category_for(cfg)

    def tick(self):
        cfg = get_cfg()
        if not cfg.get("title_auto"):
            return
        title, cat = self.current(cfg)
        if not title:
            return
        want = (title, cat)
        for plat, again in (("twitch", 1800), ("youtube", 600)):
            t = self.tok.get(plat)
            if not t or t.get("dead"):
                continue
            last, last_t = self.last[plat]
            if last == want and time.time() - last_t < again:
                continue
            if last_t and last != want and time.time() - last_t < 60 and last is not None:
                continue                                  # höchstens 1 Änderung pro Minute
            if not self._refresh(plat):
                self.last[plat] = (last, time.time() - again + 60)
                continue
            ok, err = (self._set_twitch if plat == "twitch" else self._set_youtube)(title, cat)
            if ok:
                if last != want:
                    log_line(f"Titel auf {plat.capitalize()}: {title} · Kategorie: {cat or '–'}")
                self.last[plat] = (want, time.time())
                self.state[plat] = {"title": title, "at": time.time(), **({"note": err} if err else {})}
            elif ok is None:      # YouTube gerade nicht live – in 1 Minute nochmal
                self.state[plat] = {"note": err}
                self.last[plat] = (None, time.time() - again + 60)
            else:
                self.state[plat] = {"error": f"Titel konnte nicht gesetzt werden: {err}"}
                self.last[plat] = (None, time.time() - again + 300)

    def status(self):
        cfg = get_cfg()
        title, cat = self.current(cfg)
        out = {"auto": cfg.get("title_auto", False), "preview": title, "category": cat}
        for plat in ("twitch", "youtube"):
            lg = self.login.get(plat)
            t = self.tok.get(plat) or {}
            out[plat] = {"connected": bool(t) and not t.get("dead"), "name": t.get("name"),
                         "login": {"code": lg["user_code"], "url": lg["url"]} if lg else None, **self.state[plat]}
        return out


title_mgr = TitleManager()

ticker = Ticker()
image_feeder = ImageFeeder()
audio_feeder = AudioFeeder()


# -------------------------------------------------------------- Streamer ----
def log_line(text):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5_000_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text + "\n")
    except OSError:
        pass


# Ziele: jedes bekommt einen eigenen Sender ("Relay") mit eigenem Wächter.
DESTINATIONS = {
    "twitch": {"name": "Twitch", "enabled": "tw_enabled", "key": "stream_key", "server": "server"},
    "youtube": {"name": "YouTube", "enabled": "yt_enabled", "key": "yt_key", "server": "yt_server"},
}


INPUT_FIFO = os.path.join(RUN_DIR, "output.ts")      # Übergabe an den Sender-Dienst
SENDER_STATUS = os.path.join(BASE, "sender_status.json")
SENDER_RESTART_FLAG = os.path.join(RUN_DIR, "restart_sender")
if not os.path.exists(INPUT_FIFO):
    os.mkfifo(INPUT_FIFO)


def sender_status():
    st = load_json(SENDER_STATUS, {})
    ok = time.time() - st.get("updated", 0) < 5
    if not ok:
        st = {"dest": {k: {"name": d["name"], "live": False, "restarts": 0, "uptime": 0,
                           "msg": "Sender-Dienst läuft nicht"} for k, d in DESTINATIONS.items()}}
    st["ok"] = ok
    return st


class Streamer:
    """Erzeugt das Bild (Countdown oder Videos) und gibt es an den Sender-Dienst weiter.
    Der Sender hält die Verbindung zu Twitch/YouTube – auch während dieses Programm neu startet."""

    def __init__(self):
        self.want = False
        self.proc = None
        self.started_at = 0
        self.restarts = 0
        self.status_msg = "Gestoppt"
        self.thread = None
        self._dirty = False
        self.fails = 0
        self.last_data = 0
        self.outq = queue.Queue(maxsize=200)
        self.lag = 0.0
        self.perf_note = ""
        self.vod = {"order": [], "url": None, "title": "", "pos": 0.0, "tries": 0, "duration": 0}
        threading.Thread(target=self._fifo_writer, daemon=True).start()
        st = load_json(STATE_PATH, {})
        self.session_started = st.get("session_started") if st.get("want") else None
        up = max((d.get("uptime", 0) for d in sender_status()["dest"].values()), default=0)
        if st.get("want") and (not self.session_started or time.time() - self.session_started < up - 30):
            # Startzeit fehlt oder ist zu spät: die Live-Zeit des Senders zählt
            self.session_started = time.time() - up
            for k, d in sender_status()["dest"].items():
                x = stats.d(k)
                x["session"] = max(x["session"], int(d.get("uptime", 0)))
                x["current"] = x["longest"] = max(x["longest"], int(d.get("uptime", 0)))
                x["total"] = max(x["total"], x["session"])
        if st.get("want"):
            self.start()  # nach Neustart/Update automatisch weitermachen

    def _save_state(self):
        save_json(STATE_PATH, {"want": self.want, "session_started": self.session_started})

    def start(self):
        with lock:
            if not self.session_started:
                self.session_started = time.time()   # neue Sitzung nur nach bewusstem Stopp
                stats.new_session()
            self.want = True
            self._save_state()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()

    def stop(self):
        with lock:
            self.want = False
            self.session_started = None
            self._save_state()
            self._kill()
            self.status_msg = "Gestoppt"

    def restart(self):
        self._kill()  # Quelle startet automatisch neu, der Sender zeigt kurz "Neu verbinden …"

    def playlist_changed(self):
        self._dirty = True

    def source_alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()

    def live(self):
        return any(d.get("live") for d in sender_status()["dest"].values())

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

    # --- Twitch-Playlist (ohne Speichern) ---
    def _next_vod(self, cfg):
        items = cfg.get("tw_playlist") or []
        if not items:
            return None
        urls = [i["url"] for i in items]
        v = self.vod
        if v["url"] in urls:                             # aktuelles VOD weiterspielen
            return next(i for i in items if i["url"] == v["url"])
        want = v.pop("next", None)
        if want in urls:                                 # im Panel ausgewählt
            url = want
        elif cfg.get("tw_order", "list") == "random":
            v["order"] = [u for u in v["order"] if u in urls]
            if not v["order"]:
                v["order"] = urls[:]
                random.shuffle(v["order"])
            url = v["order"].pop(0)
        else:                                            # Reihenfolge der Liste, danach wieder von vorne
            last = v.get("last")
            url = urls[(urls.index(last) + 1) % len(urls)] if last in urls else urls[0]
        v["last"] = url
        item = next(i for i in items if i["url"] == url)
        v.update(url=url, title=item["title"], pos=0.0, tries=0, duration=float(item.get("duration") or 0))
        nxt = v["order"][0] if v["order"] else urls[(urls.index(url) + 1) % len(urls)]
        if nxt != url:
            prefetch_stream_url(nxt)           # nächsten Link schon mal holen
        return item

    def _vod_done(self):
        self.vod.update(url=None, pos=0.0, tries=0)

    def _vod_after(self, code, reason):
        """Nach Ende eines VOD-Teils: weiter an gleicher Stelle oder nächstes VOD. True = sofort weiter."""
        v = self.vod
        if "seek" in v:                                  # im Panel vor-/zurückgespult
            v["pos"], v["tries"] = v.pop("seek"), 0
            log_line(f"Twitch-VOD gespult auf {int(v['pos'] // 60)} min")
            return True
        played = vod_progress_seconds()
        v["pos"] += played
        near_end = v["duration"] and v["pos"] >= v["duration"] - 20
        if (code == 0 and not reason) or near_end:
            log_line(f"Twitch-VOD fertig: {v['title']}")
            self._vod_done()
            return True
        v["tries"] = 0 if played > 60 else v["tries"] + 1
        _stream_url_cache.pop(v["url"], None)           # beim nächsten Versuch frischen Link holen
        if v["tries"] >= 4:
            log_line(f"Twitch-VOD '{v['title']}' bricht immer wieder ab – überspringe")
            self._vod_done()
            return True
        log_line(f"Twitch-VOD unterbrochen – mache bei {int(v['pos'] // 60)} min weiter")
        return v["tries"] <= 1

    def _stderr_reader(self, proc, log):
        """Schreibt ffmpeg-Meldungen ins Log – aber ohne Wiederholungs-Spam – und misst die Verzögerung."""
        lag_lines, last_note, last_t = 0, time.time(), None
        progress_keys = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time", "dup_frames=",
                         "drop_frames=", "speed=", "progress=")
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line.startswith("speed="):
                    # Geschwindigkeit < 1x = Server schafft keine Echtzeit -> Verzögerung aufsummieren
                    try:
                        sp = float(line.split("=", 1)[1].rstrip("x"))
                    except ValueError:
                        continue
                    now = time.time()
                    if last_t and time.time() - self.started_at > 10:    # Startphase nicht mitzählen
                        dt = now - last_t
                        self.lag = max(0.0, self.lag + (1.0 - min(sp, 1.05)) * dt) if sp < 0.97 else max(0.0, self.lag - dt * 0.05)
                        if self.lag > 1 and now - last_note > 30:
                            log.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") +
                                      f"Hinweis: Live-Bild läuft nur mit {sp:.2f}x Echtzeit – "
                                      f"{self.lag:.1f} s Verzögerung (Server ausgelastet)\n")
                            log.flush()
                            last_note = now
                    last_t = now
                    continue
                if line.startswith(progress_keys):
                    continue
                m = re.search(r"after a lag of ([\d.]+)s", line)
                if m:
                    self.lag = float(m.group(1))
                    lag_lines += 1
                    if time.time() - last_note > 30:
                        log.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") +
                                  f"Hinweis: Live-Bild hinkt {self.lag:.1f} s hinterher (Server ausgelastet)\n")
                        log.flush()
                        last_note = time.time()
                    continue
                if line:
                    log.write(line + "\n")
                    log.flush()
        except (OSError, ValueError):
            pass

    def _fifo_writer(self):
        """Schreibt das fertige Bild in die Pipe zum Sender."""
        while True:
            try:
                with open(INPUT_FIFO, "wb", buffering=0) as fd:   # wartet, bis der Sender liest
                    while True:
                        fd.write(self.outq.get())
            except (BrokenPipeError, OSError):
                time.sleep(0.5)

    def _pump(self, proc):
        try:
            while True:
                chunk = proc.stdout.read(188 * 348)
                if not chunk:
                    break
                self.last_data = time.time()
                try:
                    self.outq.put_nowait(chunk)
                except queue.Full:
                    pass                               # Sender nicht da -> verwerfen
        except (OSError, ValueError):
            pass

    def _loop(self):
        while self.want:
            cfg = get_cfg()
            if not any(cfg.get(d["enabled"]) and cfg.get(d["key"]) for d in DESTINATIONS.values()):
                self.status_msg = "Kein Ziel aktiv – Stream-Key bei Twitch oder YouTube eintragen"
                time.sleep(3)
                continue
            out = "pipe:1"
            if cfg["mode"] == "countdown":
                try:
                    cmd = build_countdown_cmd(cfg, out, fmt="mpegts")
                except Exception as e:  # noqa
                    self.status_msg = f"Fehler: {e}"
                    time.sleep(5)
                    continue
                info = "Countdown"
            elif cfg["mode"] == "twitch":
                item = self._next_vod(cfg)
                if not item:
                    self.status_msg = "Twitch-Playlist ist leer – im Panel VODs hinzufügen"
                    time.sleep(3)
                    continue
                try:
                    self.status_msg = f"Hole Abspiel-Link: {item['title']} …"
                    surl = resolve_stream_url(item["url"])
                except Exception as e:  # noqa
                    log_line(f"Twitch-VOD '{item['title']}' nicht abspielbar: {e} – nächstes")
                    _stream_url_cache.pop(item["url"], None)
                    self._vod_done()
                    time.sleep(2)
                    continue
                cmd = build_vod_cmd(cfg, surl, self.vod["pos"], out)
                info = f"Twitch-VOD: {item['title']} ab {int(self.vod['pos'] // 60)} min"
            else:
                vids = [v for v in list_videos() if v["quality"] in ("", cfg["quality"])]
                if not vids:
                    self.status_msg = "Keine fertigen Videos vorhanden"
                    time.sleep(3)
                    continue
                self._write_playlist(vids, cfg["shuffle"])
                self._dirty = False
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                       "-f", "concat", "-safe", "0", "-i", PLAYLIST_PATH,
                       "-c", "copy", "-f", "mpegts", out]
                info = f"{len(vids)} Videos"
            ran = 0
            try:
                log_line(f"Bild-Quelle startet ({info})")
                log = open(LOG_PATH, "a", encoding="utf-8")
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, env=FF_ENV, bufsize=0)
                threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
                self.lag = 0.0
                threading.Thread(target=self._stderr_reader, args=(self.proc, log), daemon=True).start()
                self.started_at = self.last_data = time.time()
                self.status_msg = "Läuft"
                reason = None
                while self.proc.poll() is None:
                    time.sleep(1)
                    if time.time() - self.last_data > WATCHDOG_SECONDS:
                        reason = "Bild-Quelle hängt – starte neu"
                        self._kill()
                        break
                    # Server kommt nicht hinterher -> sparsamere Einstellung
                    if self.lag > 4 and cfg["mode"] != "videos" and cfg.get("perf_auto", True):
                        cur = live_preset(cfg)
                        if cur == PRESETS[-1] and self.lag > 8:
                            self.perf_note = ("Server zu schwach für diese Qualität – unter Einstellungen → Qualität "
                                              "auf 720p30 stellen oder einen größeren Server nehmen")
                        if cur != PRESETS[-1]:
                            nxt = PRESETS[PRESETS.index(cur) + 1]
                            set_cfg(perf_preset=nxt)
                            reason = (f"Server kommt nicht hinterher ({self.lag:.0f} s Verzögerung) – "
                                      f"schalte auf sparsamere Einstellung '{nxt}' um")
                            self.perf_note = reason
                            self._kill()
                            break
                ran = time.time() - self.started_at
                log_line(reason or f"Bild-Quelle beendet (Code {self.proc.returncode})")
                log.close()
                if cfg["mode"] == "twitch" and self.want and self.vod["url"]:
                    if self._vod_after(self.proc.returncode, reason):
                        continue                      # direkt weiter mit dem nächsten VOD
            except Exception as e:  # noqa
                self.status_msg = f"Fehler: {e}"
            if self.want:
                self.restarts += 1
                self.fails = 0 if ran > 60 else self.fails + 1
                delay = 1 if ran > 60 else min(30, 2 * 2 ** max(0, self.fails - 1))
                for left in range(delay, 0, -1):
                    if not self.want:
                        break
                    self.status_msg = f"Startet neu in {left} s …"
                    time.sleep(1)


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


# ------------------------------------------------------------ Twitch-VODs ----
YTDLP = os.path.join(BASE, "bin", "yt-dlp")
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"


def ensure_ytdlp():
    """Lädt das Hilfsprogramm yt-dlp beim ersten Mal selbst herunter (kein SSH nötig)."""
    if os.path.exists(YTDLP):
        return
    import urllib.request
    os.makedirs(os.path.dirname(YTDLP), exist_ok=True)
    tmp = YTDLP + ".new"
    with urllib.request.urlopen(YTDLP_URL, timeout=120) as r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    os.chmod(tmp, 0o755)
    os.replace(tmp, YTDLP)


YT_COOKIES = os.path.join(BASE, "youtube_cookies.txt")


def update_ytdlp():
    """yt-dlp einmal am Tag aktualisieren – YouTube ändert oft etwas."""
    stamp = YTDLP + ".checked"
    try:
        if os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < 86400:
            return
        subprocess.run([sys.executable, YTDLP, "-U"], capture_output=True, timeout=180)
        open(stamp, "w").close()
    except Exception:  # noqa
        pass


def ytdlp(*args, **kw):
    ensure_ytdlp()
    extra = ["--cookies", YT_COOKIES] if os.path.exists(YT_COOKIES) else []
    return [sys.executable, YTDLP, "--no-warnings", *extra, *args]


def explain_ytdlp_error(msg):
    m = msg.lower()
    if "not a bot" in m or "sign in to confirm" in m or "429" in m:
        return ("YouTube blockiert den Server (Bot-Schutz). Lösung: unter Musik „YouTube-Cookies“ hinterlegen "
                "oder einen SoundCloud-Link nehmen.")
    if "private" in m or "unavailable" in m:
        return "Video nicht verfügbar (privat, gelöscht oder gesperrt)"
    if "age" in m and "confirm" in m:
        return "Altersbeschränkt – geht nur mit YouTube-Cookies"
    return msg[:200]


_vod_cache = {}


def list_vods(channel):
    channel = re.sub(r"[^A-Za-z0-9_]", "", channel)[:30]
    if not channel:
        raise RuntimeError("Kanalname fehlt")
    hit = _vod_cache.get(channel.lower())
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    vods = []
    for kind, url in (("VOD", f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"),
                      ("Highlight", f"https://www.twitch.tv/{channel}/videos?filter=highlights&sort=time")):
        r = subprocess.run(ytdlp("--flat-playlist", "-J", "--playlist-end", "40", url),
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            continue
        for e in json.loads(r.stdout or "{}").get("entries") or []:
            vurl = e.get("url") or e.get("webpage_url") or ""
            if not vurl.startswith("http"):
                vurl = f"https://www.twitch.tv/videos/{str(e.get('id', '')).lstrip('v')}"
            vods.append({"id": str(e.get("id", "")), "title": e.get("title") or "Ohne Titel", "url": vurl,
                         "duration": e.get("duration") or 0, "timestamp": e.get("timestamp") or 0, "kind": kind})
    if not vods:
        raise RuntimeError("Keine VODs gefunden – ist der Kanalname richtig und sind die VODs öffentlich?")
    vods.sort(key=lambda v: v["timestamp"], reverse=True)
    _vod_cache[channel.lower()] = (time.time(), vods)
    return vods


class VodImporter:
    """Lädt VODs direkt auf den Server und gibt sie danach an die Umwandlung weiter."""

    def __init__(self):
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, url, title, duration):
        if not re.match(r"^https://(www\.|m\.)?twitch\.tv/", url):
            raise RuntimeError("Nur Twitch-Links erlaubt")
        need = max(duration, 600) / 3600 * 2.0e9 + 1.0e9      # grob 2 GB pro Stunde + Puffer
        free = shutil.disk_usage(BASE).free
        if need > free:
            raise RuntimeError(f"Nicht genug Speicher frei ({free / 1e9:.1f} GB frei, ca. {need / 1e9:.1f} GB nötig)")
        key = "twitch_" + secrets.token_hex(4)
        converter.jobs[key] = {"state": "wartet auf Download", "progress": 0, "title": title, "error": ""}
        self.q.put((key, url, title))

    def _worker(self):
        while True:
            key, url, title = self.q.get()
            job = converter.jobs[key]
            try:
                job["state"] = "lädt von Twitch"
                out_base = os.path.join(UPLOAD_DIR, key)
                cmd = ["nice", "-n", "10"] + ytdlp(
                    "-f", "best[height<=720][fps<=30]/best[height<=720]/best", "--newline", "--no-part",
                    "--concurrent-fragments", "4", "-o", out_base + ".%(ext)s", url)
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                last = ""
                for line in p.stdout:
                    last = line.strip() or last
                    m = re.search(r"\[download\]\s+([\d.]+)%", line)
                    if m:
                        job["progress"] = int(float(m.group(1)))
                if p.wait() != 0:
                    raise RuntimeError("Download fehlgeschlagen: " + last[-200:])
                files = [f for f in os.listdir(UPLOAD_DIR) if f.startswith(key + ".")]
                if not files:
                    raise RuntimeError("Download-Datei nicht gefunden")
                converter.jobs.pop(key, None)
                converter.add(os.path.join(UPLOAD_DIR, files[0]), title)   # danach normale Umwandlung
            except Exception as e:  # noqa
                job["state"] = "fehler"
                job["error"] = str(e)[:300]
                for f in os.listdir(UPLOAD_DIR):
                    if f.startswith(key + "."):
                        os.remove(os.path.join(UPLOAD_DIR, f))


vod_importer = VodImporter()


# ---------------------------------------------------------- Musik per Link ----
class MusicLinkImporter:
    """Holt den Ton von einem Link (YouTube, SoundCloud, …) direkt auf den Server – auch ganze Playlists (bis 200 Songs)."""

    def __init__(self):
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, url):
        url = url.strip()
        if not re.match(r"^https?://", url):
            raise RuntimeError("Bitte einen vollständigen Link einfügen (https://…)")
        if "spotify.com" in url or "music.apple.com" in url:
            raise RuntimeError("Spotify-/Apple-Music-Links gehen nicht – die Songs sind kopiergeschützt")
        if shutil.disk_usage(BASE).free < 2e9:
            raise RuntimeError("Weniger als 2 GB Speicher frei")
        key = "link_" + secrets.token_hex(4)
        converter.jobs[key] = {"state": "lädt Musik", "progress": 0, "title": url[:70], "error": "", "kind": "music"}
        self.q.put((key, url))

    PARALLEL = 4                       # so viele Songs gleichzeitig

    def _worker(self):
        while True:
            key, url = self.q.get()
            job = converter.jobs[key]
            try:
                update_ytdlp()
                # 1) Liste holen (geht schnell, ohne Download)
                job["state"] = "liest Playlist"
                r = subprocess.run(ytdlp("--flat-playlist", "-J", "--playlist-end", "200", url),
                                   capture_output=True, text=True, timeout=180)
                info = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
                entries = [e for e in (info.get("entries") or []) if e]
                items = [(e.get("url") or e.get("webpage_url") or e.get("id"), e.get("title") or "") for e in entries]
                items = [(u if str(u).startswith("http") else f"https://www.youtube.com/watch?v={u}", t)
                         for u, t in items if u] or [(url, info.get("title") or "")]
                total = len(items)
                done, ok, lock_, errors = [0], [0], threading.Lock(), {}
                job.update(state="lädt Musik", progress=0, title=(info.get("title") or "Musik")[:50] + f" (0/{total})")
                todo = queue.Queue()
                for it in items:
                    todo.put(it)

                def runner():
                    while True:
                        try:
                            u, t = todo.get_nowait()
                        except queue.Empty:
                            return
                        try:
                            self._fetch_one(u)
                            with lock_:
                                ok[0] += 1
                        except Exception as e:  # noqa
                            why = explain_ytdlp_error(str(e))
                            with lock_:
                                errors[why] = errors.get(why, 0) + 1
                            log_line(f"Musik-Link: '{t or u}' übersprungen ({str(e)[:160]})")
                        with lock_:
                            done[0] += 1
                            job["progress"] = int(done[0] / total * 100)
                            job["title"] = (info.get("title") or "Musik")[:50] + f" ({done[0]}/{total})"

                threads = [threading.Thread(target=runner, daemon=True) for _ in range(min(self.PARALLEL, total))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                if not ok[0]:
                    top = max(errors.items(), key=lambda kv: kv[1])[0] if errors else "unbekannter Fehler"
                    raise RuntimeError(f"Kein Song ließ sich laden – {top}")
                if errors:
                    top = max(errors.items(), key=lambda kv: kv[1])[0]
                    log_line(f"Musik-Link: {total - ok[0]} Songs übersprungen, häufigster Grund: {top}")
                job.update(state="fertig", progress=100,
                           title=f"{ok[0]} von {total} Song{'s' if total != 1 else ''} hinzugefügt")
            except Exception as e:  # noqa
                job.update(state="fehler", error=str(e)[:300])

    def _fetch_one(self, url):
        """Einen Song laden und SOFORT in die Playlist legen."""
        tmpdir = os.path.join(RUN_DIR, "link_" + secrets.token_hex(4))
        os.makedirs(tmpdir, exist_ok=True)
        try:
            r = subprocess.run(["nice", "-n", "10"] + ytdlp(
                "-f", "bestaudio/best", "--no-part", "--no-playlist", "--max-filesize", "1500M",
                "-o", os.path.join(tmpdir, "%(title).70B.%(ext)s"), url),
                capture_output=True, text=True, timeout=1800)
            files = [f for f in os.listdir(tmpdir) if not f.endswith((".part", ".ytdl", ".json"))]
            if not files:
                raise RuntimeError((r.stderr.strip().splitlines() or ["nichts geladen"])[-1])
            for fn in files:
                self._store(os.path.join(tmpdir, fn), fn)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _store(path, fn):
        _, has_video, has_audio = probe(path)
        if not has_audio:
            raise RuntimeError("keine Tonspur")
        base = re.sub(r"[^\w .()&–-]+", "_", os.path.splitext(fn)[0]).strip() or "song"
        out = os.path.join(RUN_DIR, f"extract_{secrets.token_hex(4)}.mka")
        r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-map", "0:a:0",
                            "-c:a", "copy", out], capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-map", "0:a:0",
                                "-c:a", "libopus", "-b:a", "128k", out], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("Ton nicht lesbar")
        dst, i = os.path.join(MUSIC_DIR, base + ".mka"), 2
        while os.path.exists(dst):
            dst, i = os.path.join(MUSIC_DIR, f"{base} ({i}).mka"), i + 1
        os.replace(out, dst)


music_importer = MusicLinkImporter()


# ------------------------------------------- Twitch-VODs ohne Speichern ----
_stream_url_cache = {}


def resolve_stream_url(url):
    """Holt einen frischen Abspiel-Link für ein Twitch-VOD (Links laufen nach einiger Zeit ab)."""
    hit = _stream_url_cache.get(url)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    r = subprocess.run(ytdlp("-g", "-f", "best[height<=720][fps<=30]/best[height<=720]/best", url),
                       capture_output=True, text=True, timeout=120)
    lines = [l for l in r.stdout.splitlines() if l.startswith("http")]
    if r.returncode != 0 or not lines:
        raise RuntimeError((r.stderr.strip().splitlines() or ["kein Abspiel-Link"])[-1][:200])
    _stream_url_cache[url] = (time.time(), lines[0])
    return lines[0]


def prefetch_stream_url(url):
    threading.Thread(target=lambda: _safe(resolve_stream_url, url), daemon=True).start()


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa
        return None


VOD_PROGRESS = os.path.join(RUN_DIR, "vod_progress.txt")


def vod_audio_args(cfg):
    """Ton bei VODs: Original, stumm, eigene Musik statt VOD-Ton oder VOD-Ton leise + Musik."""
    mode = cfg.get("vod_audio", "orig")
    if mode == "orig":
        return ["-map", "0:v:0", "-map", "0:a:0?"]
    music = ["-thread_queue_size", "4096", "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", AUDIO_FIFO]
    if mode == "mute":
        return [*music, "-map", "0:v:0", "-map", "1:a", "-af", "volume=0", "-shortest"]
    if mode == "music":
        return [*music, "-map", "0:v:0", "-map", "1:a", "-shortest"]
    vol = max(0, min(100, int(cfg.get("vod_mix_volume", 30)))) / 100
    return [*music, "-filter_complex",
            f"[0:a:0]aresample=44100,volume={vol}[a0];[a0][1:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
            "-map", "0:v:0", "-map", "[aout]", "-shortest"]


def build_vod_cmd(cfg, stream_url, start_pos, output):
    w, h, fps, vb = QUALITIES.get(cfg["quality"], QUALITIES["720p30"])
    if os.path.exists(VOD_PROGRESS):
        os.remove(VOD_PROGRESS)
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
            "-reconnect_delay_max", "10", "-rw_timeout", "20000000",
            "-re", *(["-ss", f"{start_pos:.1f}"] if start_pos > 1 else []), "-i", stream_url,
            *vod_audio_args(cfg),
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                   f"fps={fps},format=yuv420p,setsar=1",
            "-c:v", "libx264", "-preset", live_preset(cfg), "-b:v", vb, "-maxrate", vb, "-bufsize", vb,
            "-x264-params", "nal-hrd=cbr:force-cfr=1:repeat-headers=1",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-progress", VOD_PROGRESS, "-stats_period", "2",
            "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", output]


def vod_progress_seconds():
    try:
        with open(VOD_PROGRESS, "r", encoding="utf-8", errors="replace") as fh:
            vals = re.findall(r"out_time_us=(\d+)", fh.read())
        return int(vals[-1]) / 1e6 if vals else 0.0
    except OSError:
        return 0.0

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
 <div id="dests" style="margin-top:12px"></div>
 <div id="runtime" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)"></div>
 <div class="btns"><button class="go" id="bStart">▶ Starten</button><button class="stop" id="bStop">■ Stoppen</button><button id="bRe">↻ Neu starten</button></div>
</section>
<section class="card"><h2>Modus</h2>
 <div class="btns" style="margin-top:0"><button id="mCd">⏱ Countdown</button><button id="mTw">📺 Twitch-VODs</button><button id="mVid">🎞 Gespeicherte Videos</button></div>
 <div id="twBox" style="display:none">
  <div class="hint">Spielt VODs direkt von Twitch ab – ohne Speicher. VODs fügst du unten bei „Videos → Direkt von Twitch“ mit „▶ Abspielen“ hinzu.</div>
  <div id="twNow" style="margin-top:10px"></div>
  <div id="twSeek" style="display:none">
   <div class="btns" style="flex-wrap:wrap"><button onclick="twSeek(-600)">⏪ 10 min</button><button onclick="twSeek(-60)">◀ 1 min</button><button onclick="twSeek(60)">1 min ▶</button><button onclick="twSeek(300)">5 min ▶</button><button onclick="twSeek(600)">10 min ⏩</button><button onclick="twSeek(1800)">30 min ⏩</button></div>
   <div style="display:flex;gap:8px;margin-top:8px"><input type="text" id="twTo" placeholder="Springen zu, z. B. 1:23:45" style="flex:1"><button class="go" onclick="twJump()">Springen</button></div>
  </div>
  <div class="btns"><button id="twSkip">⏭ Nächstes VOD</button></div>
  <label>Ton bei VODs</label>
  <select id="twAudio" onchange="api('/api/config',{vod_audio:this.value}).then(refresh)">
   <option value="music">VOD stumm – eigene Musik-Playlist spielen</option>
   <option value="mix">VOD-Ton leise + eigene Musik</option>
   <option value="mute">Komplett stumm</option>
   <option value="orig">Original-Ton vom VOD</option>
  </select>
  <div id="twMixBox" style="display:none"><label>Lautstärke VOD-Ton beim Mischen (%)</label><input type="range" id="twMix" min="0" max="100" step="5" onchange="api('/api/config',{vod_mix_volume:+this.value})"></div>
  <label>Abspiel-Reihenfolge</label>
  <select id="twOrder" onchange="api('/api/twpl_order',{order:this.value}).then(refresh)"><option value="list">Wie in der Liste (oben → unten)</option><option value="random">Zufällig</option></select>
  <label>Liste sortieren</label>
  <select id="twSort" onchange="if(this.value)api('/api/twpl_sort',{by:this.value}).then(()=>{this.value='';refresh()})"><option value="">– auswählen –</option><option value="new">Neueste zuerst</option><option value="old">Älteste zuerst</option><option value="long">Längste zuerst</option><option value="short">Kürzeste zuerst</option><option value="name">Nach Name (A–Z)</option><option value="random">Einmal durchmischen</option></select>
  <div class="hint">Mit ▲ ▼ verschiebst du einzelne VODs. ⏫ holt es ganz nach oben. „▶ Jetzt“ spielt ein VOD sofort, „⏭ Als Nächstes“ danach.</div>
  <div id="twList" style="margin-top:8px"></div>
 </div>
 <div id="cdBox">
  <label>Design</label><select id="cd_theme"></select>
  <div class="hint">Design oder Farbe wechseln = ein paar Sekunden „Neu verbinden …“ (Stream bleibt online). Alles andere wird live übernommen.</div>
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
  <div data-th="ps5 release konsole"><label>Text auf der Spielkachel (wenn kein Cover)</label><input type="text" id="cd_tile" maxlength="20"></div>
  <div data-th="ps5"><label>Spielname</label><input type="text" id="cd_game_ps5" maxlength="60" oninput="$('cd_game').value=this.value">
   <label>Text über dem Countdown</label><input type="text" id="cd_boxlabel_ps5" maxlength="60" oninput="$('cd_boxlabel').value=this.value"></div>
  <label>Text, wenn die Zeit abgelaufen ist</label><input type="text" id="cd_done" maxlength="60">
  <div class="btns"><button class="go" id="bCdSave">Speichern (live)</button><button id="bPrev">👁 Vorschau</button><button onclick="$('prev').style.display='block';$('prev').src='/api/slate_preview.png?'+Date.now()">👁 „Neu verbinden“</button></div>
  <img id="prev" style="width:100%;border-radius:10px;margin-top:14px;display:none">
  <label>Hintergrundbild <span id="bgInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">Bild wählen<input type="file" id="bgFile" accept="image/png,image/jpeg,image/webp"></label><button id="bgDel">Entfernen</button></div>
  <div data-th="ps5 release konsole"><label>Cover (quadratisch) <span id="cvInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">Cover wählen<input type="file" id="cvFile" accept="image/png,image/jpeg,image/webp"></label><button onclick="api('/api/default_cover').then(j=>{refresh();if(j.ok)showPrev()})">🎨 Vice-City-Cover verwenden</button><button id="cvDel">Entfernen</button></div></div>
  <label>Musik-Playlist <span id="muInfo"></span></label>
  <div class="btns" style="margin-top:0"><label class="filebtn">＋ Songs hinzufügen<input type="file" id="muFile" accept="audio/*,video/*,.mp3,.m4a,.mp4,.webm,.mkv" multiple></label><button id="muSkip">⏭ Nächster Song</button></div>
  <div style="display:flex;gap:8px;margin-top:8px"><input type="text" id="muLink" placeholder="Link einfügen (YouTube, SoundCloud, Playlist bis 200 Songs)" style="flex:1"><button class="go" id="bMuLink">＋ Per Link</button></div>
  <div id="muJobs"></div>
  <details style="margin-top:8px"><summary style="cursor:pointer;color:var(--mut);font-size:14px">🍪 YouTube-Cookies <span id="ckInfo"></span> – falls YouTube den Server blockt</summary>
   <div class="hint">YouTube blockt Downloads von Servern oft („not a bot“). Mit deinen YouTube-Cookies klappt es meistens:<br>
   1. Im PC-Browser die Erweiterung „Get cookies.txt LOCALLY“ installieren.<br>2. youtube.com öffnen (eingeloggt, am besten mit einem Zweit-Account).<br>
   3. In der Erweiterung „Export“ → Datei <b>cookies.txt</b> hier hochladen.<br>Die Datei bleibt nur auf deinem Server.</div>
   <div class="btns"><label class="filebtn">cookies.txt wählen<input type="file" id="ckFile" accept=".txt,text/plain"></label><button onclick="api('/api/cookies_delete').then(refresh)">Entfernen</button></div>
  </details>
  <div id="muList" style="margin-top:8px"></div>
  <label>Lautstärke: <span id="volV"></span> %</label><input type="range" id="cd_volume" min="0" max="100" style="width:100%">
  <label class="chk"><input type="checkbox" id="cd_shuffle_music"> Songs zufällig mischen</label>
  <div class="hint">Songs, Bild und Lautstärke werden live getauscht – der Stream läuft weiter. Nur lizenzfreie Musik verwenden (z. B. StreamBeats, NCS), sonst drohen DMCA-Strikes.</div>
 </div>
</section>
<section class="card"><h2>📝 Stream-Titel automatisch</h2>
 <label class="chk" style="margin-top:0"><input type="checkbox" id="tiAuto" onchange="api('/api/config',{title_auto:this.checked}).then(refresh)"> Titel auf Twitch und YouTube automatisch anpassen</label>
 <div class="hint" id="tiPrev"></div>
 <label>Titel beim Countdown ({game} = Spiel, {date} = Datum)</label><input type="text" id="ti_countdown" maxlength="140">
 <label>Titel nach dem Release ({done} = „… IST DA!“)</label><input type="text" id="ti_done" maxlength="140">
 <label>Titel bei VODs ({title} = Titel des VODs)</label><input type="text" id="ti_vod" maxlength="140">
 <label>Ersatz, wenn ein Video keinen Titel hat</label><input type="text" id="ti_fallback" maxlength="100">
 <label>Twitch-Kategorie beim Countdown ({game} = Spiel)</label><input type="text" id="ti_cat_cd" maxlength="80" placeholder="{game}">
 <label class="chk"><input type="checkbox" id="ti_cat_auto" onchange="api('/api/config',{cat_vod_auto:this.checked}).then(refresh)"> Bei VODs automatisch das Spiel aus dem VOD nehmen (auch bei mehreren Spielen pro VOD)</label>
 <label>Twitch-Kategorie bei VODs, wenn das Spiel unbekannt ist</label><input type="text" id="ti_cat_vod" maxlength="80" placeholder="z. B. Just Chatting, Fortnite, Grand Theft Auto V">
 <label>YouTube-Kategorie</label><select id="ti_ytcat"><option value="20">Gaming</option><option value="24">Unterhaltung</option><option value="22">Menschen &amp; Blogs</option></select>
 <div class="hint">Das „Spiel“-Feld bei YouTube lässt sich leider nicht automatisch setzen – YouTube bietet dafür keine Schnittstelle.</div>
 <div class="btns"><button class="go" id="bTiSave">💾 Titel &amp; Kategorie speichern</button><button id="bTiNow">↻ Jetzt setzen</button></div>
 <h3 style="margin:16px 0 4px;font-size:15px">Twitch</h3><div id="tiTw"></div>
 <label>Twitch Client-ID</label><input type="text" id="ti_tw_cid" placeholder="aus dev.twitch.tv/console">
 <label>Twitch Client-Secret (nur nötig, wenn deine App „Vertraulich“ ist)</label><input type="password" id="ti_tw_sec" placeholder="">
 <div class="btns"><button id="bTiTw">🔗 Mit Twitch verbinden</button></div>
 <h3 style="margin:16px 0 4px;font-size:15px">YouTube</h3><div id="tiYt"></div>
 <label>YouTube Client-ID</label><input type="text" id="ti_yt_cid" placeholder="…apps.googleusercontent.com">
 <label>YouTube Client-Secret</label><input type="password" id="ti_yt_sec" placeholder="">
 <div class="btns"><button id="bTiYt">🔗 Mit YouTube verbinden</button></div>
 <div class="hint">Einmalige Einrichtung: siehe Anleitung, Abschnitt „Stream-Titel“. Dein Passwort gibst du dabei nur direkt bei Twitch/Google ein.</div>
</section>
<section class="card"><h2>🎮 Nächste Spiele</h2>
 <label style="margin-top:0">Wann soll nach dem Release gewechselt werden?</label>
 <select id="gWhen2" onchange="gSetWhen(this.value)">
  <option value="0">Sofort beim Release</option><option value="1">Nach 1 Stunde</option><option value="3">Nach 3 Stunden</option>
  <option value="6">Nach 6 Stunden</option><option value="12">Nach 12 Stunden</option><option value="24">Nach 1 Tag</option>
  <option value="72">Nach 3 Tagen</option><option value="168">Nach 1 Woche</option><option value="manual">Gar nicht automatisch – nur von Hand</option>
  <option value="custom" hidden>Eigene Zeit</option>
 </select>
 <label>Zu welchem Spiel soll gewechselt werden?</label>
 <select id="gNext" onchange="api('/api/config',{game_next:this.value})"></select>
 <div id="gList" style="margin-top:10px"></div>
 <label>Eigenes Spiel hinzufügen</label>
 <div style="display:flex;gap:8px;flex-wrap:wrap"><input type="text" id="gName" placeholder="Name, z. B. Fable" style="flex:2;min-width:140px"><input type="text" id="gShort" placeholder="Kurz, z. B. Fable" style="flex:1;min-width:90px"><input type="datetime-local" id="gWhen" style="flex:1;min-width:170px"></div>
 <div class="btns"><button class="go" id="bGAdd">＋ Hinzufügen</button></div>
 <div class="hint">Die Liste großer Releases pflege ich zentral und sie kommt automatisch per Update. Wechsel passieren live – ohne Neustart.</div>
</section>
<section class="card"><h2>💬 Chat im Stream</h2>
 <label class="chk" style="margin-top:0"><input type="checkbox" id="chOn"> Twitch-Chat im Stream anzeigen</label>
 <div class="hint" id="chInfo"></div>
 <label>Twitch-Kanal</label><input type="text" id="chChan">
 <label>Discord-Link (für !discord)</label><input type="text" id="chDc" placeholder="discord.gg/…">
 <label>TikTok-Link (für !tiktok)</label><input type="text" id="chTt" placeholder="tiktok.com/@…">
 <label>Gesperrte Wörter (mit Komma getrennt) – solche Nachrichten erscheinen nicht im Stream</label><input type="text" id="chBlock">
 <label>Nutzer ausblenden (z. B. Bots)</label><input type="text" id="chIgn">
 <div class="btns"><button class="go" id="bChSave">Speichern (live)</button></div>
 <div class="hint">Links in Nachrichten werden zu [Link]. Löschst du eine Nachricht oder bannst jemanden auf Twitch, verschwindet sie sofort auch aus dem Stream.</div>
 <div id="chRecent" style="margin-top:8px"></div>
</section>
<section class="card" id="vidCard"><h2>Videos</h2>
 <label class="filebtn">＋ Videos hochladen<input type="file" id="file" accept="video/*" multiple></label>
 <div class="hint">Videos werden nach dem Upload einmal umgewandelt, danach landen sie automatisch in der Playlist.</div>
 <div style="margin-top:16px;padding-top:14px;border-top:1px solid var(--line)">
  <b>📺 Direkt von Twitch</b>
  <div class="hint"><b>▶ Abspielen</b> = direkt von Twitch streamen, braucht 0 GB Speicher (Modus „📺 Twitch-VODs“). <b>⬇ Speichern</b> = auf den Server laden (bleibt auch, wenn Twitch das VOD löscht).</div>
  <div style="display:flex;gap:8px;margin-top:8px"><input type="text" id="vodCh" placeholder="Kanalname" style="flex:1"><button class="go" id="bVods">VODs laden</button></div>
  <div id="vodList" style="margin-top:8px"></div>
 </div>
 <div id="uploads"></div><div id="jobs"></div><div id="vids" style="margin-top:10px"></div>
</section>
<section class="card"><h2>Einstellungen</h2>
 <h3 style="margin:4px 0 0;font-size:16px">🟣 Twitch</h3>
 <label class="chk"><input type="checkbox" id="tw_enabled"> An Twitch senden</label>
 <label>Twitch Stream-Key <span id="keyinfo"></span></label>
 <input type="password" id="key" placeholder="live_xxxxxxxx (leer lassen = nicht ändern)">
 <label>Twitch Ingest-Server</label><input type="text" id="server">
 <label>Geplant neu verbinden nach (Stunden) – Twitch-Limit 48 h</label><input type="text" id="tw_max_hours" inputmode="decimal">
 <h3 style="margin:22px 0 0;font-size:16px">🔴 YouTube</h3>
 <label class="chk"><input type="checkbox" id="yt_enabled"> An YouTube senden</label>
 <label>YouTube Stream-Schlüssel <span id="ytkeyinfo"></span></label>
 <input type="password" id="yt_key" placeholder="xxxx-xxxx-xxxx-xxxx (leer lassen = nicht ändern)">
 <label>YouTube Server</label><input type="text" id="yt_server">
 <label>Geplant neu verbinden nach (Stunden) – YouTube: 11,83 = 11:50 h (YouTube speichert max. 12 h)</label><input type="text" id="yt_max_hours" inputmode="decimal">
 <div class="hint">YouTube Studio → Erstellen → Livestream starten → Stream. Den Stream-Schlüssel dort kopieren. Am besten einen dauerhaften Schlüssel nehmen. Neuen Key speichern = nur diese Plattform verbindet kurz neu.</div>
 <label>Leistung (Live-Bild)</label>
 <select id="perfPreset" onchange="api('/api/config',{perf_preset:this.value})"><option value="veryfast">Beste Qualität (braucht mehr Leistung)</option><option value="superfast">Ausgewogen</option><option value="ultrafast">Sparsam (für kleine Server)</option></select>
 <label class="chk"><input type="checkbox" id="perfAuto" onchange="api('/api/config',{perf_auto:this.checked})"> Automatisch sparsamer werden, wenn der Server nicht hinterherkommt</label>
 <div class="hint" id="perfInfo"></div>
 <label>Qualität</label><select id="quality"></select>
 <div class="hint" id="qhint"></div>
 <label class="chk"><input type="checkbox" id="shuffle"> Videos zufällig mischen</label>
 <div class="btns"><button class="go" id="bSave">Speichern</button></div>
 <label>Update</label>
 <label class="chk" style="margin-top:4px"><input type="checkbox" id="autoUpd" onchange="api('/api/config',{auto_update:this.checked})"> Automatisch updaten (sofort, Stream bleibt online)</label>
 <div class="hint" id="updInfo"></div>
 <div id="senderPending" style="display:none;margin-top:8px;padding:10px;border-radius:8px;background:#2a2140">🛰 Ein Sender-Update ist bereit. Beim Neustart des Senders ist der Stream ca. 10 Sekunden offline – mach das, wann es dir passt.
  <div class="btns"><button class="go" onclick="if(confirm('Sender jetzt neu starten? Der Stream ist ca. 10 Sekunden offline.'))api('/api/restart_sender').then(refresh)">Sender jetzt neu starten</button></div></div>
 <div class="btns"><button id="bUpd">⬆ Jetzt nach Update suchen</button></div>
 <div class="hint">Lädt die neueste Version von GitHub. Der Stream bleibt online – Zuschauer sehen ein paar Sekunden „Neu verbinden …“.</div>
 <label>Statistik</label>
 <div class="btns" style="margin-top:0"><button onclick="if(confirm('Alle Stream-Zeiten auf 0 setzen?'))api('/api/stats_reset').then(refresh)">Zähler zurücksetzen</button></div>
</section>
<section class="card"><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
async function setStart(){const now=new Date(),pad=n=>String(n).padStart(2,'0');
 const def=`${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
 const v=prompt('Seit wann läuft der Stream? (Format: JJJJ-MM-TTTHH:MM, deutsche Zeit)',def);if(!v)return;
 const j=await api('/api/session_start',{when:v.trim().replace(' ','T')});if(j.ok)refresh()}
const fmtL=s=>{s=Math.max(0,Math.round(s));const d=Math.floor(s/86400),p=n=>String(n).padStart(2,'0');return (d?d+(d==1?' Tag ':' Tage '):'')+p(Math.floor(s%86400/3600))+':'+p(Math.floor(s%3600/60))+':'+p(s%60)};
const fmt=s=>{s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h} h ${m} min`:`${m} min ${s%60} s`};
const mb=b=>(b/1048576).toFixed(0)+' MB';
let cfgLoaded=false;
async function api(p,body){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 if(r.status==401)location.reload();const j=await r.json();if(j.error)alert(j.error);return j}
async function refresh(){
 const r=await fetch('/api/status');if(r.status==401)return location.reload();const s=await r.json();
 $('dot').className='dot'+(s.live?' live':'');$('st').textContent=s.live?'LIVE':(s.running?s.message:'Gestoppt');
 $('sub').textContent=(s.sender_ok?(s.running?`Zuschauer sehen: ${s.showing=='Live-Programm'?(s.mode=='twitch'?'Twitch-VODs':s.mode=='countdown'?'Countdown':'Videos'):s.showing} · `:''):'⚠ Sender-Dienst läuft nicht · ')+`Speicher frei: ${s.disk_free_gb} GB`+(s.mode=='videos'?` · ${s.videos.length} Videos (${fmt(s.total_duration)})`:'');
 $('bStart').disabled=s.running;$('bStop').disabled=!s.running;$('bRe').disabled=!s.running;
 $('vids').innerHTML=s.videos.length?s.videos.map(v=>`<div class="vid"><div class="t">${esc(v.title)}<br><small>${fmt(v.duration)} · ${mb(v.size)}${v.quality&&v.quality!=s.quality?' · ⚠ andere Qualität, wird übersprungen':''}</small></div><button onclick="del('${v.name}')">Löschen</button></div>`).join(''):'<div class="empty">Noch keine Videos.</div>';
 $('jobs').innerHTML=Object.entries(s.jobs).filter(([n,j])=>j.state!='fertig'&&j.kind!='music').map(([n,j])=>`<div class="vid"><div class="t" style="flex:1">${esc(j.title)}<br><small>${j.state}${j.error?': '+esc(j.error):''} ${(j.state=='konvertiert'||j.state=='lädt von Twitch')?j.progress+'%':''}</small><div class="bar"><i style="width:${j.progress}%"></i></div></div>${j.state=='fehler'?`<button onclick="dismiss('${n}')">OK</button>`:''}</div>`).join('');
 $('log').textContent=s.log||'(noch leer)';
 const TI=s.titles||{},TC=s.title_cfg||{};$('tiAuto').checked=TI.auto;$('tiPrev').textContent='Aktueller Titel: '+(TI.preview||'–')+(TI.category?' · Kategorie: '+TI.category:'');
 for(const[k,f]of[['ti_countdown','title_countdown'],['ti_done','title_done'],['ti_vod','title_vod'],['ti_fallback','title_fallback'],['ti_cat_cd','cat_countdown'],['ti_cat_vod','cat_vod'],['ti_ytcat','yt_category'],['ti_tw_cid','tw_client_id'],['ti_yt_cid','yt_client_id']])if(document.activeElement!==$(k)&&!$(k).dataset.dirty)$(k).value=TC[f]||'';
 $('ti_cat_auto').checked=s.cat_vod_auto!==false;$('ti_tw_sec').placeholder=s.tw_secret_set?'gespeichert ✓':'leer lassen bei „Öffentlich“';$('ti_yt_sec').placeholder=s.yt_secret_set?'gespeichert ✓':'GOCSPX-…';
 for(const[p,el,b]of[['twitch','tiTw','bTiTw'],['youtube','tiYt','bTiYt']]){const x=TI[p]||{};let h='';
  if(x.login)h=`<div class="vid" style="border-color:#ffc400"><div class="t">Öffne <a href="${esc(x.login.url)}" target="_blank" style="color:#ffc400">${esc(x.login.url)}</a><br>und gib diesen Code ein: <b style="font-size:20px;letter-spacing:2px">${esc(x.login.code)}</b></div></div>`;
  else if(x.connected)h=`<div class="hint" style="color:#4ade80">✅ Verbunden${x.name?' als <b>'+esc(x.name)+'</b>':''}${x.title?' · Titel gesetzt':''}</div>`;
  else h='<div class="hint">Nicht verbunden</div>';
  if(x.error)h+=`<div class="hint" style="color:#ff6b6b">${esc(x.error)}</div>`;else if(x.note)h+=`<div class="hint">${esc(x.note)}</div>`;
  $(el).innerHTML=h;$(b).textContent=x.connected?'✕ Trennen':(x.expired?'🔄 Neu verbinden':'🔗 Mit '+(p=='twitch'?'Twitch':'YouTube')+' verbinden');$(b).dataset.c=x.connected?1:''}
 const G=s.games||{};
 if(document.activeElement!==$('gWhen2')){const v=!G.auto?'manual':String(G.done_hours);$('gWhen2').value=[...$('gWhen2').options].some(o=>o.value==v)?v:'custom'}
 if(document.activeElement!==$('gNext')){const up=(G.list||[]).filter(g=>!g.past&&g.id!=G.current);
  $('gNext').innerHTML='<option value="">Automatisch: das nächste nach Datum</option>'+up.map(g=>`<option value="${g.id}">${esc(g.name)} (${new Date(g.target).toLocaleDateString('de-DE')})</option>`).join('');$('gNext').value=up.some(g=>g.id==G.next)?G.next:''}
 $('gList').innerHTML=(G.list||[]).filter(g=>!g.past||g.id==G.current).map(g=>`<div class="vid"><div class="t">${g.id==G.current?'▶ ':''}<b>${esc(g.name)}</b><br><small>${new Date(g.target).toLocaleString('de-DE',{weekday:'short',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})} Uhr${g.id==G.current?' · läuft gerade':''}${g.custom?' · eigenes':''}</small></div><div style="display:flex;gap:6px">${g.id!=G.current&&!g.past?`<button onclick="api('/api/game_switch',{id:'${g.id}'}).then(refresh)">Jetzt zeigen</button>`:''}${g.custom?`<button onclick="api('/api/game_remove',{id:'${g.id}'}).then(refresh)">✕</button>`:''}</div></div>`).join('');
 const CH=s.chat||{};$('chOn').checked=CH.on_stream&&CH.enabled;
 $('chInfo').textContent=CH.connected?`✅ Verbunden mit #${CH.channel} · ${CH.chatters} ${CH.chatters==1?'Person hat':'Leute haben'} in dieser Sitzung geschrieben`:'⏳ Verbinde mit dem Twitch-Chat …';
 $('chRecent').innerHTML=(CH.recent||[]).map(m=>`<div style="font-size:13px;color:var(--mut)"><b style="color:var(--txt)">${esc(m.name)}:</b> ${esc(m.text)}</div>`).join('');
 if(!window.chLoaded){window.chLoaded=1;$('chChan').value=CH.channel;$('chDc').value=CH.discord;$('chTt').value=CH.tiktok;$('chBlock').value=CH.blocklist;$('chIgn').value=CH.ignore}
 const PF=s.perf||{};$('perfPreset').value=PF.preset;$('perfAuto').checked=PF.auto;
 $('perfInfo').textContent=`Server-Auslastung: ${Math.round(PF.load/PF.cpus*100)} % (${PF.cpus} Kern${PF.cpus>1?'e':''})`+(PF.lag>1?` · ⚠ Live-Bild hinkt ${PF.lag} s hinterher`:' · Live-Bild läuft flüssig')+(PF.note?` · ${PF.note}`:'');
 const U=s.update_info||{};$('autoUpd').checked=s.auto_update;
 $('updInfo').textContent=`Version ${U.sha?U.sha.slice(0,7):'?'}${U.installed?' · installiert '+new Date(U.installed*1000).toLocaleString('de-DE'):''}${U.result?' · '+U.result:''}`;
 $('senderPending').style.display=U.sender_pending?'':'none';
 $('keyinfo').textContent=s.key_set?'✅ gesetzt':'❌ fehlt';$('ytkeyinfo').textContent=s.yt_key_set?'✅ gesetzt':'❌ fehlt';
 const ST=s.stats||{},pct=(a,b)=>b>0?Math.min(100,a/b*100).toFixed(1).replace('.',','):'–';
 $('runtime').innerHTML=(s.session_started?`<div style="font-size:15px">⏱ <b>Läuft seit Start:</b> ${fmtL(s.session_secs)}</div><div class="hint" style="margin-top:2px">Läuft bei Abbrüchen, Updates und Neustarts weiter – beginnt erst nach ■ Stoppen neu. <a href="#" style="color:var(--acc)" onclick="setStart();return false">Startzeit ändern</a></div>`:'<div class="hint">Stream gestoppt – der Zähler startet mit ▶ Starten.</div>')+
  (()=>{const ks=Object.keys(s.dest).filter(k=>ST[k]&&(ST[k].total||ST[k].session));if(ks.length<1)return'';
   const ses=ks.reduce((a,k)=>a+ST[k].session,0),tot=ks.reduce((a,k)=>a+ST[k].total,0);
   return `<div style="margin-top:10px;font-size:15px">📊 <b>Gesamt ${ks.map(k=>s.dest[k].name).join(' + ')}:</b> ${fmtL(ses)} in dieser Sitzung<br><span style="color:var(--mut);font-size:14px">Insgesamt gestreamt: ${fmt(tot)}</span></div>`})()+
  Object.entries(s.dest).filter(([k])=>ST[k]&&(ST[k].total||ST[k].session)).map(([k,d])=>{const x=ST[k];return `<div style="margin-top:8px;font-size:14px"><b>${d.name}</b>: live ${fmtL(x.session)} in dieser Sitzung (${pct(x.session,s.session_secs)} % online) · Abbrüche: ${x.session_drops} · längste Strecke am Stück: ${fmtL(x.longest)}<br><span style="color:var(--mut)">Insgesamt gestreamt: ${fmt(x.total)}</span></div>`}).join('')+
  `<div class="btns" style="margin-top:10px;align-items:center"><label class="chk" style="margin:0"><input type="checkbox" id="showRt" ${s.show_runtime?'checked':''} onchange="api('/api/config',{show_runtime:this.checked})"> Im Stream zeigen:</label>
   <select style="width:auto" onchange="api('/api/config',{runtime_mode:this.value})"><option value="start" ${s.runtime_mode=='start'?'selected':''}>● ${esc(s.runtime_label)} …</option><option value="detail" ${s.runtime_mode=='detail'?'selected':''}>● Twitch · YouTube · Gesamt</option></select>
   <select style="width:auto" onchange="api('/api/config',{runtime_pos:this.value}).then(()=>setTimeout(showPrev,500))">${Object.entries(s.runtime_positions).map(([k,v])=>`<option value="${k}" ${s.runtime_pos==k?'selected':''}>📍 ${v}</option>`).join('')}</select></div>`;
 $('dests').innerHTML=Object.values(s.dest).map(d=>`<div style="display:flex;align-items:center;gap:8px;margin:4px 0"><span class="dot${d.live?' live':''}" style="width:10px;height:10px"></span><b>${d.name}</b><span style="color:var(--mut);font-size:14px">${d.live?'LIVE seit '+fmt(d.uptime):esc(d.msg)}${d.next_planned?' · geplanter Neustart in '+fmt(d.next_planned):''}${d.restarts?' · Reconnects: '+d.restarts:''}</span></div>`).join('');
 const cd=s.mode=='countdown',tw=s.mode=='twitch';$('mCd').className=cd?'go':'';$('mTw').className=tw?'go':'';$('mVid').className=(!cd&&!tw)?'go':'';
 $('cdBox').style.display=cd?'':'none';$('twBox').style.display=tw?'':'none';
 $('twSeek').style.display=s.tw_now?'':'none';
 $('twNow').innerHTML=s.tw_now?`🔴 Läuft: <b>${esc(s.tw_now.title)}</b><br><small style="color:var(--mut)">${fmt(s.tw_now.pos)}${s.tw_now.duration?' / '+fmt(s.tw_now.duration):''}</small>`:'';
 if(document.activeElement!==$('twAudio'))$('twAudio').value=s.vod_audio||'orig';$('twMixBox').style.display=s.vod_audio=='mix'?'':'none';if(document.activeElement!==$('twMix'))$('twMix').value=s.vod_mix_volume;
 if(document.activeElement!==$('twOrder'))$('twOrder').value=s.tw_order||'list';
 $('twList').innerHTML=s.tw_playlist.length?s.tw_playlist.map((v,i)=>{const cur=v.url==s.tw_cur,nx=v.url==s.tw_next;return `<div class="vid"${cur?' style="border-color:#ffc400"':''}><div class="t">${i+1}. ${cur?'▶ ':''}${esc(v.title)}<br><small>${v.timestamp?new Date(v.timestamp*1000).toLocaleDateString('de-DE')+' · ':''}${v.duration?fmt(v.duration):''}${cur?' · läuft gerade':''}${nx?' · kommt als Nächstes':''}</small></div><div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end" data-u="${esc(v.url)}"><button onclick="twMv(this,'top')" ${i?'':'disabled'} title="Ganz nach oben">⏫</button><button onclick="twMv(this,'up')" ${i?'':'disabled'}>▲</button><button onclick="twMv(this,'down')" ${i<s.tw_playlist.length-1?'':'disabled'}>▼</button>${cur?'':`<button class="go" onclick="twPlay(this,1)">▶ Jetzt</button><button onclick="twPlay(this,0)">⏭ Als Nächstes</button>`}<button onclick="twRemove(this.parentNode.dataset.u)">✕</button></div></div>`}).join(''):'<div class="empty">Noch keine VODs in der Playlist.</div>';
 $('bgInfo').textContent=s.cd_bg?'✅':'(keins)';$('cvInfo').textContent=s.cd_cover?'✅':'(keins)';$('muInfo').textContent=s.music.length?`(${s.music.length} Songs)`:'(leer – Stille)';
 $('muJobs').innerHTML=Object.entries(s.jobs).filter(([n,j])=>j.kind=='music'&&(j.state!='fertig'||true)).slice(-5).map(([n,j])=>`<div class="vid"><div class="t" style="flex:1">${esc(j.title)}<br><small>${j.state}${j.error?': '+esc(j.error):''}${j.state=='lädt Musik'?' '+j.progress+'%':''}</small><div class="bar"><i style="width:${j.progress}%"></i></div></div>${j.state=='fehler'||j.state=='fertig'?`<button onclick="dismiss('${n}')">OK</button>`:''}</div>`).join('');
 $('ckInfo').textContent=s.yt_cookies?'✅':'';
 $('muList').innerHTML=s.music.map(m=>`<div class="vid"><div class="t">${m==s.now_playing?'🔊 ':''}${esc(m)}</div><button onclick="delSong(this.dataset.n)" data-n="${esc(m)}">Löschen</button></div>`).join('');
 if(!cfgLoaded){$('cd_theme').innerHTML=Object.entries(s.themes).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');
  for(const k of CDF)$(k).value=s.cfg[k];$('cd_game_ps5').value=s.cfg.cd_game;$('cd_boxlabel_ps5').value=s.cfg.cd_boxlabel;$('cd_shuffle_music').checked=s.cfg.cd_shuffle_music;$('cd_volume').value=s.cfg.cd_volume;$('volV').textContent=s.cfg.cd_volume;showTh()}
 if(!cfgLoaded){cfgLoaded=true;$('server').value=s.server;$('yt_server').value=s.yt_server;$('tw_enabled').checked=s.tw_enabled;$('yt_enabled').checked=s.yt_enabled;$('tw_max_hours').value=s.tw_max_hours;$('yt_max_hours').value=s.yt_max_hours;$('shuffle').checked=s.shuffle;
  $('quality').innerHTML=s.qualities.map(q=>`<option ${q==s.quality?'selected':''}>${q}</option>`).join('')}
 $('quality').disabled=s.quality_locked;$('qhint').textContent=s.quality_locked?'Qualität kann nur geändert werden, wenn keine Videos vorhanden sind (alle Videos müssen das gleiche Format haben).':'';
}
const esc=t=>String(t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function del(n){if(confirm('Video löschen?')){await api('/api/delete',{name:n});refresh()}}
async function dismiss(n){await api('/api/dismiss',{name:n});refresh()}
$('bStart').onclick=async()=>{await api('/api/start');refresh()};
$('bStop').onclick=async()=>{if(confirm('Stream wirklich stoppen?')){await api('/api/stop');refresh()}};
$('bRe').onclick=async()=>{await api('/api/restart');refresh()};
$('bSave').onclick=async()=>{const b={server:$('server').value,shuffle:$('shuffle').checked,quality:$('quality').value,
 yt_server:$('yt_server').value,tw_enabled:$('tw_enabled').checked,yt_enabled:$('yt_enabled').checked,
 tw_max_hours:parseFloat($('tw_max_hours').value.replace(',','.'))||0,yt_max_hours:parseFloat($('yt_max_hours').value.replace(',','.'))||0};
 if($('key').value.trim())b.stream_key=$('key').value.trim();if($('yt_key').value.trim())b.yt_key=$('yt_key').value.trim();const j=await api('/api/config',b);if(j.ok){$('key').value='';$('yt_key').value='';alert('Gespeichert ✅')}refresh()};
$('file').onchange=()=>{for(const f of $('file').files)upload(f);$('file').value=''};
function upload(f){const id='u'+Math.random().toString(36).slice(2);const el=document.createElement('div');el.className='vid';el.id=id;
 el.innerHTML=`<div class="t" style="flex:1">${esc(f.name)}<br><small>Upload 0%</small><div class="bar"><i style="width:0"></i></div></div>`;$('uploads').appendChild(el);
 const x=new XMLHttpRequest();x.open('PUT','/api/upload?name='+encodeURIComponent(f.name));
 x.upload.onprogress=e=>{const p=Math.round(e.loaded/e.total*100);el.querySelector('small').textContent=`Upload ${p}% (${mb(e.loaded)} / ${mb(e.total)})`;el.querySelector('i').style.width=p+'%'};
 x.onload=()=>{el.remove();if(x.status!=200)alert('Upload fehlgeschlagen: '+x.responseText);refresh()};
 x.onerror=()=>{el.querySelector('small').textContent='Upload fehlgeschlagen'};x.send(f)}
$('mCd').onclick=async()=>{await api('/api/config',{mode:'countdown'});refresh()};
$('mVid').onclick=async()=>{await api('/api/config',{mode:'videos'});refresh()};
$('mTw').onclick=async()=>{await api('/api/config',{mode:'twitch'});refresh()};
$('twSkip').onclick=async()=>{await api('/api/twpl_skip');setTimeout(refresh,800)};
async function twSeek(d){const j=await api('/api/twpl_seek',{delta:d});if(j.ok)setTimeout(refresh,2500)}
async function twJump(){const p=$('twTo').value.trim().split(':').map(Number);if(!p.length||p.some(isNaN))return alert('Zeit z. B. so eingeben: 1:23:45 oder 45:00');
 const sec=p.reduce((a,x)=>a*60+x,0);const j=await api('/api/twpl_seek',{to:sec});if(j.ok){$('twTo').value='';setTimeout(refresh,2500)}}
async function twMv(b,to){await api('/api/twpl_move',{url:b.parentNode.dataset.u,to});refresh()}
async function twPlay(b,now){b.disabled=true;await api('/api/twpl_play',{url:b.parentNode.dataset.u,now:!!now});setTimeout(refresh,now?1500:200)}
async function twRemove(u){if(confirm('VOD aus der Playlist entfernen?')){await api('/api/twpl_remove',{url:u});refresh()}}
const CDF=['cd_theme','cd_target','cd_color','cd_badge','cd_head1','cd_hl','cd_head2','cd_sub','cd_game','cd_boxlabel','cd_title','cd_tile','cd_done'];
function showTh(){const t=$('cd_theme').value;document.querySelectorAll('[data-th]').forEach(e=>e.style.display=e.dataset.th.split(' ').includes(t)?'':'none')}
$('cd_theme').onchange=showTh;
$('bCdSave').onclick=async()=>{const b={};for(const k of CDF)b[k]=$(k).value;b.cd_shuffle_music=$('cd_shuffle_music').checked;
 const j=await api('/api/config',b);if(j.ok){setTimeout(showPrev,400);if(j.restarted)alert('Design geändert – Zuschauer sehen ein paar Sekunden „Neu verbinden …“, die Verbindung bleibt bestehen.')}};
function showPrev(){$('prev').style.display='block';$('prev').src='/api/preview.png?'+Date.now()}
$('bPrev').onclick=showPrev;
let volT;$('cd_volume').oninput=e=>{$('volV').textContent=e.target.value;clearTimeout(volT);volT=setTimeout(()=>api('/api/config',{cd_volume:+e.target.value}),400)};
$('cd_shuffle_music').onchange=e=>api('/api/config',{cd_shuffle_music:e.target.checked});
async function asset(kind,f){
 const CH=16*1024*1024,total=Math.max(1,Math.ceil(f.size/CH)),id=Math.random().toString(36).slice(2)+Date.now().toString(36);
 for(let i=0;i<total;i++){
  let ok=false,err='';
  for(let tryNo=1;tryNo<=6&&!ok;tryNo++){
   try{
    const r=await fetch(`/api/asset_chunk?kind=${kind}&name=${encodeURIComponent(f.name)}&id=${id}&index=${i}&total=${total}&offset=${i*CH}&size=${f.size}`,{method:'PUT',body:f.slice(i*CH,(i+1)*CH)});
    if(r.status==401)return location.reload();
    const j=await r.json();if(j.error){err=j.error;if(r.status==400&&!/unvollständig/.test(j.error))break}else ok=true;
   }catch(e){err='Verbindung unterbrochen';await new Promise(z=>setTimeout(z,2000*tryNo))}
  }
  if(!ok){alert('Upload fehlgeschlagen: '+err);refresh();return}
  if(kind=='music')$('muInfo').textContent=`(lädt hoch: ${esc(f.name)} ${Math.round((i+1)/total*100)} % · ${mb(Math.min(f.size,(i+1)*CH))} / ${mb(f.size)}${i==total-1?' – wird verarbeitet …':''})`;
 }
 refresh();if(kind=='bg'||kind=='cover')showPrev();
}
$('bgFile').onchange=e=>{if(e.target.files[0])asset('bg',e.target.files[0]);e.target.value=''};
$('muFile').onchange=async e=>{const fs=[...e.target.files];e.target.value='';$('muInfo').textContent='(lädt hoch …)';for(const f of fs)await asset('music',f)};
$('bgDel').onclick=async()=>{await api('/api/asset_delete',{kind:'bg'});refresh();showPrev()};
$('cvFile').onchange=e=>{if(e.target.files[0])asset('cover',e.target.files[0]);e.target.value=''};
$('cvDel').onclick=async()=>{await api('/api/asset_delete',{kind:'cover'});refresh();showPrev()};
async function delSong(n){if(confirm('Song löschen?')){await api('/api/asset_delete',{kind:'music',name:n});refresh()}}
$('ckFile').onchange=e=>{if(e.target.files[0])asset('cookies',e.target.files[0]).then(()=>alert('Cookies gespeichert ✅ – jetzt den Link nochmal einfügen.'));e.target.value=''};
function gSetWhen(v){api('/api/config',v=='manual'?{game_auto:false}:{game_auto:true,game_done_hours:parseFloat(v)})}
['ti_countdown','ti_done','ti_vod','ti_fallback','ti_cat_cd','ti_cat_vod','ti_ytcat','ti_tw_cid','ti_yt_cid'].forEach(k=>$(k).oninput=$(k).onchange=()=>$(k).dataset.dirty=1);
$('bTiSave').onclick=async()=>{const b={title_countdown:$('ti_countdown').value,title_done:$('ti_done').value,title_vod:$('ti_vod').value,title_fallback:$('ti_fallback').value,cat_countdown:$('ti_cat_cd').value,cat_vod:$('ti_cat_vod').value,yt_category:$('ti_ytcat').value};
 const j=await api('/api/config',b);if(j.ok){['ti_countdown','ti_done','ti_vod','ti_fallback','ti_cat_cd','ti_cat_vod','ti_ytcat'].forEach(k=>delete $(k).dataset.dirty);refresh()}};
$('bTiNow').onclick=()=>api('/api/title_now').then(()=>setTimeout(refresh,3000));
async function tiConnect(p,b){if(b.dataset.c){if(confirm('Verbindung trennen?'))await api('/api/title_logout',{platform:p});return refresh()}
 const c=p=='twitch'?{tw_client_id:$('ti_tw_cid').value,...($('ti_tw_sec').value?{tw_client_secret:$('ti_tw_sec').value}:{})}:{yt_client_id:$('ti_yt_cid').value,...($('ti_yt_sec').value?{yt_client_secret:$('ti_yt_sec').value}:{})};
 await api('/api/config',c);delete $('ti_tw_cid').dataset.dirty;delete $('ti_yt_cid').dataset.dirty;$('ti_yt_sec').value='';$('ti_tw_sec').value='';
 const j=await api('/api/title_login',{platform:p});if(j.ok)refresh()}
$('bTiTw').onclick=function(){tiConnect('twitch',this)};$('bTiYt').onclick=function(){tiConnect('youtube',this)};
$('bGAdd').onclick=async()=>{const j=await api('/api/game_add',{name:$('gName').value,short:$('gShort').value,target:$('gWhen').value});if(j.ok){$('gName').value=$('gShort').value=$('gWhen').value='';refresh()}};
$('chOn').onchange=e=>api('/api/config',{chat_on_stream:e.target.checked,chat_enabled:true});
$('bChSave').onclick=async()=>{const j=await api('/api/config',{chat_channel:$('chChan').value,discord_link:$('chDc').value,tiktok_link:$('chTt').value,chat_blocklist:$('chBlock').value,chat_ignore:$('chIgn').value});if(j.ok)alert('Gespeichert ✅')};
$('bMuLink').onclick=async()=>{const u=$('muLink').value.trim();if(!u)return;const j=await api('/api/music_link',{url:u});if(j.ok){$('muLink').value='';refresh()}};
$('muSkip').onclick=async()=>{await api('/api/music_skip');setTimeout(refresh,800)};
$('bUpd').onclick=async()=>{if(!confirm('Neueste Version installieren? Der Stream bleibt dabei online.'))return;
 const j=await api('/api/update');if(!j.ok)return;
 if(j.sender_changed&&confirm('Update installiert ✅ Der Stream bleibt online.\n\nAuch der Sender (Verbindung zu Twitch/YouTube) hat ein Update. Jetzt neu starten? Dann ist der Stream ca. 10 Sekunden offline. Du kannst das auch später machen.'))
  await api('/api/restart_sender');
 else if(!j.sender_changed)alert('Update installiert ✅ Der Stream bleibt online – Zuschauer sehen kurz „Neu verbinden …“.');
 setTimeout(()=>location.reload(),5000)};
let VODS=[];
$('bVods').onclick=async()=>{$('vodList').innerHTML='<div class="hint">Lade Liste von Twitch …</div>';
 const r=await fetch('/api/vods?channel='+encodeURIComponent($('vodCh').value.trim()));const j=await r.json();
 if(j.error){$('vodList').innerHTML=`<div class="hint" style="color:#ff6b6b">${esc(j.error)}</div>`;return}
 VODS=j.vods;$('vodList').innerHTML=VODS.map((v,i)=>`<div class="vid"><div class="t">${esc(v.title)}<br><small>${v.kind}${v.timestamp?' · '+new Date(v.timestamp*1000).toLocaleDateString('de-DE'):''}${v.duration?' · '+fmt(v.duration):''}</small></div><div style="display:flex;gap:6px"><button class="go" onclick="playVod(${i},this)">▶ Abspielen</button><button onclick="impVod(${i},this)">⬇ Speichern</button></div></div>`).join('')+
 (VODS.length>1?`<div class="btns"><button class="go" onclick="playAll(this)">▶ Alle ${VODS.length} zur Playlist</button></div>`:'')};
async function playVod(i,b){const v=VODS[i];b.disabled=true;const j=await api('/api/twpl_add',{items:[v]});b.textContent=j.ok?'✅ drin':'Fehler';refresh()}
async function playAll(b){b.disabled=true;const j=await api('/api/twpl_add',{items:VODS});b.textContent=j.ok?'✅ alle drin':'Fehler';refresh()}
async function impVod(i,b){const v=VODS[i];b.disabled=true;const j=await api('/api/vod_import',{url:v.url,title:v.title,duration:v.duration});
 b.textContent=j.ok?'✅ läuft':'Fehler';refresh()}
fetch('/api/status').then(r=>r.json()).then(s=>{$('vodCh').value=s.vod_channel||'xImJxstinn'});
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
        if path == "/api/vods":
            ch = (parse_qs(urlparse(self.path).query).get("channel") or [get_cfg()["vod_channel"]])[0]
            try:
                vods = list_vods(ch)
                set_cfg(vod_channel=re.sub(r"[^A-Za-z0-9_]", "", ch)[:30])
                return self._send(200, {"ok": True, "vods": vods})
            except Exception as e:  # noqa
                return self._send(200, {"error": f"VODs konnten nicht geladen werden: {e}"})
        if path == "/api/slate_preview.png":
            try:
                src = build_slate_loop()
                out = os.path.join(RUN_DIR, "slate_preview.png")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.0", "-i", src, "-frames:v", "1", "-update", "1", out],
                               check=True, timeout=30)
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
            if "yt_key" in data:
                upd["yt_key"] = str(data["yt_key"]).strip()
            if "yt_server" in data and str(data["yt_server"]).startswith(("rtmp://", "rtmps://")):
                upd["yt_server"] = str(data["yt_server"]).strip()
            for k in ("tw_enabled", "yt_enabled"):
                if k in data:
                    upd[k] = bool(data[k])
            for k, lim in (("tw_max_hours", 47.9), ("yt_max_hours", 23.9)):
                if k in data:
                    try:
                        upd[k] = round(max(0.0, min(lim, float(data[k]))), 2)
                    except (TypeError, ValueError):
                        pass
            if "shuffle" in data:
                upd["shuffle"] = bool(data["shuffle"])
            if data.get("mode") in ("videos", "countdown", "twitch"):
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
            if data.get("perf_preset") in PRESETS:
                upd["perf_preset"] = data["perf_preset"]
            if "perf_auto" in data:
                upd["perf_auto"] = bool(data["perf_auto"])
            for k in ("chat_enabled", "chat_on_stream"):
                if k in data:
                    upd[k] = bool(data[k])
            for k, n in (("chat_channel", 30), ("chat_blocklist", 500), ("chat_ignore", 300),
                         ("discord_link", 80), ("tiktok_link", 80)):
                if k in data:
                    upd[k] = clean_text(str(data[k]))[:n]
            if "game_auto" in data:
                upd["game_auto"] = bool(data["game_auto"])
            if "game_done_hours" in data:
                try:
                    upd["game_done_hours"] = max(0.0, min(168.0, float(data["game_done_hours"])))
                except (TypeError, ValueError):
                    pass
            for k, n in (("cat_countdown", 80), ("cat_vod", 80), ("title_countdown", 140), ("title_done", 140), ("title_vod", 140), ("title_fallback", 100),
                         ("tw_client_id", 80), ("tw_client_secret", 100), ("yt_client_id", 200), ("yt_client_secret", 100)):
                if k in data:
                    upd[k] = (re.sub(r"\s+", " ", "".join(c for c in str(data[k]) if c.isprintable())).strip()[:n]
                              if k.startswith(("title", "cat_")) else str(data[k]).strip()[:n])
            if data.get("tw_client_secret") and title_mgr.tok.get("twitch"):
                title_mgr.tok["twitch"].pop("dead", None)     # mit Secret die alte Anmeldung nochmal versuchen
                title_mgr.tok["twitch"]["exp"] = 0
                title_mgr.state["twitch"] = {}
                title_mgr.last["twitch"] = (None, 0)
                title_mgr.wake.set()
            if "cat_vod_auto" in data:
                upd["cat_vod_auto"] = bool(data["cat_vod_auto"])
            if "title_auto" in data:
                upd["title_auto"] = bool(data["title_auto"])
            if "yt_category" in data and str(data["yt_category"]).isdigit():
                upd["yt_category"] = str(data["yt_category"])
            if data.get("vod_audio") in ("orig", "mute", "music", "mix"):
                upd["vod_audio"] = data["vod_audio"]
            if "vod_mix_volume" in data:
                try:
                    upd["vod_mix_volume"] = max(0, min(100, int(data["vod_mix_volume"])))
                except (TypeError, ValueError):
                    pass
            if any(k.startswith(("title", "cat_", "yt_category")) for k in data):
                title_mgr.last = {"twitch": (None, 0), "youtube": (None, 0)}
                title_mgr.wake.set()
            if "game_next" in data:
                upd["game_next"] = str(data["game_next"])[:60]
            if "auto_update" in data:
                upd["auto_update"] = bool(data["auto_update"])
            if "show_runtime" in data:
                upd["show_runtime"] = bool(data["show_runtime"])
            if data.get("runtime_pos") in RUNTIME_POS:
                upd["runtime_pos"] = data["runtime_pos"]
            if data.get("runtime_mode") in ("start", "detail"):
                upd["runtime_mode"] = data["runtime_mode"]
            if "runtime_label" in data:
                upd["runtime_label"] = str(data["runtime_label"])[:30]
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
            if "quality" in changed:
                threading.Thread(target=lambda: _safe(build_slate_loop), daemon=True).start()
            if changed & {"cd_theme", "quality"}:
                try:
                    rebuild_layers()
                except Exception as e:  # noqa
                    print("Ebenen-Fehler:", e, flush=True)
            v = streamer.vod
            if changed & {"vod_audio", "vod_mix_volume"} and get_cfg()["mode"] == "twitch" and v["url"]:
                v["seek"] = v["pos"] + (vod_progress_seconds() if streamer.source_alive() else 0)
                streamer._kill()                     # gleiches VOD an gleicher Stelle, neuer Ton
            restart = bool(changed & RESTART_KEYS)
            if streamer.want and restart:
                streamer.restart()
            # neue Stream-Keys erkennt der Sender selbst und verbindet nur das betroffene Ziel neu
            return self._send(200, {"ok": True, "restarted": restart and streamer.want})
        elif path == "/api/asset_delete":
            if data.get("kind") in ("bg", "cover"):
                fp = BG_ORIG if data["kind"] == "bg" else COVER_ORIG
                if os.path.exists(fp):
                    os.remove(fp)
                rebuild_layers()                     # wird live entfernt
            else:
                name = os.path.basename(str(data.get("name", "")))
                p = os.path.join(MUSIC_DIR, name)
                if name and os.path.exists(p):
                    if audio_feeder.current == p:
                        audio_feeder.skip = True
                    os.remove(p)
        elif path == "/api/restart_sender":
            open(SENDER_RESTART_FLAG, "w").close()           # Sender beendet sich, systemd startet ihn neu
            v = load_json(VERSION_PATH, {})
            v["sender_pending"] = False
            save_json(VERSION_PATH, v)
        elif path == "/api/default_cover":
            import urllib.request
            try:
                tmp = os.path.join(RUN_DIR, "default_cover.png")
                with urllib.request.urlopen(RAW_BASE + "stream24/assets/vice_city_cover.png", timeout=30) as r, \
                        open(tmp, "wb") as fh:
                    shutil.copyfileobj(r, fh)
                save_upload_image(tmp, "cover")
                os.remove(tmp)
                rebuild_layers()
            except Exception as e:  # noqa
                return self._send(200, {"error": f"Cover konnte nicht geladen werden: {e}"})
        elif path == "/api/cookies_delete":
            if os.path.exists(YT_COOKIES):
                os.remove(YT_COOKIES)
        elif path == "/api/music_link":
            try:
                music_importer.add(str(data.get("url", "")))
            except Exception as e:  # noqa
                return self._send(200, {"error": str(e)})
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
        elif path == "/api/twpl_add":
            items = data.get("items") or []
            pl = get_cfg().get("tw_playlist") or []
            have = {i["url"] for i in pl}
            for it in items[:200]:
                url = str(it.get("url", ""))
                if re.match(r"^https://(www\.|m\.)?twitch\.tv/", url) and url not in have:
                    pl.append({"url": url, "title": str(it.get("title", "Twitch-VOD"))[:80],
                               "duration": float(it.get("duration") or 0),
                               "timestamp": float(it.get("timestamp") or 0)})
                    have.add(url)
            set_cfg(tw_playlist=pl)
        elif path == "/api/twpl_remove":
            url = str(data.get("url", ""))
            set_cfg(tw_playlist=[i for i in (get_cfg().get("tw_playlist") or []) if i["url"] != url])
            if streamer.vod["url"] == url and get_cfg()["mode"] == "twitch":
                streamer._vod_done()
                streamer._kill()                         # Sender zeigt kurz "Neu verbinden …", dann nächstes VOD
        elif path == "/api/session_start":
            # Startzeit von Hand setzen ("YYYY-MM-DDTHH:MM", deutsche Zeit)
            try:
                ts = target_epoch({"cd_target": str(data.get("when", ""))[:16]})[0]
            except Exception:  # noqa
                return self._send(200, {"error": "Ungültige Zeit"})
            if ts > time.time():
                return self._send(200, {"error": "Die Startzeit darf nicht in der Zukunft liegen"})
            if not streamer.want:
                return self._send(200, {"error": "Der Stream läuft gerade nicht"})
            streamer.session_started = ts
            streamer._save_state()
        elif path == "/api/title_login":
            plat = "youtube" if data.get("platform") == "youtube" else "twitch"
            err = title_mgr.start_login(plat)
            if err:
                return self._send(200, {"error": err})
        elif path == "/api/title_logout":
            title_mgr.logout("youtube" if data.get("platform") == "youtube" else "twitch")
        elif path == "/api/title_now":
            title_mgr.last = {"twitch": (None, 0), "youtube": (None, 0)}
            title_mgr.wake.set()
        elif path == "/api/game_add":
            name = clean_text(str(data.get("name", "")))[:60]
            when = str(data.get("target", ""))[:16]
            try:
                target_epoch({"cd_target": when})
            except Exception:  # noqa
                return self._send(200, {"error": "Ungültiges Datum"})
            if not name:
                return self._send(200, {"error": "Name fehlt"})
            gid = "c_" + re.sub(r"[^a-z0-9]", "", name.lower())[:20] + "_" + when[:10].replace("-", "")
            short = clean_text(str(data.get("short") or name))[:30]
            custom = [g for g in (get_cfg().get("games_custom") or []) if g["id"] != gid]
            custom.append({"id": gid, "name": name, "short": short, "tile": short[:20], "target": when})
            set_cfg(games_custom=custom)
        elif path == "/api/game_remove":
            gid = str(data.get("id", ""))
            set_cfg(games_custom=[g for g in (get_cfg().get("games_custom") or []) if g["id"] != gid])
        elif path == "/api/game_switch":
            gid = str(data.get("id", ""))
            g = next((x for x in all_games() if x["id"] == gid), None) if gid else next_game(time.time())
            if not g:
                return self._send(200, {"error": "Kein passendes Spiel gefunden"})
            switch_game(g, "von Hand")
        elif path == "/api/stats_reset":
            stats.reset_all()
        elif path == "/api/twpl_move":
            pl = get_cfg().get("tw_playlist") or []
            url, d = str(data.get("url", "")), str(data.get("to", ""))
            i = next((k for k, x in enumerate(pl) if x["url"] == url), None)
            if i is not None:
                it = pl.pop(i)
                j = {"top": 0, "bottom": len(pl), "up": max(0, i - 1), "down": min(len(pl), i + 1)}.get(d, i)
                pl.insert(j, it)
                set_cfg(tw_playlist=pl)
        elif path == "/api/twpl_sort":
            by = str(data.get("by", ""))
            pl = get_cfg().get("tw_playlist") or []
            keys = {"new": (lambda x: -x.get("timestamp", 0)), "old": (lambda x: x.get("timestamp", 0)),
                    "long": (lambda x: -x.get("duration", 0)), "short": (lambda x: x.get("duration", 0)),
                    "name": (lambda x: x.get("title", "").lower())}
            if by == "random":
                random.shuffle(pl)
            elif by in keys:
                pl.sort(key=keys[by])
            set_cfg(tw_playlist=pl)
            streamer.vod["order"] = []
        elif path == "/api/twpl_order":
            set_cfg(tw_order="random" if data.get("order") == "random" else "list")
            streamer.vod["order"] = []
        elif path == "/api/twpl_play":
            url = str(data.get("url", ""))
            if any(x["url"] == url for x in (get_cfg().get("tw_playlist") or [])):
                streamer.vod["next"] = url
                if data.get("now") and get_cfg()["mode"] == "twitch" and streamer.vod["url"] != url:
                    streamer._vod_done()
                    streamer._kill()                     # kurz "Neu verbinden …", dann das gewählte VOD
        elif path == "/api/twpl_seek":
            v = streamer.vod
            if get_cfg()["mode"] != "twitch" or not v["url"]:
                return self._send(200, {"error": "Gerade läuft kein Twitch-VOD"})
            cur = v["pos"] + (vod_progress_seconds() if streamer.source_alive() else 0)
            try:
                new = float(data["to"]) if "to" in data else cur + float(data.get("delta", 0))
            except (TypeError, ValueError):
                return self._send(200, {"error": "Ungültige Zeit"})
            top = (v["duration"] - 30) if v["duration"] else new
            v["seek"] = max(0.0, min(new, top))
            streamer._kill()                             # kurz "Neu verbinden …", dann ab der neuen Stelle
        elif path == "/api/twpl_skip":
            if get_cfg()["mode"] == "twitch":
                streamer._vod_done()
                streamer._kill()
        elif path == "/api/vod_import":
            try:
                vod_importer.add(str(data.get("url", "")), str(data.get("title", "Twitch-VOD"))[:80],
                                 float(data.get("duration") or 0))
            except Exception as e:  # noqa
                return self._send(200, {"error": str(e)})
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
        if u.path == "/api/asset_chunk":
            return self._chunk_upload(u)
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
        allowed = {"bg": (".png", ".jpg", ".jpeg", ".webp"), "cover": (".png", ".jpg", ".jpeg", ".webp"),
                   "music": MUSIC_EXT}
        if kind not in allowed or ext not in allowed[kind]:
            return self._send(400, {"error": "Dateityp nicht erlaubt"})
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0:
            return self._send(400, {"error": "Leere Datei"})
        free = shutil.disk_usage(BASE).free
        if size + 500_000_000 > free:
            return self._send(400, {"error": f"Nicht genug Speicher frei ({free // 1_000_000} MB frei, Datei {size // 1_000_000} MB)"})
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
            if kind in ("bg", "cover"):
                save_upload_image(tmp, kind)
                rebuild_layers()                     # wird live ausgetauscht
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

    def _chunk_upload(self, u):
        """Große Dateien in Stücken (z. B. 16 MB) – bricht die Verbindung kurz ab, wird nur das Stück wiederholt."""
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        kind = qs.get("kind", "")
        name = os.path.basename(unquote(qs.get("name", "")))
        ext = os.path.splitext(name)[1].lower()
        uid = re.sub(r"[^a-zA-Z0-9]", "", qs.get("id", ""))[:32]
        try:
            index, total, offset = int(qs["index"]), int(qs["total"]), int(qs["offset"])
            fullsize = int(qs["size"])
        except (KeyError, ValueError):
            return self._send(400, {"error": "Ungültige Anfrage"})
        allowed = {"bg": (".png", ".jpg", ".jpeg", ".webp"), "cover": (".png", ".jpg", ".jpeg", ".webp"),
                   "music": MUSIC_UPLOAD_EXT, "cookies": (".txt",)}
        if kind not in allowed or ext not in allowed[kind] or not uid:
            return self._send(400, {"error": f"Dateityp {ext or '?'} nicht erlaubt"})
        tmp = os.path.join(RUN_DIR, f"chunk_{uid}{ext}")
        if index == 0:
            free = shutil.disk_usage(BASE).free
            if fullsize + 500_000_000 > free:
                return self._send(400, {"error": f"Nicht genug Speicher frei ({free // 1_000_000} MB frei, "
                                                 f"Datei {fullsize // 1_000_000} MB)"})
            open(tmp, "wb").close()
        if not os.path.exists(tmp):
            return self._send(400, {"error": "Upload abgelaufen – bitte neu starten"})
        size = int(self.headers.get("Content-Length") or 0)
        data = b""
        while len(data) < size:
            chunk = self.rfile.read(min(1 << 20, size - len(data)))
            if not chunk:
                return self._send(400, {"error": "Stück unvollständig"})
            data += chunk
        with open(tmp, "r+b") as fh:                 # an die richtige Stelle schreiben (Wiederholung = überschreiben)
            fh.seek(offset)
            fh.write(data)
        if index < total - 1:
            return self._send(200, {"ok": True, "next": index + 1})
        if os.path.getsize(tmp) != fullsize:
            return self._send(400, {"error": "Datei unvollständig angekommen – bitte nochmal"})
        try:
            self._finish_asset(kind, name, ext, tmp)
        except Exception as e:  # noqa
            if os.path.exists(tmp):
                os.remove(tmp)
            return self._send(400, {"error": str(e)})
        self._send(200, {"ok": True, "done": True})

    def _finish_asset(self, kind, name, ext, tmp):
        if kind == "cookies":
            txt = open(tmp, "r", encoding="utf-8", errors="replace").read()
            os.remove(tmp)
            if "youtube.com" not in txt or "\t" not in txt:
                raise RuntimeError("Das ist keine cookies.txt mit YouTube-Cookies (Netscape-Format)")
            with open(YT_COOKIES, "w", encoding="utf-8") as fh:
                fh.write(txt)
            os.chmod(YT_COOKIES, 0o600)
            return
        if kind in ("bg", "cover"):
            save_upload_image(tmp, kind)
            rebuild_layers()                         # wird live ausgetauscht
            os.remove(tmp)
            return
        base = re.sub(r"[^\w .()&-]+", "_", os.path.splitext(name)[0]).strip() or "song"
        _, has_video, has_audio = probe(tmp)
        if not has_audio:
            raise RuntimeError("In der Datei ist keine Tonspur")
        if has_video or ext not in MUSIC_EXT:
            # Video (z. B. 10-Stunden-Mix als .mp4): nur den Ton herausziehen, ohne Qualitätsverlust
            out = os.path.join(RUN_DIR, f"extract_{secrets.token_hex(4)}.mka")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp, "-vn", "-map", "0:a:0", "-c:a", "copy", out],
                               capture_output=True, text=True)
            if r.returncode != 0:
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp, "-vn", "-map", "0:a:0",
                                    "-c:a", "libopus", "-b:a", "128k", out], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("Ton konnte nicht aus der Datei gelesen werden")
            os.remove(tmp)
            tmp, ext = out, ".mka"
        dst, i = os.path.join(MUSIC_DIR, base + ext), 2
        while os.path.exists(dst):
            dst, i = os.path.join(MUSIC_DIR, f"{base} ({i}){ext}"), i + 1
        os.replace(tmp, dst)                         # kommt automatisch in die Playlist

    def _update(self):
        ok, info = do_update()
        if not ok:
            return self._send(200, {"error": info})
        self._send(200, {"ok": True, "sender_changed": info})
        threading.Timer(1.0, restart_panel).start()

def status():
    cfg = get_cfg()
    vids = list_videos()
    return {
        "running": streamer.want,
        "live": streamer.live(),
        "message": streamer.status_msg,
        "uptime": max((d.get("uptime", 0) for d in sender_status()["dest"].values()), default=0),
        "restarts": streamer.restarts,
        "videos": [{k: v[k] for k in ("name", "title", "duration", "size", "quality")} for v in vids],
        "total_duration": sum(v["duration"] for v in vids),
        "jobs": converter.jobs,
        "key_set": bool(cfg["stream_key"]),
        "yt_key_set": bool(cfg["yt_key"]),
        "tw_enabled": cfg["tw_enabled"], "yt_enabled": cfg["yt_enabled"], "yt_server": cfg["yt_server"],
        "tw_max_hours": cfg["tw_max_hours"], "yt_max_hours": cfg["yt_max_hours"],
        "sender_version": sender_status().get("version", 0),
        "dest": sender_status()["dest"],
        "sender_ok": sender_status()["ok"],
        "showing": sender_status().get("showing", ""),
        "server": cfg["server"],
        "quality": cfg["quality"],
        "qualities": list(QUALITIES),
        "quality_locked": bool(vids) or converter.busy(),
        "shuffle": cfg["shuffle"],
        "mode": cfg["mode"],
        "vod_channel": cfg["vod_channel"],
        "session_started": streamer.session_started,
        "auto_update": cfg.get("auto_update", True),
        "titles": title_mgr.status(),
        "title_cfg": {k: cfg.get(k, DEFAULT_CFG[k]) for k in ("title_countdown", "title_done", "title_vod", "title_fallback", "cat_countdown", "cat_vod", "yt_category",
                                                            "tw_client_id", "yt_client_id")},
        "yt_secret_set": bool(cfg.get("yt_client_secret")), "tw_secret_set": bool(cfg.get("tw_client_secret")), "cat_vod_auto": cfg.get("cat_vod_auto", True),
        "vod_audio": cfg.get("vod_audio", "orig"), "vod_mix_volume": cfg.get("vod_mix_volume", 30),
        "games": {"auto": cfg.get("game_auto", True), "done_hours": cfg.get("game_done_hours", 12),
                  "current": cfg.get("game_current"), "next": cfg.get("game_next", ""),
                  "list": [{**g, "custom": g["id"].startswith("c_"),
                            "past": target_epoch({"cd_target": g["target"]})[0] < time.time()} for g in all_games()]},
        "chat": {"enabled": cfg.get("chat_enabled", True), "on_stream": cfg.get("chat_on_stream", True),
                 "channel": cfg.get("chat_channel", ""), "connected": twitch_chat.connected,
                 "chatters": len(twitch_chat.chatters), "blocklist": cfg.get("chat_blocklist", ""),
                 "ignore": cfg.get("chat_ignore", ""), "discord": cfg.get("discord_link", ""),
                 "tiktok": cfg.get("tiktok_link", ""),
                 "recent": [{"name": m["name"], "text": m["text"]} for m in list(twitch_chat.msgs)[-5:]]},
        "perf": {"preset": live_preset(cfg), "auto": cfg.get("perf_auto", True), "lag": round(streamer.lag, 1),
                 "load": round(os.getloadavg()[0], 2), "cpus": os.cpu_count() or 1, "note": streamer.perf_note},
        "update_info": {"result": auto_updater.last_result, "checked": auto_updater.last_check,
                        **{k: v for k, v in load_json(VERSION_PATH, {}).items() if k in ("sha", "installed", "sender_pending")}},
        "session_secs": time.time() - streamer.session_started if streamer.session_started else 0,
        "stats": stats.data["dest"],
        "show_runtime": cfg.get("show_runtime", True), "runtime_label": cfg.get("runtime_label", "LIVE seit"),
        "runtime_mode": cfg.get("runtime_mode", "start"),
        "runtime_pos": cfg.get("runtime_pos", "tr"), "runtime_positions": RUNTIME_POS,
        "tw_playlist": cfg.get("tw_playlist") or [], "tw_order": cfg.get("tw_order", "list"),
        "tw_cur": streamer.vod["url"], "tw_next": streamer.vod.get("next"),
        "tw_now": {"title": streamer.vod["title"], "pos": streamer.vod["pos"] + (vod_progress_seconds() if streamer.source_alive() else 0),
                   "duration": streamer.vod["duration"]} if streamer.vod["url"] else None,
        "cfg": {k: v for k, v in cfg.items() if k.startswith("cd_")},
        "themes": THEMES,
        "cd_bg": os.path.exists(BG_ORIG),
        "cd_cover": os.path.exists(COVER_ORIG),
        "music": [os.path.basename(m) for m in list_music()],
        "yt_cookies": os.path.exists(YT_COOKIES),
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
    if os.path.exists(BG_LAYER) and not os.path.exists(BG_ORIG):
        shutil.copy(BG_LAYER, BG_ORIG)       # Hintergrund aus älterer Version übernehmen
    try:
        rebuild_layers()
    except Exception as e:  # noqa
        print("Ebenen-Fehler:", e, flush=True)
    ticker.tick()
    threading.Thread(target=lambda: _safe(build_slate_loop), daemon=True).start()
    if not load_json(VERSION_PATH, {}).get("sha"):
        _safe(lambda: save_json(VERSION_PATH, {"sha": latest_sha(), "installed": time.time()}))
    for t in (ticker, image_feeder, audio_feeder, stats, auto_updater, twitch_chat, game_queue, title_mgr):
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
    import signal

    def _shutdown(*_):
        stats.save()
        streamer._kill()                                     # ffmpeg nicht verwaist zurücklassen
        os._exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"24/7 Stream Panel läuft auf Port {PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
