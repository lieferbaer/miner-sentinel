# MinerSentinel – Übergabe an Claude Code

Stand: 20.10.2026, Branch `vps-stand` (Commit `1d4aff3`), deployt auf dem VPS lb-db.de.

Dieses Dokument enthält alles, was bisher in der Cowork-Session „MinerSentinel Docker setup“ gebaut,
entschieden und gelernt wurde – mit Gründen. Lies es komplett, bevor du etwas änderst. Wenn etwas
unklar ist oder diesem Dokument widerspricht: **frag** (siehe Abschnitt 14), statt zu raten.

---

## 0. Inhalt

1. Arbeitsregeln (vom Nutzer, verbindlich)
2. Projekt und Ziel
3. Infrastruktur: iMac, GitHub, VPS
4. Container, Dienste, Datenfluss
5. Deploy, Tests, Rollback
6. Die MinerGeräte und ihre Schnittstellen (gemessene Fakten)
7. Eigene Funktionen im Detail (mit Gründen)
   7.1 Datenerfassung / Avalon-Collector · 7.2 Übersicht · 7.3 Drosselung ·
   7.4 Takt-Tabellen (A15) · 7.5 Over-Hot · 7.6 Lüfter (AC Infinity) · 7.7 Pools/f2pool
8. Integration des Originals 1.2.0 (Entscheidungen)
9. Datenbank und Migrationen
10. Fehlerhistorie und Lehren (wichtig – nicht wiederholen)
11. Offene Punkte / nächste Schritte
12. Dateikarte
13. Schnellreferenz Befehle
14. Rückfragen an die Cowork-Session

---

## 1. Arbeitsregeln (verbindlich)

Diese Regeln hat der Nutzer (Onur) ausdrücklich festgelegt. Verstöße haben in der Vergangenheit zu
Ärger geführt.

| Regel | Grund / Hintergrund |
|---|---|
| Antworten **auf Deutsch, sehr kurz**, ohne Einleitung, ohne Zusammenfassung am Ende, ohne Füllsätze. | Ausdrücklicher Wunsch. |
| Begriff **„MinerGerät“** statt „Worker“ verwenden. | Ausdrücklicher Wunsch. |
| **Vor größeren Änderungen** erst den Plan kurz und verständlich erklären, dann auf Freigabe warten, erst danach umsetzen. | Mehrfach eingefordert. Kleine, klar beauftragte Fixes dürfen direkt erfolgen. |
| **Erst prüfen, dann behaupten.** Keine Vermutung als Tatsache formulieren. Zahlen aus echten Daten (DB, Logs, API) belegen. | Falsche Aussagen („GHSspd ~1 min“, „sparsamster Modus“) haben Vertrauen gekostet. |
| **Keine ungefragten Extras**, keine Alternativ-Listen, keine irrelevanten Infos. | Ausdrücklicher Wunsch. |
| **Takt-Messungen (Max-Messung, Messreihe) startet nur der Nutzer selbst.** Nie selbst starten. | Ausdrückliche Anweisung nach einem Vorfall. Abbrechen/Pausieren nur auf Anweisung. |
| Auf dem VPS lb-db.de **keine anderen Webseiten, keine JavaScript-Webseite und keine davon abhängigen Konfigurationsdateien anfassen.** Nur `/opt/minersentinel` und die eigenen Container. | Auf dem Server laufen produktive Seiten von LieferBär. Reverse-Proxy-/Webserver-Konfiguration für miners.lb-db.de ebenfalls nicht ändern. |
| **Keine Passwörter in Formulare eintippen**, keine Zugangsdaten anlegen/ändern. Zugangsdaten trägt der Nutzer selbst ein. | Sicherheitsregel. Für die A15-Modusumschaltung wird deshalb der **unauthentifizierte** POST an `cgconf.cgi` genutzt (kein Login). |
| Selbst geschriebene Tests/Hilfsskripte auf dem Server nach Gebrauch löschen (nicht in `/opt/minersentinel/backend` liegen lassen). | Ist einmal passiert (`t41.py`). Das Backend-Verzeichnis ist in den Container gemountet und versioniert. |
| Git: commit/push nur auf Anweisung. Der Nutzer pusht selbst über **GitHub Desktop** (auf dem iMac ist keine Git-Anmeldung für die CLI hinterlegt). | Push per CLI scheitert mit „could not read Username“. |
| Wenn du etwas am Laufen änderst (Datendienst-Neustart usw.), danach **Logs und Funktion prüfen** und kurz berichten. | Mehrfach hat erst die Kontrolle Fehler gezeigt. |

---

## 2. Projekt und Ziel

- **MinerSentinel** ist ein Monitoring-Dashboard für Bitcoin-Miner (Original: `github.com/dcbert/miner-sentinel`,
  Version 1.2.0 vom 03.10.2026). Onur betreibt einen **eigenen Fork**: `github.com/lieferbaer/miner-sentinel`.
- Erreichbar unter **https://miners.lb-db.de** (Login über MinerSentinel-Benutzer; Zugangsdaten beim Nutzer erfragen,
  nicht ins Repo schreiben).
- Überwacht und steuert Onurs Avalon-Miner (siehe Abschnitt 6) und den Lüfter-Controller AC Infinity 69 Pro.
- Langfristige Ziele des Nutzers (Kontext, nicht beauftragt):
  - bis zu 10× Avalon A15 zuhause (~40 kW), leiser und sparsamer betreiben,
  - Lüfter (AC Infinity) per Automation an die Miner koppeln,
  - Drosselung bekommt später eine **komplett neue Logik** (Werte der jetzigen Drosselung sind unwichtig),
  - Over-Hot-Benachrichtigung per Messenger (später),
  - ferner Zukunftsplan: entfernter Mining-Standort mit Raspberry Pi + LTE (nur Notiz, nichts tun).

