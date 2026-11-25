#!/usr/bin/env bash
# Einmalig: mehrere Kanäle/Streams erlauben (startet nichts neu, der laufende Stream bleibt online)
set -e
if [ "$EUID" -ne 0 ]; then echo "Bitte als root ausführen:  sudo bash profiles.sh"; exit 1; fi
DIR=/opt/stream24

cat > /etc/systemd/system/stream24@.service <<EOF
[Unit]
Description=xImJxstinn 24/7 Stream - weiterer Kanal %i
After=network-online.target
Wants=network-online.target

[Service]
User=stream24
Environment=STREAM24_DIR=$DIR/profiles/%i
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/stream24-sender@.service <<EOF
[Unit]
Description=xImJxstinn 24/7 Stream - Sender für Kanal %i
After=network-online.target
Wants=network-online.target

[Service]
User=stream24
Environment=STREAM24_DIR=$DIR/profiles/%i
ExecStart=/usr/bin/python3 $DIR/sender.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# Kleines Hilfsprogramm: darf nur Kanal-Dienste an-/ausschalten (sonst nichts)
cat > /usr/local/sbin/stream24-profile <<'HELPER'
#!/bin/sh
set -e
case "$1" in enable|disable) ;; *) echo "nur enable/disable"; exit 2;; esac
echo "$2" | grep -Eq '^[a-z0-9-]{1,40}$' || { echo "ungültiger Name"; exit 2; }
if [ "$1" = enable ]; then
  systemctl enable --now "stream24-sender@$2" "stream24@$2"
else
  systemctl disable --now "stream24@$2" "stream24-sender@$2"
fi
HELPER
chmod 755 /usr/local/sbin/stream24-profile
chown root:root /usr/local/sbin/stream24-profile

# Sudo-Regel: das Panel darf genau dieses Hilfsprogramm ausführen
TMP=$(mktemp)
echo "stream24 ALL=(root) NOPASSWD: /usr/local/sbin/stream24-profile" > "$TMP"
if visudo -cf "$TMP" >/dev/null; then
  install -m 440 -o root -g root "$TMP" /etc/sudoers.d/stream24
else
  echo "⚠ Sudo-Regel ungültig – nichts geändert"; rm -f "$TMP"; exit 1
fi
rm -f "$TMP"
if ! visudo -c >/dev/null 2>&1; then
  echo "⚠ In einer anderen Sudo-Datei ist ein Fehler – Ausgabe:"; visudo -c || true
fi

mkdir -p "$DIR/profiles"
chown stream24:stream24 "$DIR/profiles"
systemctl daemon-reload
if sudo -u stream24 sudo -n /usr/local/sbin/stream24-profile check x 2>&1 | grep -q "nur enable/disable"; then
  echo "✅ Fertig – im Panel kannst du jetzt oben bei „Kanal“ mit „＋ Neu“ weitere Streams anlegen."
else
  echo "⚠ Test fehlgeschlagen:"; sudo -u stream24 sudo -n /usr/local/sbin/stream24-profile check x || true
fi
