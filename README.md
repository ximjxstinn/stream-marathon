# Stream Marathon

Subathon-Timer für OBS von **xImJxstinn**. Follows, Subs, Gift-Subs, Bits und Raids verlängern den Timer automatisch.

## Ordner

- `app/` – die Update-Dateien. Die App lädt sie automatisch über `app/manifest.json`. **Nur über `release.js` erzeugen**, sonst stimmen die Prüfsummen nicht.
- `src/` – der Quellcode.

## Neues Update veröffentlichen

1. Änderungen in `src/` machen
2. In `src/package.json` die Version erhöhen (z. B. `1.2.0` → `1.2.1`)
3. Eintrag in `src/changelog.json` ergänzen
4. `node src/release.js app`
5. Committen und pushen

Alle Apps holen sich das Update automatisch, sobald GitHub es verteilt hat (bis zu 5 Minuten). Ein laufender Timer läuft dabei weiter.

**Beta-Kanal:** `node src/release.js beta` – nur Apps mit Update-Kanal „Beta“ (Admin-Bereich) bekommen es.

**Stream-Deck-Plugin:** Quelle in `src/deckplugin/`. Nach Änderungen neu zippen nach `src/ui/StreamMarathon.streamDeckPlugin`.

`src/main.js` (der Starter) ist nicht Teil der Updates. Änderungen daran brauchen eine neue Windows-ZIP.