---

## 3. Infrastruktur

### 3.1 iMac (Entwicklung)
- Repo: `/Users/onurtural/Documents/GitHub/miner-sentinel`
- Remotes: `origin` = `https://github.com/lieferbaer/miner-sentinel.git` (Fork), `upstream` = `https://github.com/dcbert/miner-sentinel.git`.
- Branches:
  - `main` = Original 1.2.0 (sauber, unverändert),
  - **`vps-stand`** = aktueller Arbeitsstand (Original 1.2.0 + alle eigenen Änderungen) – **hier weiterarbeiten**,
  - `imac-alt-sep` = geparkter alter iMac-Stand vom 09.09. (enthält bewusst ungelöste Konfliktmarker; nur Archiv, nicht mergen),
  - `claude` = alter Branch, nicht verwendet.
- Der alte Ordner `~/miner-sentinel` (MinerSentinel per Docker auf dem iMac) ist **gestoppt** und veraltet.
- Lokal gibt es kein laufendes MinerSentinel. Getestet wird mit Unit-Tests (Abschnitt 5.3) und am VPS.

### 3.2 VPS lb-db.de (Produktion)
- AlmaLinux 9.8, Host `server4-de-lfrbr.lieferbaer.de`, verwaltet mit **CloudPanel**.
- Zugang vom iMac: **`ssh lb-db`** (Eintrag in `~/.ssh/config`: User root, Port 22, Key-Login).
- Projektordner: **`/opt/minersentinel`** – ein Git-Checkout:
  - Branch `vps-stand`, Remote **`fork`** = lieferbaer-Fork, Remote `origin` = dcbert (alt, nicht benutzen).
  - Ungetrackt/ignoriert und **niemals überschreiben/committen**: `.env` (DB-Zugang, Django-Secrets),
    `backend/acinfinity_data/` (AC-Infinity-Zugangsdaten, Token, Sicherungen), `docker-compose.yml.imac` (Alt-Datei).
- Container-Laufzeit: **Podman 5.8.2 (rootful)** + **podman-compose 1.5.0** (nicht Docker).
- Besonderheit des Hosts: Die Jailshell spiegelt Container-Overlay-Mounts unter `/home/virtfs/*`.
  Dadurch schlagen `podman rm` („device or resource busy“) und `podman build` manchmal fehl.
  **Darum gibt es die Skripte** `ms-recreate.sh` und `ms-build-frontend.sh`, die diese Spiegel vorher/während des Builds aushängen.
  Nicht durch eigene Varianten ersetzen.
- Andere Seiten auf diesem Server: **tabu** (siehe Regeln).

---

## 4. Container, Dienste, Datenfluss

`docker-compose.yml` (vom VPS übernommen, an Podman angepasst: Images voll qualifiziert `docker.io/library/...`):

| Container | Inhalt | Code-Mount | Änderungen wirksam durch |
|---|---|---|---|
| `minersentinel-postgres` | Postgres 17 (Volume `postgres_data`), DB/User `minersentinel` | – | – (bei Deploys **nicht** neu erstellen) |
| `minersentinel-backend` | Django 5.2 + DRF, `runserver 0.0.0.0:8000`, beim Start `migrate` + `createsuperuser` | `./backend:/app` | **Autoreload** (Datei speichern genügt). Bei neuen Python-Paketen: Image neu bauen + Container neu erstellen |
| `minersentinel-data-service` | Flask + APScheduler: Collector, Drosselung, Takt-Tabellen | `./data-service:/app` | **`podman restart -t 5 minersentinel-data-service`** (kein Autoreload) |
| `minersentinel-web` | nginx mit gebautem Vite-Frontend, `127.0.0.1:3080:80` | – (im Image) | **`./ms-build-frontend.sh`** (baut Image, erstellt Container neu) |

- Netzwerk: `minersentinel_minersentinel-network` (Alias `postgres` für die DB).
- Backend-Container läuft als `appuser` (uid 1000) und **darf `/app` nicht beschreiben** (Dateien gehören root).
  `makemigrations` deshalb mit `podman exec -u 0 …` ausführen und danach `chown root:root` auf die neue Datei.
  Ausnahme: `backend/acinfinity_data/` gehört uid 1000 (Modus 700), damit das Backend dort schreiben kann.
- `.env` setzt u. a. `POLLING_INTERVAL_SECONDS=10` → der Collector läuft alle **10 s** (Override statt Minuten aus den Settings).

**Scheduler-Jobs im Data-Service** (`data-service/app.py`):

| Job-ID | Intervall | Aufgabe |
|---|---|---|
| `poll_all_sources` | 10 s (env) | Alle Geräte abfragen (Bitaxe, Avalon, NMAxe, NerdNOS) + Pool-Statistiken |
| `check_devices` | Minuten aus Settings | Geräte-Check |
| `network_data` | 30 min | Netzwerkdaten |
| `throttle_controller` | 15 s | Drosselung (Abschnitt 7.3) |
| `calibration` | 15 s | Takt-Tabellen-Läufe (Abschnitt 7.4) |
| `check_settings` | 2 min | Settings neu laden |
| `prune_retention` | 24 h | Original: alte Daten löschen |

Achtung: `throttle_controller` und `calibration` feuern **im selben Moment**, und `poll_all_sources` alle 10 s
dazwischen → mehrere gleichzeitige Anfragen an den A15. Siehe Abschnitt 10 (Verbindungsabbrüche).

