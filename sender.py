#!/usr/bin/env python3
"""xImJxstinn 24/7 Stream – SENDER

Eigener, kleiner Dienst, der die Verbindung zu Twitch und YouTube hält.
Das Panel (server.py) liefert das fertige Bild über eine Pipe. Startet das Panel neu
(z. B. bei einem Update), zeigt der Sender so lange "Neu verbinden …" – die Verbindung
zu Twitch/YouTube bleibt dabei bestehen. Die Zeitstempel werden hier nahtlos
fortgeschrieben, damit Twitch/YouTube keinen Neustart bemerken.

Dieser Dienst wird bei normalen Updates NICHT neu gestartet.
"""
import datetime
import io
import json
import os
import queue
import signal
import subprocess
import threading
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:  # noqa
    ZoneInfo = None

BASE = os.environ.get("STREAM24_DIR", "/opt/stream24")
CFG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "stream.log")
STATUS_PATH = os.path.join(BASE, "sender_status.json")
RUN_DIR = os.path.join(BASE, "countdown", "run")
INPUT_FIFO = os.path.join(RUN_DIR, "output.ts")
RESTART_FLAG = os.path.join(RUN_DIR, "restart_sender")
SENDER_VERSION = 4

QUALITIES = {"720p30": (1280, 720, 30, "3000k"), "720p60": (1280, 720, 60, "4500k"),
             "1080p30": (1920, 1080, 30, "4500k"), "1080p60": (1920, 1080, 60, "6000k")}
WATCHDOG_SECONDS = 15
SLATE_AFTER = 1.5          # so lange ohne Bild vom Panel -> "Neu verbinden …"
DESTINATIONS = {
    "twitch": {"name": "Twitch", "enabled": "tw_enabled", "key": "stream_key", "server": "server",
               "max_hours": "tw_max_hours"},
    "youtube": {"name": "YouTube", "enabled": "yt_enabled", "key": "yt_key", "server": "yt_server",
                "max_hours": "yt_max_hours"},
}
DEFAULTS = {"tw_max_hours": 47.5, "yt_max_hours": 23.5, "tw_enabled": True, "yt_enabled": False, "stream_key": "", "yt_key": "",
            "server": "rtmp://ingest.global-contribute.live-video.net/app",
            "yt_server": "rtmp://a.rtmp.youtube.com/live2", "quality": "720p30",
            "yt_daily_restart": True, "yt_daily_time": "11:55:00", "yt_daily_pause": 10}
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
os.makedirs(RUN_DIR, exist_ok=True)
if not os.path.exists(INPUT_FIFO):
    os.mkfifo(INPUT_FIFO)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return default


def get_cfg():
    return {**DEFAULTS, **load_json(CFG_PATH, {})}


def log_line(text):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 5_000_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text + "\n")
    except OSError:
        pass


# ------------------------------------------------------------- Zeitstempel ----
WRAP = 1 << 33


def _rd_ts(p, i):
    return ((p[i] >> 1) & 7) << 30 | p[i + 1] << 22 | (p[i + 2] >> 1) << 15 | p[i + 3] << 7 | p[i + 4] >> 1


def _wr_ts(p, i, v):
    v %= WRAP
    p[i] = (p[i] & 0xF0) | ((v >> 29) & 0x0E) | 1
    p[i + 1] = (v >> 22) & 0xFF
    p[i + 2] = ((v >> 14) & 0xFE) | 1
    p[i + 3] = (v >> 7) & 0xFF
    p[i + 4] = ((v << 1) & 0xFE) | 1


