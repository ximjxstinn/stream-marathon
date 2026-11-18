#!/usr/bin/env bash
# Panel unter eigener Domain mit HTTPS erreichbar machen, z. B. https://stream.ximjxstinn.de
# Aufruf (als root):  bash domain.sh stream.ximjxstinn.de
set -e
DOMAIN="${1:-stream.ximjxstinn.de}"

if [ "$EUID" -ne 0 ]; then echo "Bitte als root ausführen"; exit 1; fi
PORT=$(grep -oP 'STREAM24_PORT=\K[0-9]+' /etc/systemd/system/stream24.service 2>/dev/null || echo 8090)
MYIP=$(curl -4 -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

echo "==> Prüfe DNS: $DOMAIN -> $MYIP ?"
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}')
if [ "$DNSIP" != "$MYIP" ]; then
  echo "❌ $DOMAIN zeigt noch nicht auf diesen Server (DNS: ${DNSIP:-kein Eintrag}, Server: $MYIP)."
  echo "   Lege beim Domain-Anbieter einen A-Eintrag an:  stream  ->  $MYIP"
  echo "   Danach ein paar Minuten warten und das Skript nochmal starten."
  exit 1
fi
echo "   ✅ DNS passt"

setup_nginx() {
  echo "==> Richte nginx ein ..."
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  cat > /etc/nginx/sites-available/stream24 <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 0;
    proxy_request_buffering off;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
}
EOF
  ln -sf /etc/nginx/sites-available/stream24 /etc/nginx/sites-enabled/stream24
  nginx -t
  systemctl reload nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect
}

setup_caddy() {
  echo "==> Richte Caddy ein (HTTPS automatisch) ..."
  CF=/etc/caddy/Caddyfile
  touch "$CF"
  if ! grep -q "^$DOMAIN" "$CF"; then
    printf '\n%s {\n    reverse_proxy 127.0.0.1:%s {\n        transport http {\n            read_timeout 15m\n            write_timeout 15m\n        }\n    }\n}\n' "$DOMAIN" "$PORT" >> "$CF"
  fi
  caddy validate --config "$CF" --adapter caddyfile >/dev/null
  systemctl enable --now caddy >/dev/null 2>&1 || true
  systemctl reload caddy || systemctl restart caddy
}

if systemctl is-active --quiet caddy; then
  setup_caddy
elif systemctl is-active --quiet nginx; then
  setup_nginx
elif ss -ltn '( sport = :80 )' | grep -q LISTEN; then
  echo "❌ Port 80 wird von einem anderen Programm benutzt:"; ss -ltnp '( sport = :80 )'
  echo "   Schick Claude bitte diese Ausgabe."; exit 1
else
  echo "==> Kein Webserver gefunden – installiere Caddy ..."
  if apt-get install -y -qq caddy >/dev/null 2>&1; then
    setup_caddy
  else
    apt-get install -y -qq nginx >/dev/null
    setup_nginx
  fi
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

sleep 3
if curl -fsS -o /dev/null "https://$DOMAIN/" ; then
  echo; echo "✅ Fertig! Panel jetzt unter:  https://$DOMAIN"
else
  echo; echo "⚠ Eingerichtet, aber https://$DOMAIN antwortet noch nicht. In 1–2 Minuten nochmal im Browser probieren."
fi