---

## 5. Deploy, Tests, Rollback

### 5.1 Arbeitsablauf (vereinbart)
1. Auf dem iMac im Branch `vps-stand` ändern, testen (5.3).
2. Nutzer committet/pusht (oder Claude committet auf Anweisung, Nutzer pusht über GitHub Desktop).
3. Auf dem VPS: `cd /opt/minersentinel && git fetch fork vps-stand && git checkout -f vps-stand && git reset --hard fork/vps-stand`
   – **vorher prüfen**, dass `git status` auf dem VPS keine ungesicherten Server-Änderungen zeigt.
4. Je nach geänderten Teilen:
   - nur `backend/` (Python ohne neue Pakete): nichts weiter, Autoreload. Logs prüfen.
   - `backend/requirements.txt` oder `backend/Dockerfile`: `podman build -t localhost/minersentinel_backend:latest backend` (mit virtfs-Aufräumschleife wie in `ms-build-frontend.sh`), dann `./ms-recreate.sh backend minersentinel-backend`.
   - Migrationen: Der Backend-Container führt beim Start `migrate` aus. Bei laufendem Container: `podman exec minersentinel-backend python manage.py migrate`.
   - `data-service/`: `podman restart -t 5 minersentinel-data-service` (bei Paket-/Dockerfile-Änderung Image bauen + `./ms-recreate.sh data-service minersentinel-data-service`).
   - `frontend/`: `rm -f /root/fe-build7.status; (nohup ./ms-build-frontend.sh > /root/fe-build7.status 2>&1 &)` → fertig, wenn die Status-Datei `BUILT` + `FIN` enthält (`build try N failed` kommt vor, das Skript wiederholt 3×). Log: `/root/fe-build7.log`.
5. Prüfen: `podman logs --since 2m minersentinel-data-service | grep -iE "error|traceback"`, Backend-Log auf 500er, Seite im Browser.
   Fehler „Host is unreachable“ für `192.168.178.137` (Nano 3S) sind bekannt/harmlos, wenn das Gerät aus ist.

### 5.2 Sicherungen / Rollback
- Vor jedem größeren Deploy: `tar czf` von `/opt/minersentinel` + `podman exec minersentinel-postgres pg_dump -U minersentinel -d minersentinel -Fc > db.dump`.
- Letzte Vollsicherung vor dem 1.2.0-Deploy: **`/root/ms-deploy-bak-20261020-2213/`** (Ordner-Tarball, `db.dump`, alte `0015_calibration.py`).
- Alte Images vor dem Deploy: `localhost/minersentinel_{backend,data-service,frontend}:pre-deploy`.
- Ältere Einzel-Patches/Sicherungen aus der Cowork-Zeit: `/root/thr6/bak21 … bak42` (je die geänderten Dateien vor dem Patch).

### 5.3 Tests (wie in der CI `.github/workflows/ci.yml`)
- Backend: `PYTHONPATH=backend DJANGO_SETTINGS_MODULE=minersentinel.settings_test python -m pytest backend/api/tests -c pyproject.toml --ds=minersentinel.settings_test`
  (SQLite, Migrationen deaktiviert → prüft die Migrationskette **nicht**).
- Data-Service: `PYTHONPATH=data-service python -m pytest data-service/tests -c pyproject.toml -m "not integration"`
- Frontend: `cd frontend && npm ci && npm run lint && npm run test:run && npm run build`
- Stand bei Übergabe: Backend 149 ✓, Data-Service 84 ✓ (5 übersprungen), Frontend 144 ✓, Lint 0 Fehler (15 Warnungen aus dem Original), Build ✓.

---

## 6. Die MinerGeräte (gemessene Fakten)

| DB-id | Name | device_id | IP:Port | Familie | Modi |
|---|---|---|---|---|---|
| 1 | Avalon Nano 3S | `nano3s-1` | 192.168.178.137:4028 | `nano` | WORKMODE 0/1/2 = Low/Medium/High |
| 2 | Avalon Q | `avalonq-1` | 192.168.178.138:4028 | `q` | WORKMODE 0/1/2 = Eco/Standard/Super |
| 3 | Avalon A15 | `A15` | 192.168.178.149:4028 | `a15` | WORKMODE 2 = **Power Mode** mit Stufe 00–04, WORKMODE 1 = **High Performance** |

- Die Geräte stehen bei Onur zuhause (Heimnetz FRITZ!Box, öffentliche IP wechselt ca. alle 2 Tage). Der VPS erreicht das Heimnetz `192.168.178.0/24` über den **WireGuard-Tunnel `wg0`** (VPS-Seite `10.66.66.1`) – **nicht ändern**. Nutzerwunsch: nicht der gesamte Heimverkehr soll über den VPS laufen.
- Pool: f2pool, Konto `onr`, MinerGerät-Namen z. B. `onr.A15`.
- Nano 3S und Avalon Q: **„bekommen keine Messwerte, damit haben wir nichts zu tun“** (Nutzer). Keine Takt-Tabellen, keine Experimente an ihnen.