class Retimer:
    """Schreibt die Zeitstempel des MPEG-TS um, sodass sie über jeden Quellenwechsel
    hinweg lückenlos weiterlaufen. Nach einem Wechsel wird bis zum nächsten Keyframe gewartet."""
    VIDEO_PID = 0x100

    def __init__(self):
        self.last_dts = None
        self.delta = None
        self.wait_key = True
        self.buf = b""

    def new_session(self):
        self.delta = None
        self.wait_key = True
        self.buf = b""

    def process(self, data):
        data = self.buf + data
        out = bytearray()
        i, n = 0, len(data)
        while i + 188 <= n:
            if data[i] != 0x47:                   # Synchronisation verloren -> nächstes Sync-Byte suchen
                i += 1
                continue
            p = bytearray(data[i:i + 188])
            i += 188
            pid = ((p[1] & 0x1F) << 8) | p[2]
            pusi, afc = p[1] & 0x40, (p[3] >> 4) & 3
            pos, rai, pcr_at = 4, False, None
            if afc & 2:
                alen = p[4]
                if alen > 0:
                    flags = p[5]
                    rai = bool(flags & 0x40)
                    if flags & 0x10 and alen >= 7:
                        pcr_at = 6
                pos = 5 + alen
            if self.wait_key:
                if pid < 0x20 or pid == 0x1000:   # PAT/PMT/SDT immer durchlassen
                    out += p
                    continue
                if not (pid == self.VIDEO_PID and rai):
                    continue
                self.wait_key = False
            if pusi and afc & 1 and pos + 19 <= 188 and p[pos] == 0 and p[pos + 1] == 0 and p[pos + 2] == 1:
                sid = p[pos + 3]
                if sid not in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
                    ptsdts = (p[pos + 7] >> 6) & 3
                    if ptsdts & 2:
                        pts = _rd_ts(p, pos + 9)
                        dts = _rd_ts(p, pos + 14) if ptsdts == 3 else pts
                        if self.delta is None:
                            base = (self.last_dts + 9000) if self.last_dts is not None else dts
                            self.delta = (base - dts) % WRAP
                        _wr_ts(p, pos + 9, pts + self.delta)
                        if ptsdts == 3:
                            _wr_ts(p, pos + 14, dts + self.delta)
                        if pid == self.VIDEO_PID:
                            self.last_dts = (dts + self.delta) % WRAP
            if pcr_at is not None and self.delta is not None:
                base = (p[6] << 25 | p[7] << 17 | p[8] << 9 | p[9] << 1 | p[10] >> 7)
                b = (base + self.delta) % WRAP
                p[6], p[7], p[8], p[9] = (b >> 25) & 0xFF, (b >> 17) & 0xFF, (b >> 9) & 0xFF, (b >> 1) & 0xFF
                p[10] = ((b & 1) << 7) | (p[10] & 0x7F)
            out += p
        self.buf = data[i:]
        return bytes(out)


