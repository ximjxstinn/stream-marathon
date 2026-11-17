# 📺 xImJxstinn 24/7 Stream

Der Hetzner-Server streamt rund um die Uhr zu Twitch: entweder deine Videos in Dauerschleife oder einen Live-Countdown (z. B. bis GTA 6).
Steuern kannst du alles übers Handy-Panel: Videos hochladen, Starten und Stoppen, den Stream-Key eintragen.
**Dein PC muss dafür nicht laufen.**

## So funktioniert's
- Du lädst Videos (Clips, alte VODs, Highlights) übers Panel hoch.
- Der Server wandelt jedes Video **einmal** in ein einheitliches Format um. Danach läuft der Stream mit `-c copy`, also fast ohne CPU-Last. Ein kleiner Hetzner-Server reicht dafür.
- Die Videos laufen ohne Pause hintereinander, auf Wunsch gemischt, und fangen danach wieder von vorn an.
- Twitch trennt jeden Stream nach 48 Stunden. Fällt die Verbindung weg, verbindet sich der Server nach 5 Sekunden automatisch neu.
- Wird der Server neu gestartet und der Stream war an, geht er von selbst wieder an.

## Installation (einmalig, ca. 5 Minuten)
1. Den Ordner `stream24` auf den Server kopieren. Das geht z. B. mit WinSCP oder in der Windows-Eingabeaufforderung:
   ```
   scp -r stream24 root@DEINE-SERVER-IP:/root/
   ```
2. Per SSH auf den Server gehen und den Installer starten:
   ```
   ssh root@DEINE-SERVER-IP
   cd /root/stream24
   bash install.sh
   ```
   Der Installer fragt nach einem Port (Enter = 8090) und nach einem Passwort fürs Panel.
3. Wenn du in der **Hetzner Cloud Console** eine Firewall aktiv hast, gibst du dort den TCP-Port **8090** frei.
4. Öffne `http://DEINE-SERVER-IP:8090` auf dem iPhone und logge dich ein. Mit „Teilen → Zum Home-Bildschirm“ bekommst du es wie eine App.

## Einrichten im Panel
1. **Stream-Key** eintragen: Twitch → Creator-Dashboard → Einstellungen → Stream → Primärer Stream-Key kopieren.
2. **Qualität** wählen, *bevor* du Videos hochlädst. Empfehlung: `720p30` (3000 kbit/s), das sieht gut aus und läuft stabil.
3. **Videos hochladen.** Warte, bis bei allen „fertig“ steht.
4. **▶ Starten** drücken. Nach ein paar Sekunden bist du auf Twitch live.


## ⏱ Countdown-Modus (z. B. GTA 6 Live-Timer)
Hier streamt der Server einen live mitlaufenden Timer statt Videos. Videos brauchst du dafür keine.
1. Im Panel unter **Modus** auf **⏱ Countdown** tippen. Das ist schon voreingestellt.
2. Design wählen (🎮 Konsole oder Standard), Überschrift, Zieldatum (deutsche Zeit) und den Text für „Zeit abgelaufen“ eintragen. Voreingestellt sind *GTA 6 COUNTDOWN*, *19.11.2026 00:00* und *GTA 6 IST DA!*.
3. Mit **👁 Vorschau** siehst du direkt auf dem Handy, wie das Bild auf Twitch aussieht.
4. Optional ein **Hintergrundbild** hochladen. Es wird automatisch abgedunkelt, damit der Timer gut lesbar bleibt. Dazu kannst du **Hintergrundmusik** hochladen, die in Dauerschleife läuft. Nimm dafür nur lizenzfreie Musik!
5. **▶ Starten** drücken.

Nach Ablauf zeigt der Stream den Abschlusstext an und läuft weiter, bis du ihn stoppst.
Im Countdown-Modus rechnet der Server live. Bei 720p30 schafft das ein kleiner Hetzner-Server (z. B. CX22) ohne Probleme.

## Twitch + YouTube gleichzeitig
Der Server erzeugt das Bild einmal und schickt es an alle eingeschalteten Plattformen. Das kostet kaum zusätzliche Leistung.
- Im Panel unter **Einstellungen → 🔴 YouTube** „An YouTube senden“ anhaken, den Stream-Schlüssel eintragen und speichern.
- Den Schlüssel findest du in YouTube Studio → Erstellen → Livestream starten → Stream. Nimm am besten einen dauerhaften Schlüssel.
- Jede Plattform hat einen eigenen Wächter: Fällt YouTube aus, läuft Twitch weiter, und andersherum genauso.

## Ausfälle & Auto-Reconnect
- **Internet weg / Twitch hängt:** Ein Wächter prüft jede Sekunde, ob wirklich Bilder rausgehen. Kommt 15 Sekunden lang nichts an, verbindet er neu: zuerst nach 2 Sekunden, bei Dauer-Störung mit wachsenden Abständen bis höchstens 30 Sekunden.
- **Server-Neustart:** Der Dienst startet automatisch mit und macht weiter, wenn der Stream vorher an war.
- **„Neu verbinden …“:** Muss der Countdown neu starten (Design, Farbe, Qualität), sehen die Zuschauer ein paar Sekunden ein „Neu verbinden …“-Bild. Die Verbindung zu Twitch und YouTube bleibt dabei bestehen.
- **Live-Änderungen:** Texte, Datum, Hintergrundbild, Songs und Lautstärke werden übernommen, ohne dass der Stream neu startet. Ein neuer Stream-Key verbindet nur die betroffene Plattform neu. Musik darf beliebig lang sein, auch 10-Stunden-Mixe gehen.

## Gut zu wissen
- **Kennzeichnung:** Markiere den Stream als Wiederholung, z. B. mit „🔁 24/7 Rerun“ im Titel. So denkt niemand, du sitzt gerade live da.
- **Traffic:** Bei 3000 kbit/s rund um die Uhr kommst du auf ungefähr 1 TB im Monat. Bei Hetzner sind 20 TB inklusive.
- **Speicherplatz** siehst du im Panel. 1 Stunde 720p30 braucht ungefähr 1,4 GB.
- **Ton:** Videos ohne Ton bekommen automatisch eine stille Tonspur.
- **Qualität ändern:** Das geht nur, wenn keine Videos vorhanden sind, weil alle Videos dasselbe Format haben müssen.

## Befehle auf dem Server
```
systemctl status stream24      # läuft der Dienst?
systemctl restart stream24     # Panel neu starten
journalctl -u stream24 -f      # Live-Logs
bash install.sh                # erneut ausführen = Update / neues Passwort
```

## Optional: eigene Domain mit HTTPS (z. B. stream.ximjxstinn.de)
Wenn auf dem Server schon nginx läuft (wie für `afk.ximjxstinn.de`):
```nginx
server {
    server_name stream.ximjxstinn.de;
    client_max_body_size 0;          # große Video-Uploads erlauben
    proxy_request_buffering off;
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_read_timeout 3600s;
    }
}
```
Danach `certbot --nginx -d stream.ximjxstinn.de` für HTTPS. Den Port 8090 musst du dann nicht mehr in der Firewall freigeben.