### 6.1 cgminer-API (TCP 4028)
- Befehle: `{"command":"estats"}`, `summary`, `pools`, `version`; Schreiben: `ascset` z. B. `0,frequency,<p1:p2:p3>-0-0`, Neustart `ascset|0,reboot,0`, Workmode Nano/Q `0,workmode,set,<n>`.
- Wichtige `estats`-Felder (im String `MM ID0`, Format `KEY[wert]`):
  - `SF0[a b c]` = Takt je Board (MHz) → „Punkte“; `Freq` = Mittel.
  - `GHSspd` = vom Miner gemessene Hashrate. **Aktualisiert sich nur ca. alle 130 s** (aus DB gemessen) und mittelt über diese Zeit.
  - `GHSmm` = rechnerische Hashrate (A15: GHSmm ≈ 481,5 × Freq). `GHSspd ≈ GHSmm × (1 − DH)`.
    GHSmm/Freq aktualisieren sich im selben Takt wie GHSspd – **kein schnelleres Signal** (getestet, Idee verworfen).
  - `DH` = Fehlerrate in % (typisch 3–9 %, nach Neustart steigend).
  - `WORKMODE`, `WALLPOWER` (A15: gemessene Eingangsleistung), `PS[...]` Index 6 = gemessene Netzteil-Leistung (Nano 3S/Q/A15).
  - `MPO` = konfiguriertes Limit, **keine Messung** → wird bewusst nicht als Leistung verwendet.
  - `TMax`, **`MTmax[a b c]`** = heißester Chip je Hashboard (A15). Nach Neustart kurz **`-273`** = „kein Sensorwert“ → wird verworfen (Anzeige „—“).
  - `SYSTEMSTATU` z. B. `Work: In Work, Hash Board: 3` bzw. mit **`Over Hot`** (Boards abgeschaltet).
- Die Firmware regelt Takte innerhalb eines Modus selbst nach. Hashrate ist **nicht proportional** zum Takt-Mittel.
- Drosseln nur über Takt spart wenig Strom (A15 in 04: 130 TH = 2237 W, 100 TH = 1926 W, 70 TH = 1789 W).

### 6.2 A15-Webinterface (Port 80)
- **`GET http://192.168.178.149/updatecgconf.cgi`** (ohne Login) liefert `CGConfCallback({...})` mit
  `mode` (2 = Power Mode, 1 = High Performance), `power_level` (0–4) und Pool-Feldern (`pool1..3`, `worker1..3`, `passwd1..3`).
- **Umschalten**: `POST http://192.168.178.149/cgconf.cgi` mit allen Pool-Feldern + `mode` + `power_level` (zweistellig, z. B. `04`) –
  derselbe Request wie der „Save“-Button der Seite. Ohne Login möglich.
- Die übrigen Seiten (z. B. `/`, `cgconf.html`) liefern nur die **Login-Seite** – Bezeichnungen dort sind ohne Login nicht lesbar.
- **Modus-Namen (vom Nutzer bestätigt):** „Power Mode 00“ … „Power Mode 04“ und „High Performance“. Genau so anzeigen.
  Interne Schlüssel: `2/L0` … `2/L4`, `1/-`.
- Der Webserver des A15 **trennt beim Umschalten öfter die Verbindung** („Connection reset by peer“) und übernimmt den Befehl
  dann **nicht**. Gleichzeitige GET-Anfragen (bis 4 parallel getestet) sind dagegen stabil. → Umschaltbefehle müssen bis zur
  Bestätigung wiederholt werden (Abschnitt 7.4 / 10).
- Ob ein gleichzeitig im Browser angemeldeter Nutzer die Abbrüche verstärkt, ist **nicht belegt**.

---

## 7. Eigene Funktionen im Detail

### 7.1 Datenerfassung (`data-service/collectors/avalon_collector.py`)
- Hashrate: nur **GHSspd** (gemessen). Leistung: nur **WALLPOWER**, sonst **PS[6]**, sonst 0 (unbekannt). Kein MPO/ATA2-Fallback.
  Grund: Audit-Fix (patch21) – geschätzte Werte hatten falsche Zahlen erzeugt. Tests in `data-service/tests/test_normalized_adapters.py` entsprechend angepasst.
- Stagnationsprüfung über Shares statt über die Hashrate.
- `details` enthält zusätzlich: `work_mode`, `work_level` (A15: aus dem Webinterface, nur wenn WORKMODE = 2), `chip_temp_boards` (MTmax, ungültige Werte → `null`), `system_status` (SYSTEMSTATU).
- `_work_level` liest das A15-Webinterface bei **jeder** Abfrage (10 s) – erzeugt Last am A15-Webserver.

### 7.2 Übersicht (`frontend/src/pages/OverviewDashboard.jsx`)
- **Eigene Einzelansicht je MinerGerät** (Live-Werte alle 10 s, Diagramme je Zeitraum) – vom Nutzer gewollt.
  Darüber die **Problem-/Highlight-Leiste des Originals** (`components/dashboard/FleetAlertsBar.jsx`).
- Kleine Endpunkt **`GET /api/devices/system-latest/`** statt des 1,8-MB-`details`-Aufrufs (Performance).
- A15-Kachel „Working mode“: „High Performance“ bzw. „Power Mode 0X“.
- Kachel „Chip temperature“: beim A15 **alle drei MTmax groß nebeneinander**: „98 / 90 / 82 °C“ (ausdrücklicher Wunsch, nicht klein darunter).
- Familien-Bezeichnungen: Nano Low/Medium/High, Q Eco/Standard/Super.
- Darunter je Avalon die **Drosselungs-Karte** (`components/devices/ThrottleCard.jsx`).
- Test: `frontend/src/pages/__tests__/OverviewDashboard.test.jsx` (eigene Version, prüft Gerätesektion, Leerzustand, Fehlerzustand).