# ------------------------------------------------------------------ Relays ----
class Relay:
    """Schickt den Stream per -c copy an EIN Ziel. Eigener Wächter, eigene Reconnects."""

    def __init__(self, dest_id):
        self.id = dest_id
        self.d = DESTINATIONS[dest_id]
        self.q = queue.Queue(maxsize=400)
        self.proc = None
        self.started_at = 0
        self.last_progress = 0
        self.restarts = 0
        self.fails = 0
        self.status_msg = "Aus"
        self.kick = False
        self.planned = 0
        self.hold_until = 0
        self.daily_done = ""
        threading.Thread(target=self._loop, daemon=True).start()

    def max_hours(self):
        try:
            return max(0.0, float(get_cfg().get(self.d["max_hours"]) or 0))
        except (TypeError, ValueError):
            return 0.0

    def daily_pause(self, cfg):
        try:
            return max(0.0, min(300.0, float(cfg.get("yt_daily_pause", 10))))
        except (TypeError, ValueError):
            return 10.0

    def daily_due(self, cfg, now=None):
        """True, genau einmal pro Tag, wenn die eingestellte Uhrzeit erreicht ist (nur YouTube)."""
        if self.id != "youtube" or not cfg.get("yt_daily_restart"):
            return False
        try:
            parts = (str(cfg.get("yt_daily_time") or "11:55:00").strip().split(":") + ["0", "0"])[:3]
            hh, mm, ss = (int(x) for x in parts)
            if now is None:
                try:
                    tz = ZoneInfo("Europe/Berlin") if ZoneInfo else None
                except Exception:  # noqa
                    tz = None
                now = datetime.datetime.now(tz)
            target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        except Exception:  # noqa
            return False
        late = (now - target).total_seconds()
        day = target.strftime("%Y-%m-%d")
        if 0 <= late < 60 and self.daily_done != day and self.started_at < target.timestamp():
            self.daily_done = day
            return True
        return False

    def sig(self, cfg):
        return (cfg.get(self.d["enabled"]), cfg.get(self.d["key"]), cfg.get(self.d["server"]))

    def enabled(self, cfg):
        return bool(cfg.get(self.d["enabled"]) and cfg.get(self.d["key"]))

    def live(self):
        return bool(self.proc and self.proc.poll() is None and self.last_progress > self.started_at
                    and time.time() - self.last_progress < 5)

    def push(self, chunk):
        if self.proc and self.proc.poll() is None:
            try:
                self.q.put_nowait(chunk)
            except queue.Full:
                pass

    def restart(self):
        self.kick = True
        self.kill()

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def _writer(self, proc):
        try:
            while proc.poll() is None:
                try:
                    chunk = self.q.get(timeout=1)
                except queue.Empty:
                    continue
                proc.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _progress(self, proc):
        last = None
        try:
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    val = line.strip().split("=", 1)[1]
                    if val != last and val not in ("N/A", "0"):
                        last = val
                        self.last_progress = time.time()
                        self.status_msg = "Live"
        except Exception:  # noqa
            pass

    def _loop(self):
        name = self.d["name"]
        while True:
            cfg = get_cfg()
            if not sender.want:
                self.status_msg = "Gestoppt"
                time.sleep(1)
                continue
            if not self.enabled(cfg):
                self.status_msg = "Aus" if not cfg.get(self.d["enabled"]) else "Kein Stream-Key"
                time.sleep(1)
                continue
            if time.time() < self.hold_until:
                self.status_msg = f"Pause vor Neustart … {int(self.hold_until - time.time()) + 1} s"
                time.sleep(0.5)
                continue
            if time.time() - sender.last_out > 3:
                self.status_msg = "Wartet auf Bild …"
                time.sleep(0.5)
                continue
            while not self.q.empty():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
            url = cfg[self.d["server"]].rstrip("/") + "/" + cfg[self.d["key"]]
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "2500000",
                   "-f", "mpegts", "-i", "pipe:0", "-map", "0", "-c", "copy",
                   "-rw_timeout", str(WATCHDOG_SECONDS * 1_000_000),
                   "-progress", "pipe:1", "-stats_period", "1",
                   "-f", "flv", "-flvflags", "no_duration_filesize", url]
            log_line(f"{name}: verbinde …")
            ran = 0
            try:
                log = open(LOG_PATH, "a", encoding="utf-8")
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=log, bufsize=0)
                self.proc.stdout = io.TextIOWrapper(io.BufferedReader(self.proc.stdout),
                                                    encoding="utf-8", errors="replace")
                self.started_at = time.time()
                self.last_progress = 0
                self.status_msg = "Verbindet …"
                threading.Thread(target=self._writer, args=(self.proc,), daemon=True).start()
                threading.Thread(target=self._progress, args=(self.proc,), daemon=True).start()
                reason = None
                while self.proc.poll() is None:
                    time.sleep(1)
                    cfg_now = get_cfg()
                    if self.daily_due(cfg_now):
                        pause = self.daily_pause(cfg_now)
                        reason = (f"{name}: täglicher Neustart um {cfg_now.get('yt_daily_time')} – "
                                  f"Pause {pause:g} s, dann verbinde neu")
                        self.status_msg = "Täglicher Neustart …"
                        self.kick = True
                        self.planned += 1
                        self.hold_until = time.time() + pause
                        self.kill()
                        break
                    max_h = self.max_hours()
                    if max_h and time.time() - self.started_at > max_h * 3600:
                        reason = f"{name}: geplanter Neustart nach {max_h:g} h (Plattform-Limit) – verbinde sofort neu"
                        self.status_msg = "Geplanter Neustart …"
                        self.kick = True
                        self.planned += 1
                        self.kill()
                        break
                    stalled = time.time() - max(self.last_progress, self.started_at)
                    if stalled > 5:
                        self.status_msg = f"Keine Verbindung seit {int(stalled)} s …"
                    limit = WATCHDOG_SECONDS if self.last_progress else WATCHDOG_SECONDS * 2
                    if stalled > limit:
                        reason = f"{name}: seit {int(stalled)} s kein Fortschritt – verbinde neu"
                        self.kill()
                        break
                ran = time.time() - self.started_at
                log_line(reason or f"{name}: Verbindung beendet (Code {self.proc.returncode})")
                log.close()
            except Exception as e:  # noqa
                self.status_msg = f"Fehler: {e}"
            if self.kick or not sender.want:
                self.kick = False
                continue
            self.restarts += 1
            self.fails = 0 if ran > 60 else self.fails + 1
            delay = min(30, 2 * 2 ** max(0, self.fails - 1))
            for left in range(delay, 0, -1):
                if not sender.want or self.kick:
                    break
                self.status_msg = f"Verbindet neu in {left} s … (Versuch {self.fails or 1})"
                time.sleep(1)


