#!/usr/bin/env bash
# xImJxstinn 24/7 Stream – Installer für Ubuntu/Debian (z. B. Hetzner Cloud)
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Bitte als root ausführen:  sudo bash install.sh"
  exit 1
fi

DIR=/opt/stream24
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Pakete installieren (ffmpeg, python3, Schrift) ..."
apt-get update -qq
apt-get install -y -qq ffmpeg python3 fonts-dejavu-core fonts-dejavu-extra >/dev/null

echo "==> Dateien kopieren nach $DIR ..."
id stream24 >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin stream24
mkdir -p "$DIR"
for F in server.py sender.py; do
  if [ -f "$SRC/$F" ]; then
    cp "$SRC/$F" "$DIR/$F"
  else
    echo "==> $F von GitHub laden ..."
    curl -fsSL "https://raw.githubusercontent.com/ximjxstinn/stream-marathon/stream24/$F" -o "$DIR/$F"
  fi
done

if [ -f /etc/systemd/system/stream24.service ] && grep -q password_hash "$DIR/config.json" 2>/dev/null; then
  # Update: Port und Passwort bleiben wie sie sind
  PORT=$(grep -oP 'STREAM24_PORT=\K[0-9]+' /etc/systemd/system/stream24.service || echo 8090)
  echo "==> Update – Port $PORT und Passwort bleiben erhalten"
else
  read -r -p "Port fürs Panel [8090]: " PORT
  PORT=${PORT:-8090}
  while true; do
    read -r -s -p "Passwort fürs Panel festlegen: " PW; echo
    [ -n "$PW" ] && break
  done
  STREAM24_DIR="$DIR" python3 "$DIR/server.py" --set-password "$PW"
fi
chown -R stream24:stream24 "$DIR"

echo "==> Dienst einrichten (startet automatisch, auch nach Server-Neustart) ..."
cat > /etc/systemd/system/stream24.service <<EOF
[Unit]
Description=xImJxstinn 24/7 Twitch Stream
After=network-online.target
Wants=network-online.target

[Service]
User=stream24
Environment=STREAM24_DIR=$DIR
Environment=STREAM24_PORT=$PORT
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

# Eigener Dienst für die Verbindung zu Twitch/YouTube – bleibt bei Panel-Updates online
cat > /etc/systemd/system/stream24-sender.service <<EOF
[Unit]
Description=xImJxstinn 24/7 Stream - Sender (Verbindung zu Twitch/YouTube)
After=network-online.target
Wants=network-online.target

[Service]
User=stream24
Environment=STREAM24_DIR=$DIR
ExecStart=/usr/bin/python3 $DIR/sender.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now stream24-sender stream24
systemctl restart stream24-sender stream24

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null && echo "==> Port $PORT in ufw freigegeben"
fi

IP=$(hostname -I | awk '{print $1}')
echo
echo "✅ Fertig! Panel öffnen:  http://$IP:$PORT"
echo "   (Falls eine Hetzner-Firewall aktiv ist: dort TCP-Port $PORT freigeben.)"
echo "   Status/Logs:  systemctl status stream24 stream24-sender   |   journalctl -u stream24 -f"