### 7.3 Drosselung (`data-service/throttle_controller.py`, Backend `DeviceViewSet.throttle`)
- Zweck: Ziel-Hashrate (TH/s) über Takt-Senkung halten. **Wird später komplett neu entworfen** – jetzige Werte unwichtig.
- Ablauf: Takt ändern → 60 s Rampe (`PENDING_SECONDS`) → nur GHSspd-Werte zählen, die **≥ 145 s** nach der Änderung frisch sind
  (`CLEAN_AFTER`, weil GHSspd 130 s mittelt + 15 s Takt) → nächster Takt per **Gerade zwischen gemessenen Punkten** (`next_clock`), nicht Dreisatz.
  Grund: Der Dreisatz + veraltete Werte führten zu Hin-und-her-Schaukeln; Hashrate ist nicht proportional zum Takt.
- Totband ±2 %, Nachregeln ab ±4 %, max. 20 % Takt-Schritt, nie unter 15 % des Standardtakts.
- `GET /api/devices/<id>/throttle/` antwortet aus dem gespeicherten Controller-Zustand (`connection_config.throttle.last_seen`) – schnell, mit `stale`-Flag.
- **Während eines Takt-Tabellen-Laufs**: POST/Workmode → **409**; der Controller **liest weiter** (Beobachtungsmodus, `observe`), regelt aber nichts.
  Grund: Sonst blieb die Karte auf einem alten Stand stehen und zeigte den falschen Modus.
- **A15: der Controller misst nichts** (keine Vollgas-Messung, kein `_record_point`). Messwerte für den A15 kommen nur aus den Takt-Tabellen.
- Der frühere „Tipp: sparsamster Modus“ wurde **entfernt** (war falsch).

### 7.4 Takt-Tabellen – nur A15 (`data-service/calibration.py`, `frontend/src/pages/CalibrationPage.jsx`, Menü „Takt-Tabellen“)
Zwei Laufarten, gestartet **nur vom Nutzer** auf der Seite:

**Max-Messung** – je gewähltem Modus zwei saubere Messungen bei den Takten, die **die Firmware selbst** setzt.
**Messreihe** – ein Modus, ab Standardtakt in **10-MHz-Schritten** abwärts bis **15 %** des Standardtakts, eine Messung je Punkt.

Regeln und Gründe (alle vom Nutzer entschieden):

| Regel | Grund |
|---|---|
| Max-Messung setzt **nie** Takte. Jeder Modus wird über das Webinterface eingestellt; ist er schon aktiv: kurz auf eine andere Stufe und zurück („Bounce“, `bounce_key`: 04↔03). | Früher wurden gespeicherte Takte per `ascset` erzwungen → „04 Low“ lief mit 3672 W statt ~2,5 kW. Nutzer: „kompletter Schwachsinn“. Die Firmware soll ihre eigenen Takte setzen. |
| Aufheben einer Drosselung beim Start sendet **keine** Takte. | Gleicher Grund. |
| Messreihe: Standardtakt = Takte, die die Firmware nach dem Umschalten setzt und die **2 min unverändert** bleiben (`CLOCK_STABLE_S`). Nie gespeicherte Profile verwenden. | Gleicher Grund. |
| Gemessen wird der Wert **im Moment** der sauberen Messung, **kein Durchschnitt**. | Nutzer: GHSspd mittelt schon 130 s. |
| Max-Messung: jede Messung **≥ 5 min** nach Moduswechsel bzw. nach der vorherigen Messung (`MAX_WAIT=300`), dann nächster frischer GHSspd-Wert. Die 2. Messung wartet **erneut** voll. | Früher kam die 2. Messung ~2 min nach der 1. – sinnlos. |
| Messreihe: Messung frühestens 145 s nach Taktänderung (`CLEAN_AFTER`). | GHSspd-Fenster. |
| Stabilität: Leistung **3 min** (`STABLE_S`) innerhalb **±1 %** um den Mittelwert (`STABLE_BAND`). | Kriterium „b“ des Nutzers. Früher fälschlich ±0,5 % (max−min ≤ 1 %), korrigiert. |
| Alle Rohwerte werden je Punkt gespeichert (`calibration_points.data`: alle estats-Felder, summary, Webinterface-Konfiguration ohne Passwörter, roher estats-String). Tabelle mit Spaltenfilter. | Nutzer: „Alle möglichen Werte als Data aufjedenfall mitspeichern“. |
| Pause / Fortsetzen / Abbrechen. Fortsetzen misst den **letzten gespeicherten Punkt neu**. | Nutzerwunsch. |
| **Abbrechen lässt den aktuellen Modus stehen.** Nur bei der Messreihe (MinerSentinel hat Takte gesetzt) wird kurz weg- und zurückgeschaltet, damit die Firmware wieder eigene Takte setzt. Normales Ende: zurück zum Startmodus. | Nutzerentscheidung nach dem Fall, dass Abbrechen ihn von High Performance auf 04 zurückstellte. |
| Umschaltbefehl wird wiederholt, bis der A15 den Modus meldet: erneut alle **20 s** (`RESEND_S`), Abbruch nach **300 s** (`SWITCH_TIMEOUT`). Max-Messung überspringt dann den Modus mit Notiz „ließ sich nicht umschalten“. | Der A15 verwirft Befehle bei Verbindungsabbruch (Abschnitt 10). |
| Der Modus wird bei jedem Durchgang (15 s) geprüft. | – |
| Verbindungsfehler überschreiben nicht die Statusmeldung, sondern stehen in `state.last_error` (orange, klein). Lese-Anfragen werden nach 2 s einmal wiederholt (`_retry`). | Vorher war nur noch „Connection reset by peer“ sichtbar. |
| Over Hot → Lauf pausiert automatisch (`pause_reason='overhot'`), Neustart über das Banner, dann „Fortsetzen“. | Nutzerwunsch. |
| Während eines Laufs ist **Drosseln gesperrt** und die **Original-Gerätesteuerung gesperrt** (Abschnitt 8). | Störungen der Messung verhindern. |
| Anzeige: nur der Modusname („Power Mode 02 · Messung 1 von 2“), keine Positionsnummern. **Zwei Countdowns**: „Restzeit <Modus>“ und „Restzeit gesamt“, je mit Uhrzeit „fertig ca. um“. | Nutzer: „Modus 3 von 7 (02)“ war verwirrend; Restzeit pro Modus ausdrücklich gewünscht. |
| Schätzung vor Start: ~13 min pro Modus (60 s Umschalten + 2 × 365 s), Messreihe ~4 min pro Punkt. | – |