# ------------------------------------------------------------------ Sender ----
def slate_cmd(cfg):
    w, h, fps, vb = QUALITIES.get(cfg.get("quality"), QUALITIES["720p30"])
    # Vom Panel vorbereitete, animierte Schleife ("Neu verbinden …") -> nur abspielen, kaum CPU.
    # So kann das Design über Panel-Updates geändert werden, ohne den Sender neu zu starten.
    loop = os.path.join(RUN_DIR, f"slate_{w}x{h}.mp4")
    if os.path.exists(loop) and os.path.getsize(loop) > 10000:
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", loop,
                "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "pipe:1"]
    bg = os.path.join(RUN_DIR, f"slate_bg_{w}x{h}.png")
    if not os.path.exists(bg):
        g = "exp(-((X-W/2)^2+(Y-H*0.45)^2)/(2*(H*0.32)^2))"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}", "-vf",
                        f"geq=r='clip(14+40*{g},0,255)':g='clip(10+12*{g},0,255)':b='clip(26+70*{g},0,255)'",
                        "-frames:v", "1", "-update", "1", bg], check=False)
    reg = next((f for f in FONTS if os.path.exists(f)), FONTS[0])
    bold = reg.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    bold = bold if os.path.exists(bold) else reg
    files = {}
    for name, text in (("s_icon", "↻"), ("s_title", "Neu verbinden …"), ("s_sub", "Gleich geht's weiter")):
        files[name] = os.path.join(RUN_DIR, name + ".txt")
        with open(files[name], "w", encoding="utf-8") as fh:
            fh.write(text)
    q = lambda p: "'" + p.replace("'", "") + "'"   # noqa
    vf = (f"scale={w}:{h},fps={fps},format=yuv420p,"
          f"drawtext=fontfile={q(reg)}:textfile={q(files['s_icon'])}:expansion=none:fontsize=h*0.34:"
          f"fontcolor=0x9b5cf6:alpha='0.7+0.3*sin(t*3)':x=(w-text_w)/2:y=h*0.27,"
          f"drawtext=fontfile={q(bold)}:textfile={q(files['s_title'])}:expansion=none:fontsize=h*0.075:"
          f"fontcolor=white:x=(w-text_w)/2:y=h*0.62,"
          f"drawtext=fontfile={q(reg)}:textfile={q(files['s_sub'])}:expansion=none:fontsize=h*0.035:"
          f"fontcolor=white@0.6:x=(w-text_w)/2:y=h*0.73")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-loop", "1", "-framerate", str(fps), "-i", bg,
            "-re", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", vf, "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", vb, "-maxrate", vb, "-bufsize", vb,
            "-x264-params", "nal-hrd=cbr:force-cfr=1:repeat-headers=1",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "pipe:1"]


