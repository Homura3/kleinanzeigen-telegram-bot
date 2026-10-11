# ebBot - Kleinanzeigen-Benachrichtigungen in Telegram

Ein kleiner Telegram-Bot, der Suchen auf [kleinanzeigen.de](https://www.kleinanzeigen.de) beobachtet und
dir schreibt, sobald eine neue Anzeige auftaucht. Auf Wunsch beobachtet er auch ebay.de. Er spricht Deutsch
und Englisch.

🇬🇧 [English instructions](README.md)

Der Bot besteht aus einer einzigen Python-Datei mit zwei Abhängigkeiten, speichert seine Daten in einer einzigen
SQLite-Datenbank und läuft problemlos auf einem Raspberry Pi, einem NAS oder jedem kleinen Server.

![ebBot in Telegram: die /about-Nachricht und Meldungen über neue Anzeigen](docs/telegram.webp)

<details>
<summary>Admin-Dashboard</summary>

![Das Admin-Dashboard von ebBot](docs/dashboard.webp)

</details>

## Was er kann

- **Meldungen bei neuen Anzeigen.** Leg eine Suche an und der Bot meldet jede Anzeige, die danach erscheint.
- **Drei Wege zu einer Suche:** schreib einfach, was du suchst, bau sie mit `/new` Schritt für Schritt
  (Suchbegriffe, Kategorie, Budget, Ort) oder schick einen Suchlink von der Seite mit all deinen Filtern.
- **Filter:** überspringe Anzeigen mit bestimmten Wörtern (`/exclude`) oder unter einem Preis (`/minprice`).
- **Mehrere Nutzer.** Freunde können denselben Bot verwenden. Standardmäßig muss der Admin jede neue Person
  erst freigeben.
- **Aufgeräumte Chats.** Meldungen werden nach 24 Stunden wieder gelöscht (einstellbar), und eine Anzeige,
  die zu zwei deiner Suchen passt, kommt nur einmal.
- **Admin-Dashboard.** Eine passwortgeschützte Webseite, um Leute freizugeben, Nutzer und Suchen anzusehen
  und die Einstellungen des Bots zu ändern. Sie lässt sich auf dem Handy zum Startbildschirm hinzufügen.
- **Sorgsam mit deinen Daten.** Alles liegt in einer Datenbankdatei, die nur der eigene Nutzer des Bots lesen
  kann, und jeden Tag wird eine Kopie beiseitegelegt. Das Dashboard-Passwort lässt sich als Hash hinterlegen,
  und ein Sicherheitsprotokoll zeigt Anmeldungen, Fehlversuche und Änderungen.
- **Passt auf sich selbst auf.** Schlagen Scans fehl oder blockiert die Seite den Bot, wartet er länger ab
  und warnt den Admin - und meldet sich, wenn es wieder läuft.
- **Deutsch und Englisch.** Jeder wählt mit `/language` seine eigene Sprache. Neue Nutzer starten in der
  Sprache ihrer Telegram-App, und das Dashboard hat einen eigenen Sprachumschalter.
- **Optional eBay.** Mit einem kostenlosen eBay-Entwicklerschlüssel beobachtet der Bot auch Suchen auf
  ebay.de, über die offizielle Browse-API von eBay.

### Befehle im Chat

| Befehl | Was er macht |
|---|---|
| `/new` | Suche Schritt für Schritt bauen |
| `/add` | Suche beobachten (oder einfach den Link schicken) |
| `/list` | Deine Suchen und Filter |
| `/show` | Zeigen, was in einer Suche gerade online ist |
| `/edit` | Eine deiner Suchen ändern |
| `/remove` | Suche nicht mehr beobachten |
| `/exclude` | Anzeigen mit bestimmten Wörtern überspringen |
| `/minprice` | Anzeigen unter einem Preis überspringen |
| `/pause`, `/resume` | Meldungen stumm schalten und wieder einschalten |
| `/language` | Zwischen Deutsch und Englisch wechseln (`/deutsch`, `/english`) |
| `/about`, `/help` | Wie der Bot arbeitet, und Hilfe zu jedem Befehl |

Deutsche Schreibweisen wie `/hilfe`, `/liste` und `/sprache` funktionieren ebenfalls, und Antworten werden
in beiden Sprachen verstanden (`ja` oder `yes`, `egal` oder `skip`).

## Einrichtung

Du brauchst zwei Dinge von Telegram:

1. **Ein Bot-Token.** Schreib [@BotFather](https://t.me/BotFather), schick `/newbot` und folge den Schritten.
2. **Deine eigene Nutzer-ID.** Schreib [@userinfobot](https://t.me/userinfobot); er antwortet mit deiner ID.
   Dieses Konto wird der Admin des Bots.

### Mit Docker (empfohlen)

```sh
git clone https://github.com/Homura3/kleinanzeigen-telegram-bot.git
cd kleinanzeigen-telegram-bot
cp .env.example .env      # dann TOKEN, CHAT_ID und ADMIN_PASSWORD eintragen
docker compose up -d --build
```

Die Daten des Bots liegen im Docker-Volume `ebbot-data`. Das Dashboard erreichst du unter
`http://<dein-server>:8080`. Der Container läuft als unprivilegierter Nutzer auf einem schreibgeschützten
Dateisystem.

### Ohne Docker

Der Bot wird mit Python 3.11 entwickelt und betrieben.

```sh
pip install -r requirements.txt
export TOKEN=...            # von @BotFather
export CHAT_ID=...          # deine Telegram-Nutzer-ID
export ADMIN_PASSWORD=...   # optional, schaltet das Dashboard ein
export LANGUAGE=de          # optional, Deutsch als Ausweichsprache
python3 bot.py
```

Öffne danach deinen Bot in Telegram und drück auf **Start**.

## Einstellungen

Alle Einstellungen sind Umgebungsvariablen. Nur `TOKEN` ist Pflicht; setz auch `CHAT_ID`, damit der Bot
einen Admin hat.

| Variable | Standard | Bedeutung |
|---|---|---|
| `TOKEN` | - | Bot-Token von @BotFather |
| `CHAT_ID` | - | Telegram-Nutzer-ID des Admins |
| `ADMIN_PASSWORD` | aus | Passwort für das Dashboard (mindestens 10 Zeichen). Ohne Passwort ist das Dashboard aus |
| `ADMIN_PASSWORD_HASH` | - | Dasselbe Passwort als Hash, damit es nicht in deinen Einstellungen lesbar ist. Siehe [Sicherheit](#sicherheit) |
| `ALLOWED_IDS` | - | Nutzer-IDs, die den Bot ohne Freigabe verwenden dürfen, durch Kommas getrennt |
| `APPROVAL` | `1` | Neue Leute brauchen die Freigabe des Admins. `0` öffnet die Anmeldung für alle |
| `INTERVAL` | `600` | Sekunden zwischen den Kleinanzeigen-Scans |
| `MAX_LINKS` | `5` | Suchen pro Nutzer (pro Seite) |
| `MAX_USERS` | `50` | Maximale Nutzerzahl |
| `MAX_PER_CYCLE` | `10` | Höchstens so viele Meldungen pro Suche in einem Scan |
| `ALERT_HOURS` | `24` | Stunden, bis Meldungen aus den Chats gelöscht werden. `0` behält sie; Telegram erlaubt höchstens 48 |
| `LANGUAGE` | `en` | Ausweichsprache, `en` oder `de`: gilt für Leute, deren Telegram-App keine der beiden Sprachen hat, und für alle, die noch keine gewählt haben |
| `DATA_DIR` | `~/ebbot` | Ordner für die Datenbank `bot.db` und ihre täglichen Kopien (`/data` im Docker-Image) |
| `BACKUP_DAYS` | `7` | Wie viele tägliche Kopien der Datenbank in `backups/` aufbewahrt werden. `0` legt keine an |
| `DASHBOARD_PORT` | `8080` | Port des Dashboards |
| `EBAY_APP_ID`, `EBAY_CERT_ID` | - | Schlüssel von [developer.ebay.com](https://developer.ebay.com) |
| `EBAY_INTERVAL` | `300` | Sekunden zwischen den eBay-Scans |

eBay schaltet sich ein, sobald `EBAY_APP_ID` und `EBAY_CERT_ID` beide gesetzt sind. Einige dieser
Werte lassen sich später auch im Dashboard ändern.

## Sicherheit

- **Eine private Datenbank.** Nutzer, Suchen, das Meldungsprotokoll, Einstellungen und Dashboard-Sitzungen
  liegen in `bot.db` im `DATA_DIR`. Der Bot legt sie so an, dass nur sein eigener Nutzer sie lesen und
  schreiben kann; dasselbe gilt für die täglichen Kopien in `backups/`.
- **Gehashtes Dashboard-Passwort.** Statt `ADMIN_PASSWORD` kannst du `ADMIN_PASSWORD_HASH` setzen. Erzeuge
  den Hash mit dem Bot selbst und füg die ausgegebene Zeile in deine `.env` ein:

  ```sh
  docker compose run --rm ebbot python3 bot.py hash-password    # oder: python3 bot.py hash-password
  ```

  Sind beide gesetzt, zählt der Hash.
- **Sicherheitsprotokoll.** Das Dashboard listet die letzten Anmeldungen, falschen Passwörter, Sperren und
  Änderungen auf und hat einen Button, der alle Geräte abmeldet. Acht falsche Passwörter sperren eine
  Adresse für zehn Minuten.
- **Sitzungen.** Von jedem Sitzungstoken wird nur ein Hash gespeichert, mit der Datenbank allein kann sich
  also niemand anmelden.
- **Umstieg von einer älteren Version?** Der erste Start übernimmt `bot.json`, `ebay.json` und
  `categories.json` in die Datenbank und benennt sie in `*.imported` um. `EBAY_DATA_DIR` wird nur noch für
  diese Übernahme gelesen. Mit Docker liegen die Daten jetzt in einem Volume statt in `./data`: kopier deine
  alten Dateien aus `./data` vor dem ersten Start in das Volume, wenn du sie behalten willst.

## Gut zu wissen

- **Halte das Dashboard privat.** Es spricht unverschlüsseltes HTTP. Greif über dein Heimnetz oder ein VPN
  darauf zu, oder stell es hinter einen Reverse-Proxy mit HTTPS, bevor du es ins Internet stellst.
- **Geh behutsam vor.** Der Bot liest die öffentlichen Suchseiten von Kleinanzeigen. Halte den Scan-Abstand
  moderat; bei sehr kurzen Abständen wird der Bot blockiert.
- **Keine Verbindung zu den Anbietern.** Dies ist ein privates Hobbyprojekt. Es steht in keiner Verbindung zu
  Kleinanzeigen, eBay oder Telegram und wird von ihnen nicht unterstützt; automatisierte Zugriffe können gegen
  die Nutzungsbedingungen einer Seite verstoßen. Nutzung auf eigene Gefahr.

## Mitmachen

Fehlerberichte, Ideen und Pull Requests sind willkommen. Eröffne ein [Issue](https://github.com/Homura3/kleinanzeigen-telegram-bot/issues).
Wenn dir der Bot nützt, hilft ein ⭐ anderen, ihn zu finden.

## Lizenz

[MIT](LICENSE)