Zustandsautomat (`step_run`): `switch` → `wait_mode` → (`set_clock` nur Messreihe) → `measure` → … → `restore` → `restore_back` → `restore_done` → `done`.
Status: `running`, `paused`, `cancelling`, `finishing` (aktiv), `done`, `cancelled`. Der Data-Service schreibt nur, wenn der Status
unverändert ist (Pause/Abbruch aus der Oberfläche haben Vorrang).

Backend: `GET/POST /api/devices/<id>/calibration/` (`start_max` mit `modes`, `start_step` mit `mode`, `pause`, `resume`, `cancel`).
Antwort enthält u. a. `phase`, `stable`, `count`, `steps_total`, `notes`, `changed_at`, `server_now` (für die Countdowns), `last_error`.

**Stand:** Lauf **7** (Messreihe Power Mode 04) ist **pausiert**. Max-Werte-Tabelle wurde auf Wunsch geleert und neu begonnen.

### 7.5 Over Hot (`frontend/src/components/devices/OverHotBanner.jsx`, in `Layout.jsx`)
- Allgemeine Funktion für **alle** MinerGeräte, auf **jeder Seite**: zeigt Banner, wenn `system_status` „Over Hot“ enthält.
- Button „Neustart“ mit Rückfrage → `POST /api/devices/<id>/restart/` (`ascset|0,reboot,0`).
- Benachrichtigung per Messenger: **später** (noch nicht gebaut).

### 7.6 Lüfter – AC Infinity (`backend/api/acinfinity.py`, `backend/api/fan_views.py`, `frontend/src/pages/FansPage.jsx`, Menü „Lüfter“)
Hardware: Controller **UIS 69 Pro** (devType 11, „Legacy“, devId `1424979258063698564`), Lüfter **CLOUDLINE A8** (EC, PWM) an **Port 1**. Controller per WLAN in der AC-Infinity-Cloud.

API (inoffiziell; Quellen: ober37/ac-infinity-mcp `docs/API.md`, keithah/homebridge-acinfinity):
- Basis `https://www.acinfinityserver.com/api`, Login `POST /user/appUserLogin` mit `appEmail` + **`appPasswordl`** (mit „l“), Passwort max. 25 Zeichen → Token `data.appId` als Header `token`.
- Nur **eine** Sitzung je Konto → Token wird gecacht (`acinfinity_data/token`), neu anmelden nur bei Code 10003. (Im Test wurde die Handy-App **nicht** abgemeldet.)
- Lesen: `POST /user/devInfoListAll?userId=<token>` (Werte ×100: Temperatur, Luftfeuchte, VPD); `POST /dev/getdevModeSettingList` (devId, port, appId).
- Schreiben: `POST /dev/addDevMode` – **immer erst lesen, dann alle flachen Felder zurückschicken**, ohne `modeSetid`, `fieldSet`, `devSetting`, `ipcSetting`; **≥ 1,5 s** zwischen Schreibbefehlen (sonst 403).
- Neue Stufe wird erst nach > 5 s zurückgemeldet → MinerSentinel wartet 10 s und liest `speak` neu.

Testergebnisse am echten Gerät (20.10.):

| Test | Ergebnis |
|---|---|
| Variante A: Port 1, `onSpead` + `modeType=2` | ✓ Code 200, Stufe übernommen |
| Variante B (keithah 1.3.3): Port 0, `modeType=0`, `offSpead=1`, `atType=2` | ✗ Code 999999 „Operation failed“ → **entfernt** |
| Nur `onSpead` (ohne `modeType`) | ✓ Code 200, Stufe übernommen, `modeType` bleibt 0 → **so ist es jetzt umgesetzt** |

- `modeType` ist am 69 Pro praktisch wirkungslos; der eigentliche Modus ist **`atType`** (1 = Aus, 2 = An, 3 = Auto, 4/5 Timer, 6 Zyklus, 7 Zeitplan, 8 VPD) bzw. `curMode` (2 = An manuell).
- **Keine Drehzahl einstellbar**, nur Stufen 0–10; keine Drehzahl-Rückmeldung.
- Seite: Zugangsdaten-Formular (schreibt `backend/acinfinity_data/credentials`, Passwort wird nie angezeigt, leer = unverändert), Controller-/Port-Anzeige,
  Sicherung der Einstellungen Port 0–4 (JSON in `acinfinity_data/backup-*.json`) mit „Zurückschreiben“ (Rückfrage), Test-Buttons „Stufe setzen“, „An“, „Aus“.
  Test-Buttons erst nach einer Sicherung freigegeben.
