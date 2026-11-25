#!/usr/bin/env bash
# Einmalig: mehrere Kanäle/Streams erlauben (startet nichts neu, der laufende Stream bleibt online)
set -e
if [ "$EUID" -ne 0 ]; then echo "Bitte als root ausführen:  sudo bash profiles.sh"; exit 1; fi
DIR=/opt/stream24
SC=$(command -v systemctl)

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

# Das Panel darf (nur) diese Kanal-Dienste an- und ausschalten
cat > /etc/sudoers.d/stream24 <<EOF
stream24 ALL=(root) NOPASSWD: $SC enable --now stream24-sender@* stream24@*, $SC disable --now stream24@* stream24-sender@*
EOF
chmod 440 /etc/sudoers.d/stream24
visudo -cf /etc/sudoers.d/stream24 >/dev/null

mkdir -p "$DIR/profiles"
chown stream24:stream24 "$DIR/profiles"
systemctl daemon-reload
echo "✅ Fertig – im Panel kannst du jetzt oben bei „Kanal“ mit „＋ Neu“ weitere Streams anlegen."