class Sender:
    def __init__(self):
        self.want = bool(load_json(STATE_PATH, {}).get("want"))
        self.last_input = 0
        self.last_out = 0
        self.source = None            # "input" (Panel) oder "slate"
        self.slate = None
        self.retimer = Retimer()
        self.lock = threading.Lock()
        self.relays = {}

    def start(self):
        self.relays = {k: Relay(k) for k in DESTINATIONS}
        threading.Thread(target=self._input_loop, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    def feed(self, src, data):
        with self.lock:
            if src == "slate" and time.time() - self.last_input < SLATE_AFTER:
                return
            if src != self.source:
                self.source = src
                self.retimer.new_session()
                log_line("Sender: sende " + ("Live-Programm vom Panel" if src == "input" else "'Neu verbinden …'"))
            out = self.retimer.process(data)
        if out:
            self.last_out = time.time()
            for r in self.relays.values():
                r.push(out)

    def _input_loop(self):
        while True:
            try:
                with open(INPUT_FIFO, "rb", buffering=0) as f:   # wartet auf das Panel
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.last_input = time.time()
                        if self.want:
                            self.feed("input", chunk)
            except OSError:
                time.sleep(0.5)

    def _slate_pump(self, proc):
        try:
            while True:
                chunk = proc.stdout.read(188 * 348)
                if not chunk:
                    break
                self.feed("slate", chunk)
        except (OSError, ValueError):
            pass

    def _kill_slate(self):
        p = self.slate
        if p and p.poll() is None:
            p.kill()
            p.wait()
        self.slate = None

    def _monitor(self):
        sigs = {}
        while True:
            time.sleep(0.25)
            if os.path.exists(RESTART_FLAG):
                os.remove(RESTART_FLAG)
                log_line("Sender: Neustart angefordert")
                shutdown()
            st = load_json(STATE_PATH, {})
            self.want = bool(st.get("want"))
            cfg = get_cfg()
            # Ziel-Einstellungen geändert -> nur dieses Ziel neu verbinden
            for k, r in self.relays.items():
                s = r.sig(cfg)
                if k in sigs and sigs[k] != s:
                    r.restart()
                sigs[k] = s
            if not self.want:
                self._kill_slate()
                for r in self.relays.values():
                    r.kill()
            else:
                stale = time.time() - self.last_input > SLATE_AFTER
                connected = any(r.proc and r.proc.poll() is None for r in self.relays.values())
                if stale and connected and not (self.slate and self.slate.poll() is None):
                    try:
                        self.slate = subprocess.Popen(slate_cmd(cfg), stdin=subprocess.DEVNULL,
                                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                        threading.Thread(target=self._slate_pump, args=(self.slate,), daemon=True).start()
                    except Exception as e:  # noqa
                        log_line(f"Sender: 'Neu verbinden' konnte nicht starten: {e}")
                elif not stale and self.slate:
                    self._kill_slate()
            self._write_status()

    def _write_status(self, _last=[0]):
        if time.time() - _last[0] < 1:
            return
        _last[0] = time.time()
        data = {"updated": time.time(), "version": SENDER_VERSION, "want": self.want,
                "showing": "Live-Programm" if self.source == "input" and time.time() - self.last_input < 2
                else ("Neu verbinden …" if self.slate else "–"),
                "dest": {k: {"name": r.d["name"], "live": r.live(), "msg": r.status_msg, "restarts": r.restarts,
                             "uptime": time.time() - r.started_at if r.live() else 0, "planned": r.planned,
                             "next_planned": (r.max_hours() * 3600 - (time.time() - r.started_at))
                             if r.live() and r.max_hours() else None}
                         for k, r in self.relays.items()}}
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATUS_PATH)


sender = Sender()


def shutdown(*_):
    sender._kill_slate()
    for r in sender.relays.values():
        r.kill()
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log_line("Sender gestartet")
    sender.start()
    while True:
        time.sleep(3600)