- Endpunkte: `/api/fans/status/`, `/api/fans/credentials/`, `/api/fans/backup/`, `/api/fans/restore/`, `/api/fans/test/` (`action`: `set` | `on` | `off`).
- **Offen:** Automation (nach welcher Regel?) – mit dem Nutzer besprechen. Nutzer sucht ggf. einen Controller mit offener API.

### 7.7 Pools / f2pool (`data-service/collectors/f2pool_collector.py`, `frontend/src/components/settings/DevicePoolSettings.jsx`)
- Pool-Einstellung **je MinerGerät** (`connection_config.pool_stats`: `type`, `address`, `url`, `token`) + globaler Fallback aus den Settings.
- Unterstützte Typen: `ckpool`, `publicpool`, `f2pool` (mit API-Token), `btcpowlab`, `parasite` (die letzten beiden aus 1.2.0).
- `get_pool_targets()` in `app.py` sammelt alle Ziele; Staleness-Alarm des Originals wird nur mit Pool-Typ geprüft, wenn alle Ziele denselben Typ haben.

---

## 8. Integration des Originals 1.2.0 (Entscheidungen vom 20.10.)

| Bereich | Entscheidung |
|---|---|
| Übersicht | Unsere Einzelansicht + Problem-/Highlight-Leiste des Originals (`FleetAlertsBar`). |
| Original-Gerätesteuerung (`components/devices/DeviceControls.jsx`, `data-service/control/*`): Neustart, Lüfter, Workmode für Avalon | Bleibt, ist aber **während eines Takt-Tabellen-Laufs gesperrt** (Frontend-Hinweis + Backend 409 in `device_control`, Flag `calibration_active` in `device_capabilities`). |
| Navigation | Original-Menü + „Activity“ + unsere „Takt-Tabellen“ und „Lüfter“. |
| Settings | Original + unser Block „Pool configuration per device“. |
| Dockerfiles / Compose | Original-Inhalt (node 22, `apk upgrade`, Build auf `$BUILDPLATFORM`) mit voll qualifizierten Image-Namen (`docker.io/library/...`, für Podman). Postgres im Compose mit Digest gepinnt. |
| Tests | Übersichts-Test neu geschrieben; zwei Leistungs-Tests auf „nur gemessene Werte“ umgestellt. |
| Lint | Eine `react/display-name`-Regel in `AnalyticsDashboard.jsx` per Kommentar abgeschaltet (Tooltip-Renderfunktion). |

---

## 9. Datenbank und Migrationen

- Eigene Tabellen: `calibration_runs`, `calibration_points` (Modelle `CalibrationRun`, `CalibrationPoint` in `backend/api/models.py`).
- Migration **`0020_calibration`** (hängt an `0019_push_channels_and_retention` des Originals).
  Auf dem VPS hieß sie früher `0015_calibration` – beim Deploy wurde der alte Eintrag aus `django_migrations` gelöscht, 0015–0019 des Originals ausgeführt und 0020 mit `--fake` eingetragen (Tabellen existierten schon).
- `makemigrations --check` meldet eine ausstehende Migration mit Index-Umbenennungen (`alert_event_*`) und `AlterField make/protocol` – das kommt **aus dem Original** (Modelle und Migrationen dort nicht deckungsgleich). Nicht ungefragt erzeugen; falls nötig mit dem Nutzer klären.
- Drosselungs-Zustand liegt in `devices.connection_config -> 'throttle'` (JSON, mit `rev` gegen gleichzeitiges Schreiben).

---

## 10. Fehlerhistorie und Lehren (nicht wiederholen)

1. **Erzwungene Takte bei der Max-Messung** → falsche Werte (04 mit 3672 W). Lehre: Firmware-Takte nie überschreiben, wenn „Standard“ gemessen werden soll.
2. **„Verbindungsabbruch = gesendet“** (eigene Annahme, ungeprüft) → Umschalten auf 04 klappte gar nicht mehr, Modi wurden übersprungen. Nutzer: „Was bisher makellos funktioniert, jetzt geht nicht mehr.“ Lehre: Der A15 übernimmt bei Abbruch meist **nicht**; immer per Rücklesen bestätigen und wiederholen.
3. **Abbrechen sendete alle 15 s erneut** (Restore-Phase wurde jeden Durchgang neu betreten) → Kettenabbrüche, 93 s Abbruchdauer. Behoben mit `state.restoring`.
4. **2. Max-Messung ohne Wartezeit**. Behoben (Wartezeit neu starten).
5. **Stabilitätsband halb so groß wie vereinbart** (±0,5 statt ±1 %). Behoben.
6. **Drosselungs-Karte zeigte falschen Modus**, weil der Controller während Messungen nichts las. Behoben (Beobachtungsmodus).
7. **Falscher „Tipp: sparsamster Modus“** aus zu wenigen/veralteten Daten. Entfernt. Lehre: keine Empfehlungen aus dünnen Daten.
8. **Schaukeln der Drosselung** durch Dreisatz + veraltete GHSspd-Werte. Behoben (CLEAN_AFTER, Geraden-Methode).
9. **Selbst Messungen gestartet** – vom Nutzer ausdrücklich untersagt.
10. **Test-Skript auf dem Server vergessen** (`backend/t41.py`). Immer aufräumen.
11. Shell-Eigenheiten beim Arbeiten über `osascript`/`ssh` (Anführungszeichen, `\x27`, `\|`) haben Befehle zerbrochen – in Claude Code direkt `ssh lb-db '…'` oder Skript-Dateien per `scp` nutzen.

---

## 11. Offene Punkte / nächste Schritte

1. **Lüfter-Automation**: Regel mit dem Nutzer festlegen (z. B. nach MTmax/Leistung des A15, Raumtemperatur/Luftfeuchte des 69 Pro oder Uhrzeit). Schutz: bei API-Fehler letzte Stufe lassen + Hinweis; nach jeder Änderung zurücklesen.
2. **Neue Drosselungs-Logik** (Nutzer plant komplett neu; vermutlich auf Basis der Takt-Tabellen und Modi statt reiner Taktsenkung).
3. **Over-Hot-Benachrichtigung per Messenger** (Telegram/Discord/ntfy-Kanäle des Originals vorhanden – prüfen, ob nutzbar).
4. Messreihe Lauf 7 ist pausiert – Nutzer entscheidet.
5. Last am A15-Webserver reduzieren (Collector liest `updatecgconf.cgi` alle 10 s, Drosselung + Kalibrierung je 15 s gleichzeitig) – nur nach Rücksprache.
6. Ausstehende Original-Migration (Index-Umbenennung) – nur nach Rücksprache.

---

## 12. Dateikarte (eigene bzw. stark geänderte Dateien)

```
backend/api/
  views.py              DeviceViewSet: system-latest, calibration, restart, workmode, throttle, set-pool, pools;
                        device_control/device_capabilities (Original) + Sperre bei Kalibrierung
  models.py             + CalibrationRun, CalibrationPoint
  migrations/0020_calibration.py
  acinfinity.py         AC-Infinity-Client (Login, Token-Cache, Lesen/Schreiben, Sicherungen)
  fan_views.py          /api/fans/* Endpunkte
  urls.py               + fans/*
  analytics_unified.py, serializers.py, unified_serializers.py   kleinere Anpassungen (VPS-Stand)
data-service/
  app.py                Jobs, get_pool_targets(), Pool-Abfrage je Gerät
  calibration.py        Takt-Tabellen-Zustandsautomat
  throttle_controller.py Drosselung
  collectors/avalon_collector.py   Messwerte, MTmax, Modus/Stufe, Over Hot
  collectors/f2pool_collector.py   f2pool-API
  tests/test_normalized_adapters.py angepasst (nur gemessene Leistung)
frontend/src/
  pages/OverviewDashboard.jsx, CalibrationPage.jsx, FansPage.jsx, AnalyticsDashboard.jsx (VPS-Stand)
  components/devices/ThrottleCard.jsx, OverHotBanner.jsx, DeviceControls.jsx (Sperre)
  components/dashboard/FleetAlertsBar.jsx
  components/settings/DevicePoolSettings.jsx
  components/layout/Layout.jsx (Menü, OverHotBanner), App.jsx (Routen /takt-tabellen, /luefter)
ms-build-frontend.sh, ms-recreate.sh   VPS-Hilfsskripte (virtfs)
docker-compose.yml, */Dockerfile       Podman-tauglich
```

---

## 13. Schnellreferenz Befehle (VPS, als root über `ssh lb-db`)

```bash
cd /opt/minersentinel
# Status
podman ps --format '{{.Names}} {{.Status}}' | grep minersentinel
podman logs --since 5m minersentinel-data-service 2>&1 | grep -iE "error|traceback" | grep -v 192.168.178.137
# DB-Abfrage
podman exec minersentinel-postgres psql -U minersentinel -d minersentinel -tAc "select id,status,message from calibration_runs order by id desc limit 3"
# Django-Shell-Skript ausführen (danach Datei löschen!)
cp x.py backend/ && podman exec minersentinel-backend sh -c "python manage.py shell < x.py"; rm -f backend/x.py
# Data-Service neu starten
podman restart -t 5 minersentinel-data-service
# Frontend bauen + ausrollen
rm -f /root/fe-build7.status; (nohup ./ms-build-frontend.sh > /root/fe-build7.status 2>&1 &); cat /root/fe-build7.status
# A15 direkt lesen
curl -s -m 5 http://192.168.178.149/updatecgconf.cgi | head -c 300
python3 -c "import socket,json;s=socket.create_connection(('192.168.178.149',4028),5);s.sendall(b'{\"command\":\"estats\"}');print(s.recv(65536)[:400])"
```

API-Tests im Backend ohne Browser: Django-Shell mit `rest_framework.test.APIClient`, `force_authenticate(User.objects.get(username='satoshi'))`, `HTTP_HOST='localhost'`.

---

## 14. Rückfragen an die Cowork-Session

Die bisherige Arbeit lief in der Cowork-Session „MinerSentinel Docker setup“:
**https://claude.ai/code/session_018fS1Z9eLgVGhGqvY6DNrgG** – dort ist der komplette Verlauf.

So kannst du (Claude Code) dort nachfragen:

1. **Fragen-Datei (zuverlässig):** Schreib deine Frage in `docs/claude/FRAGEN-AN-COWORK.md` (Vorlage dort) und bitte Onur,
   in der Cowork-Session „Schau in die Fragen-Datei“ zu schreiben. Die Cowork-Session hat Zugriff auf diesen Repo-Ordner und
   antwortet **in derselben Datei** unter deiner Frage. Danach die Datei neu lesen.
2. **Direktnachricht (falls verfügbar):** Wenn deine Umgebung `ListAgents`/`SendMessage` anbietet und die Cowork-Session dort
   auftaucht, kannst du die Frage direkt senden. Die Cloud-Session kann nicht zurückschreiben – die Antwort kommt dann ebenfalls
   in die Fragen-Datei.
3. Bei allem, was der **Nutzer** entscheiden muss (Abschnitt 1): direkt Onur fragen, nicht die Cowork-Session.
