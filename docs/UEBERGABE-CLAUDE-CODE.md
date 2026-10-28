# MinerSentinel – Übergabe an Claude Code

Stand: 25.10.2026, Branch `vps-stand`, Version **v2.0.0** (neue Lüfter-/Taktregelung + Protokoll), deployt auf dem VPS lb-db.de.

Dieses Dokument enthält alles, was in der Cowork-Session „MinerSentinel Docker setup“ (bis 20.10.) und in der
Claude-Code-Session ab 20.10. gebaut, entschieden und gelernt wurde – mit Gründen. Lies es komplett, bevor du
etwas änderst. Wenn etwas unklar ist oder diesem Dokument widerspricht: **frag** (siehe Abschnitt 15), statt zu raten.

---

## 0. Inhalt

1. Arbeitsregeln (vom Nutzer, verbindlich)
2. Projekt und Ziel
3. Infrastruktur: iMac (inkl. lokale Test-Umgebung), GitHub, VPS
4. Container, Dienste, Datenfluss (zentrale Abfrage, Live-API)
5. Deploy, Tests, Rollback
6. Die MinerGeräte und ihre Schnittstellen (gemessene Fakten)
7. Eigene Funktionen im Detail (mit Gründen)
   7.1 Datenerfassung · 7.2 Übersicht · 7.3 Drosselung (nur Nano/Q) · 7.4 Takt-Tabellen (A15) · 7.5 Over-Hot ·
   7.6 Lüfter (AC Infinity) · 7.7 Pools/f2pool · 7.8 Temperatur-Automatik (Regelung v2) · 7.9 Zentrale Abfrage & Live-API ·
   7.10 Protokoll
8. Integration des Originals 1.2.0 (Entscheidungen)
9. Datenbank und Migrationen
10. Fehlerhistorie und Lehren (wichtig – nicht wiederholen)
11. Offene Punkte / nächste Schritte
12. Dateikarte
13. Schnellreferenz Befehle
14. Lokale Test-Umgebung (iMac)
15. Rückfragen an die Cowork-Session

---

## 1. Arbeitsregeln (verbindlich)

| Regel | Grund / Hintergrund |
|---|---|
| Antworten **auf Deutsch, sehr kurz**, ohne Einleitung, ohne Zusammenfassung am Ende, ohne Füllsätze. | Ausdrücklicher Wunsch. |
| **Jede Antwort beginnt mit Datum und Uhrzeit** (`TT.MM.JJJJ, HH:MM`, vorher `date` ausführen, nicht schätzen). | Nutzer 21.10., gilt für alle Chats/Sessions. |
| **Pläne/Logik:** Abschnitte mit Überschrift und kurzer Inhaltsangabe (nicht nur Nummern), **keine Abkürzungen**, nichts Erfundenes hineinschreiben; eigene Vorschläge ausdrücklich als Vorschlag kennzeichnen; Fragen konkret mit Beispiel und mit der wörtlichen Stelle des Nutzertexts. Frühere Messungen nur als Bezug, wenn der Nutzer es erlaubt. Vor dem Abgeben jeden Satz gegen den Text des Nutzers prüfen. | Nutzer 23./24.10.: falsche Verknüpfungen und erfundene Annahmen kosteten mehrere Runden. |
| „erst X, dann Freigabe“ ist **keine** Freigabe. Bei Unklarheit fragen. | Vorfall 21.10. |
| Begriff **„MinerGerät“** statt „Worker“ verwenden. | Ausdrücklicher Wunsch. |
| **Vor größeren Änderungen** erst den Plan kurz und verständlich erklären, dann auf Freigabe warten, erst danach umsetzen. | Mehrfach eingefordert. Kleine, klar beauftragte Fixes dürfen direkt erfolgen. |
| **Erst prüfen, dann behaupten.** Keine Vermutung als Tatsache. Zahlen aus echten Daten (DB, Logs, API) belegen; Unbelegtes als „nicht belegt“ kennzeichnen. | Falsche Aussagen haben Vertrauen gekostet. |
| **Keine ungefragten Extras**, keine Alternativ-Listen, keine irrelevanten Infos. | Ausdrücklicher Wunsch. |
| **Takt-Messungen (Max-Messung, Messreihe) startet/fortsetzt/pausiert nur der Nutzer selbst.** | Ausdrückliche Anweisung nach einem Vorfall. |
| Auf dem VPS lb-db.de **nur `/opt/minersentinel` und die eigenen Container** anfassen. Keine anderen Seiten, keine Reverse-Proxy-/Webserver-Konfiguration. | Auf dem Server laufen produktive Seiten von LieferBär. |
| **Keine Passwörter eingeben**, keine Zugangsdaten anlegen/ändern. **Zugangsdaten-Dateien (`.env`, `acinfinity_data/credentials`) nicht lesen, kopieren oder vergleichen – auch nicht per Hash.** | Sicherheitsregel; Hash-Vergleich wurde am 21.10. ausdrücklich abgelehnt. |
| Selbst geschriebene Test-/Hilfsskripte auf dem Server nach Gebrauch löschen (nicht in `/opt/minersentinel/backend` liegen lassen). | Ist einmal passiert (`t41.py`). |
| **Git:** commit nur auf Anweisung („committen“). **Seit 21.10. pusht Claude selbst** (`gh auth login` als lieferbaer, `gh auth setup-git`; Anmeldung hat der Nutzer im Browser bestätigt). Danach „VPS auf Stand bringen“ (Abschnitt 5). | Nutzerwunsch 21.10. (vorher Push über GitHub Desktop). |
| Nach jeder Änderung am Laufenden: **Logs und Funktion prüfen** und kurz berichten. | Mehrfach hat erst die Kontrolle Fehler gezeigt. |

---

## 2. Projekt und Ziel

- **MinerSentinel**: Monitoring-Dashboard für Bitcoin-Miner (Original `github.com/dcbert/miner-sentinel` 1.2.0). Eigener Fork
  `github.com/lieferbaer/miner-sentinel`, erreichbar unter **https://miners.lb-db.de** (Login `satoshi`, Passwort beim Nutzer).
- Überwacht und steuert Onurs Avalon-Miner (Abschnitt 6) und den Lüfter-Controller AC Infinity 69 Pro.
- Ziele des Nutzers: bis zu 10× Avalon A15 zuhause (~40 kW), leiser und sparsamer; Lüfter an die Miner gekoppelt
  (**umgesetzt: Temperatur-Automatik, 7.8**); Over-Hot-Benachrichtigung per Messenger (offen); ferner Zukunft:
  entfernter Standort mit Raspberry Pi + LTE (nur Notiz).

---

## 3. Infrastruktur

### 3.1 iMac (Entwicklung)
- Repo: `/Users/onurtural/Documents/GitHub/miner-sentinel`, Remotes: `origin` = Fork lieferbaer, `upstream` = dcbert.
- `remote.origin.fetch` steht seit 20.10. auf `+refs/heads/*:refs/remotes/origin/*` (vorher nur `main` → GitHub Desktop
  zeigte bei `vps-stand` immer „Publish branch“). `vps-stand` folgt `origin/vps-stand`.
- Branches: `main` = Original 1.2.0, **`vps-stand` = Arbeitsstand**, `imac-alt-sep` = Archiv (Konfliktmarker, nicht mergen), `claude` = alt.
- Lokale Test-Umgebung: Abschnitt 14.

### 3.2 VPS lb-db.de (Produktion)
- AlmaLinux 9.8, CloudPanel. Zugang vom iMac: **`ssh lb-db`** (root, Key-Login).
- Projektordner **`/opt/minersentinel`** (Git-Checkout, Branch `vps-stand`, Remote **`fork`** = lieferbaer; `origin` = dcbert, nicht benutzen).
- Nie überschreiben/committen: `.env`, `backend/acinfinity_data/` (AC-Infinity-Zugangsdaten, Token, Sicherungen, Pause-Datei),
  `docker-compose.yml.imac`.
- **Podman 5.8.2 (rootful) + podman-compose 1.5.0.** Host-Besonderheit: Die Jailshell spiegelt Overlay-Mounts unter
  `/home/virtfs/*` → `podman rm`/`build` schlagen manchmal fehl. Dafür `ms-recreate.sh` und `ms-build-frontend.sh`.
  Kann trotzdem einen „hängenden“ Container hinterlassen → Abschnitt 13 „Web-Container hängt“.
- Heimnetz `192.168.178.0/24` über **WireGuard `wg0`** (VPS `10.66.66.1`) – nicht ändern.

---

## 4. Container, Dienste, Datenfluss

| Container | Inhalt | Code-Mount | Änderungen wirksam durch |
|---|---|---|---|
| `minersentinel-postgres` | Postgres 17, DB/User `minersentinel` | – | – (nie neu erstellen) |
| `minersentinel-backend` | Django 5.2 + DRF, `runserver` | `./backend:/app` | Autoreload. Neue Migration: `podman exec minersentinel-backend python manage.py migrate` |
| `minersentinel-data-service` | Flask + APScheduler | `./data-service:/app`, **`./backend/acinfinity_data:/acinfinity_data`**, **`./backend/api:/backend_api:ro`** (seit 21.10.) | `podman restart -t 5 minersentinel-data-service`; Compose-Änderung: `./ms-recreate.sh data-service minersentinel-data-service` |
| `minersentinel-web` | nginx + gebautes Vite-Frontend, `127.0.0.1:3080` | – | `./ms-build-frontend.sh` |

- Data-Service-Umgebung (Compose): `ACINFINITY_DATA_DIR=/acinfinity_data`, `ACINFINITY_MODULE_DIR=/backend_api`
  (er nutzt den AC-Infinity-Client des Backends und dieselbe Anmeldung). Läuft als `appuser` (uid 1000), darf `acinfinity_data` beschreiben.
- Backend läuft als `appuser`, darf `/app` nicht beschreiben → `makemigrations` mit `podman exec -u 0` + `chown root:root`.
  Migrationen schreiben wir inzwischen **von Hand** (siehe 9).
- `.env`: `POLLING_INTERVAL_SECONDS=10` → Datenerfassung (`poll_all_sources`) alle 10 s.

**Scheduler-Jobs im Data-Service** (`data-service/app.py`):

| Job-ID | Intervall | Aufgabe |
|---|---|---|
| **`live_cycle`** | **5 s** | **Zentrale Abfrage** aller Avalons (`live_reader.py`), danach sofort **Temperatur-Automatik**; jede 3. Runde (15 s) **Drosselung + Takt-Tabellen** in eigenem Worker (7.9) |
| `poll_all_sources` | 10 s (env) | Datenerfassung: **speichert** die zentralen Werte in die DB (fragt Avalons nicht selbst), Pools, andere Geräte |
| **`prune_automation_journal`** | **täglich 03:30** (CronTrigger) | Protokoll-Tabellen: Einträge älter als **360 Tage** löschen (in Blöcken, 7.10) |
| `check_devices`, `network_data` (30 min), `check_settings` (2 min), `prune_retention` (24 h) | – | Original |

Die früheren Einzel-Jobs `throttle_controller`, `calibration` (je 15 s) und `fan_automation` gibt es nicht mehr.

**Browser:** eine gemeinsame Abfrage **`GET /api/live/` alle 5 s** (`frontend/src/lib/LiveContext.jsx`, eingebunden in `Layout.jsx`),
solange die Seite offen ist. Steuerung läuft immer auf dem Server, unabhängig vom Browser.

---

## 5. Deploy, Tests, Rollback

### 5.1 Arbeitsablauf
1. Auf dem iMac in `vps-stand` ändern, Tests (5.3), Nutzer sagt „committen“ → Claude committet und pusht (`git push origin vps-stand`).
   **Vor jedem Push die eigene Versionsnummer `frontend/src/lbVersion.js` um 1 erhöhen** (letzte Stelle; Anzeige neben
   „MinerSentinel“ oben links). Start 1.0.0 am 21.10.; seit 25.10. Zählung 2.0.x (Nutzer: „ab sofort Version 2.0.0“); **seit 26.10. 2.1.x** (Nutzer: „Version 2.1.0“, nach 2.0.9); **seit 27.10. 2.2.x** (Nutzer: „als Version 2.2.0 committen“, nach 2.1.8, mit dem einstellbaren Zielbereich).
2. Nutzer sagt „VPS auf Stand bringen“ (oft zusammen mit „committen“).
3. VPS: `git ls-remote origin vps-stand` (iMac) prüfen, dann auf dem VPS
   `cd /opt/minersentinel && git status --short && git fetch -q fork vps-stand && git checkout -q -f vps-stand && git reset -q --hard fork/vps-stand`
   – vorher sicherstellen, dass `git status` nur `?? docker-compose.yml.imac` zeigt.
4. Je nach Teil: Backend (Autoreload; Migration → `migrate`), Data-Service (`podman restart -t 5 …`; Compose → `ms-recreate.sh`),
   Frontend (`rm -f /root/fe-build7.status; (nohup ./ms-build-frontend.sh > /root/fe-build7.status 2>&1 &)`, fertig bei `BUILT`+`FIN`;
   „build try 1 failed“ ist das bekannte virtfs-Problem, das Skript wiederholt selbst).
   **Reihenfolge bei neuen Tabellen/Spalten: erst `migrate`, dann Data-Service neu starten** (der Data-Service liest/schreibt per SQL;
   fehlt z. B. `temp_test_at`, scheitert die Abfrage der Automatik).
   Vor Migrationen/Datenänderungen betroffene Tabellen sichern (`pg_dump -t …` nach `/root/ms-bak-…`).
5. Prüfen: `podman ps` (alle 4 Container!), Data-Service-Log auf `ERROR`/`Traceback`, `curl https://miners.lb-db.de/` = 200.
   Ein Data-Service-Neustart unterbricht einen laufenden Takt-Lauf nur ~10 s (Zustand in der DB).

### 5.2 Sicherungen / Rollback
- `/root/ms-deploy-bak-20261020-2213/` – vor 1.2.0-Deploy (Tarball, `db.dump`, alte `0015_calibration.py`); Images `:pre-deploy`.
- `/root/ms-cache-bak-20261020-2255/` – Data-Service-Dateien vor dem Zwischenspeicher.
- **`/root/ms-fan-auto-bak-20261021-0047/`** – vor der Temperatur-Automatik: `docker-compose.yml`, `podman inspect` von
  Data-Service/Backend, `db.dump`, `git-head.txt`; Images `localhost/minersentinel_{backend,data-service,frontend}:pre-fan-automation`.
  Rückweg: Compose zurückkopieren, Images verwenden, `migrate api 0020`.
- `/root/ms-caltab-bak-20261021-1757.sql` – Takt-Tabellen (`calibration_points`, `calibration_runs`) vor dem Leeren am 21.10.
- **`/root/ms-bak-v107-20261025-1053/`** – vor v1.0.7 (neue Regelung): `fan_devices.sql` (`fan_automation`, `devices`), `git-head.txt`.
  Rückweg: Commit aus `git-head.txt` auschecken, Tabellen zurückspielen, `migrate api 0023` (entfernt die Protokoll-Tabellen).
- `/root/thr6/bak21 … bak42` – alte Einzel-Patches der Cowork-Zeit.

### 5.3 Tests (wie CI)
- Backend: `PYTHONPATH=backend DJANGO_SETTINGS_MODULE=minersentinel.settings_test python -m pytest backend/api/tests -c pyproject.toml --ds=minersentinel.settings_test`
- Data-Service: `PYTHONPATH=data-service python -m pytest data-service/tests -c pyproject.toml -m "not integration"`
- Frontend: `cd frontend && npm ci && npm run lint && npm run test:run && npm run build`
- Python ≥ 3.10 nötig (System-Python 3.9 reicht nicht): `/opt/homebrew/bin/python3.10 -m venv <dir>` + requirements von backend und data-service + pytest, pytest-django.
- pytest mit `-p no:cacheprovider -q -o addopts=""` aufrufen, sonst fehlt die Zusammenfassung (pyproject-Einstellungen).
  Das venv im Scratchpad kann von macOS teilweise gelöscht werden (25.10.: `pytest` fehlte) → in jeder Session neu anlegen.
- Stand 28.10. (v2.2.5): **Backend 218 ✓, Data-Service 363 ✓ (7 übersprungen), Frontend 170 ✓**, Lint 0 Fehler (15 Warnungen aus dem Original).
  Achtung: `npm run lint` meldet zusätzlich Fehler aus `frontend/dist-local/` (lokaler Build-Ordner, nicht im Repo) – ignorieren.

---

## 6. Die MinerGeräte (gemessene Fakten)

| DB-id | Name | device_id | IP:Port | Familie | Modi |
|---|---|---|---|---|---|
| 1 | Avalon Nano 3S | `nano3s-1` | 192.168.178.137:4028 | `nano` | WORKMODE 0/1/2 = Low/Medium/High |
| 2 | Avalon Q | `avalonq-1` | 192.168.178.138:4028 | `q` | WORKMODE 0/1/2 = Eco/Standard/Super |
| 3 | Avalon A15 | `A15` | 192.168.178.149:4028 | `a15` | WORKMODE 2 = Power Mode 00–04, WORKMODE 1 = High Performance |

- Nano 3S und Avalon Q: „bekommen keine Messwerte, damit haben wir nichts zu tun“ – Drosselungs-Karte bleibt dort im alten Stand (TH/s).
- Pool: f2pool, Konto `onr`.

### 6.1 cgminer-API (TCP 4028)
- Befehle: `estats`, `summary`, `pools`, `version`; Schreiben: `ascset` `0,frequency,<p1:p2:p3:p4>-0-0`, `0,reboot,0`, Workmode Nano/Q `0,workmode,set,<n>`.
- **`SF0`, `SF1`, `SF2` = je Board 4 Takt-Stufen (PLL-Punkte), auf allen Boards gleich**, z. B. Standard Power Mode 04:
  330 / 351 / 370 / 390 MHz. *(Die alte Angabe „SF0 = Takt je Board“ war falsch.)* MinerSentinel setzt diese 4 Stufen
  (verhältnisgleich) und zeigt ihr **Mittel** als „Takt“ bzw. „Takt-Stufen (Mittel)“.
- **`Freq`** = vom Miner berechneter **effektiver Durchschnittstakt** (je nach Verteilung der Chips auf die 4 Stufen),
  aktualisiert ~alle 130 s. Weicht vom Mittel der Stufen in beide Richtungen ab (Standard 04: 360 vs. 333; Messreihe 286 vs. 304;
  Automatik 231 vs. 253). Übersicht zeigt ihn als „effektiver Takt“ mit ⓘ-Erklärung.
- Die Firmware **zieht gesetzte Takt-Stufen langsam nach oben** (280 → 286, 228 → 237, 220 → 228 MHz; 25.10. auch +13…+20).
  Die Regelung v2 rechnet mit dem **gesetzten** Takt (Nutzerentscheidung 23.10.); der gemeldete dient nur der Übernahme-Prüfung und
  der Erkennung „Miner ändert den Takt selbst“ (7.8).
- Ein neuer Takt wird von der Firmware **in Schritten über einige Sekunden** eingestellt (25.10.: 183 → 152 → 123 → 100 MHz).
- Ein Moduswechsel (Webinterface) stellt den **Standardtakt des Modus** ein (Mode 04 ≈ 360, 03 ≈ 380, 02 ≈ 400, 01 ≈ 420, 00 ≈ 440 MHz).
- `GHSspd` = gemessene Hashrate, aktualisiert ~alle 130 s, mittelt diese Zeit. `GHSmm` ≈ 481,5 × Freq. `DH` = Fehlerrate %.
- Leistung: `WALLPOWER` (A15), sonst `PS[6]`. `MPO` ist nur ein Limit.
- **`TMax` = heißester Chip = max(`MTmax[a b c]`)**, alle 10 s neu, schwankt ±1 °C. Heißestes Board ist immer **Board 1**
  (4–6 °C über Board 2). Nach Neustart kurz `-273` = kein Wert.
- **DH (Fehlerrate)** = fehlerhafte ÷ alle Prüfungen der Chips je Zeitfenster (~134 s); `GHSspd` zählt nur die fehlerfreien →
  DH kostet direkt Hashrate (25.10. Stufe 2: GHSmm 143 TH/s, GHSspd 126 TH/s bei DH 12 %). Normal laut Canaan bei A10–A12 0,6–1,6 %;
  **Nutzerziel ≤ 3 %**. Gemessen 25.10.: ~175 MHz → 6 %, ~315 MHz → 8–13 %; bei 79–83 °C kein Temperatur-Einfluss sichtbar.
- **Temperatur (Recherche 25.10., Quellen in der Session):** Canaan regelt auf **TAvg** (Mittel aller Chips), A15-Werkseinstellung
  70 °C, Mini 3 (gleiche Chips) im Mining-Modus 75 °C; Canaan schreibt, die Chips arbeiten bei höheren Temperaturen besser
  (Nutzererfahrung: ASICs haben einen optimalen Bereich, **kühler ist nicht besser**). Mit TMax 80 °C liegt TAvg bei ~67 °C.
  Überhitzungsschutz der Firmware: TMax > 108 °C. Zusammenhang Temperatur ↔ DH bei festem Takt ist nirgends gemessen → Temperatur-Test (7.8).
- `SYSTEMSTATU` mit `Over Hot` = Boards abgeschaltet. `FanR`/`Fan1..` = interne A15-Lüfter: **nur Anzeige auf der Übersicht, sonst
  nirgends verwenden** (Nutzer: „Lüfter-Simulatoren“).
- Thermik (Analyse 21.10., 4 saubere Leistungssprünge ≥ 80 W + Lauf 7): Temperaturänderung erreicht ~50 % nach 60–90 s,
  ~85 % nach **3 min**, ~100 % nach ~7 min; **~1,5 °C und ~50 W pro 10 MHz** (Lauf 7, 320 → 280 MHz). Unsicher (wenige Daten,
  Lüftertest lief parallel). Drosseln über Takt spart wenig Strom.

### 6.2 A15-Webinterface (Port 80)
- `GET /updatecgconf.cgi` (ohne Login): `mode` (2/1), `power_level` (0–4), Pool-Felder. Umschalten: `POST /cgconf.cgi` mit allen
  Pool-Feldern + `mode` + `power_level` (zweistellig) – ohne Login. Webserver trennt beim Umschalten oft die Verbindung und übernimmt
  dann **nicht** → wiederholen bis Rückmeldung (7.4).
- Modus-Namen: „Power Mode 00“ … „Power Mode 04“, „High Performance“. Schlüssel `2/L0`…`2/L4`, `1/-`.

### 6.3 AC Infinity (UIS 69 Pro, CLOUDLINE A8 an Port 1)
- Inoffizielle Cloud-API (Details 7.6). Nur Stufen 0–10, keine Drehzahl. **Der Controller meldet eine neue Stufe erst nach > 5 s.**
- Nur **eine Sitzung je Konto**: eine Anmeldung kann die andere (Test-Umgebung, Handy-App) abmelden (Code 10003).

---

## 7. Eigene Funktionen im Detail

### 7.1 Datenerfassung (`collectors/avalon_collector.py`)
- Nimmt `estats`/`summary` **aus der zentralen Abfrage** (`_live_request`, bis 8 s alt), fragt nur im Ausnahmefall selbst.
  `version`/`pools` alle 10 min, A15-Webinterface aus dem Zwischenspeicher (bis 35 s).
- Hashrate nur `GHSspd`, Leistung nur `WALLPOWER`/`PS[6]` (keine Schätzwerte). `details`: `work_mode`, `work_level`,
  `chip_temp_boards` (MTmax), `system_status`, `error_rate_pct` (DH). Speichert alle 10 s.

### 7.2 Übersicht (`pages/OverviewDashboard.jsx`)
- Einzelansicht je MinerGerät; **Live-Werte aus `/api/live/` (5 s)**, Diagramme eigene Abfragen (1 min).
- A15: Kachel „Working mode“, „Chip temperature“ mit allen drei MTmax groß („98 / 90 / 82 °C“).
- Efficiency: „effektiver Takt X MHz“ + ⓘ (Erklärung nach 1 s Mouse-over / Antippen, `components/ui/info-tip.jsx`).
- Fan speed (A15): „rpm / S2“ – S = **zurückgelesene** AC-Lüfterstufe (nicht die gesetzte), nur bei eingeschalteter Automatik.
- Hashrate-Karte: „… GH/s · Fehlerrate x %“ (aktuelle `DH` des Miners, `details.error_rate_pct`, alle 10 s); dieselbe Karte ist
  seit v2.0.0 die **erste Karte auf der Lüfter-Seite** (dort steht die Fehlerrate nicht mehr in „Takt laut Miner“).
- Darüber Problem-/Highlight-Leiste des Originals (`FleetAlertsBar`), darunter je Avalon die Takt-/Drosselungs-Karte
  (A15: „Takt und Modus“ ohne Eingabe, 7.3).

### 7.3 Drosselung – nur Nano 3S / Avalon Q (`throttle_controller.py`, Backend `DeviceViewSet.throttle`, `components/devices/ThrottleCard.jsx`)
- **A15: Drosselung abgeschafft (v1.0.7, Nutzer 24.10.).** `POST /api/devices/<A15>/throttle/` → 400 („Die Drosselung des A15 ist
  abgeschafft; den Takt regelt die Temperatur-Automatik.“). Die A15-Karte heißt „Takt und Modus“: groß der **Takt laut Miner**,
  darunter „gesetzt X MHz (Automatik|Takt-Messung) · Standard Y MHz“, Modus-Buttons und Modus-Tabelle; keine Eingabe.
  „gesetzt“ = `_last_set_clock` (nur noch Takt-Messung und Automatik; bei ausgeschalteter Automatik nur, solange der Miner den Takt
  noch fährt – sonst „—“). Migration 0024 entfernt eine noch vorhandene alte A15-Drosselung (ohne Taktbefehl).
- **Nano 3S / Avalon Q: unverändert** (Ziel-Hashrate, Geraden-Methode, Totband ±2 %, max. 20 % Schritt; „Aufheben“ sendet Standardtakt).
- Sperren: nur während ein Takt-Lauf **läuft** (409).
- Während Takt-Lauf oder Automatik liest der Controller nur (Beobachtungsmodus) – lernt dann keine „Standardtakte“.
- **A15-Standardtakte (`profiles`) werden nie mehr überschrieben**, sobald bekannt, und nicht gelernt, nachdem die Automatik Takte
  gesetzt hat (`auto_clock_set`): sonst würde der Takt nach „Automatik aus“ als Standardtakt gelernt.
- Zustand: `devices.connection_config -> 'throttle'` (mit `rev`; Controller schreibt nur bei unverändertem `rev`, sonst Rollback).

### 7.4 Takt-Tabellen – nur A15 (`calibration.py`, `pages/CalibrationPage.jsx`)
Max-Messung (je Modus 2 saubere Messungen bei Firmware-Takten) und Messreihe (ab Standardtakt in 10-MHz-Schritten bis 15 %).
Start/Pause/Fortsetzen/Abbrechen **nur durch den Nutzer**.

| Regel | Grund |
|---|---|
| Max-Messung setzt nie Takte; Modus über das Webinterface, ist er schon aktiv: kurz weg und zurück (Bounce 04↔03). | Firmware soll eigene Takte setzen (Fehler 1). |
| Messreihe: Standardtakt = Firmware-Takte, 2 min unverändert. | – |
| Messwert **im Moment** der sauberen Messung, kein Durchschnitt. | GHSspd mittelt bereits. |
| Max-Messung: ≥ 5 min nach Moduswechsel/vorheriger Messung; Messreihe: ≥ 145 s nach Taktänderung (`CLEAN_AFTER`). | GHSspd-Fenster. |
| **Stabilität: Leistung 3 min innerhalb ±3 %** (`STABLE_BAND = 0.03`, Nutzerentscheidung 21.10.). Die **tatsächliche Schwankung** wird je Punkt gespeichert (`data.power_spread_pct`, Spalte „Schwankung“). Ggf. zurück auf ±2 %, wenn die Werte zeigen, dass ±3 % zu viel ist. | Lüfter regelt im Lauf stufig auf 80 °C (7.8). Vorher ±1 %. |
| Jeder Punkt speichert alle Rohwerte **und die AC-Lüfterstufe** (`data.fan_level`, Spalte „Lüfter (AC)“). | – |
| **Messreihe endet ab dem 10. Punkt, sobald ein gespeicherter Punkt DH ≥ 10 % hat** (`DH_STOP_PCT`, `DH_STOP_FROM`); der Punkt wird noch gespeichert, dann normales Ende. | Nutzer 21.10.: tiefere Takte lohnen nicht. Lauf 7: Punkt 0 (Standardtakt) hatte 18,9 %, Punkt 22 (150 MHz) 10,4 %. |
| **Start und Fortsetzen schalten die Temperatur-Automatik ein**; ohne eingerichteten Lüfter kein Start (409). | Während des Laufs pausiert die Taktregelung, der Lüfter reagiert nur nach „Hohe Chiptemperaturen“ (7.8). |
| **Fortsetzen:** letzten Punkt neu messen; **Max-Messung schaltet dabei kurz weg und zurück**. | In der Pause kann die Automatik Takte gesetzt haben. |
| Abbrechen lässt den aktuellen Modus stehen. Normales Ende: zurück zum Startmodus. Messreihe **ohne** Automatik: kurz weg/zurück für Firmware-Takte; **mit Automatik nicht** – Takt bleibt, die Automatik übernimmt sofort (Lernwert). | 21.10.: Weg/zurück holte bei Lüfter Stufe 1–2 die Firmware-Takte → 100 °C. |
| Umschaltbefehl alle 20 s wiederholen bis Rückmeldung, Abbruch nach 300 s. | Fehler 2. |
| Over Hot → automatische Pause. | – |
| Anzeige: Modusname, zwei Countdowns (Modus/gesamt); Status kommt aus `/api/live/`, Tabellen laden bei Fortschritt bzw. jede Minute. | – |

Typische Dauer Messreihe: **~4,5 min pro Punkt** bei stabilen Bedingungen (Lauf 7 Punkte 6–9), anfangs 11–13 min.
**Stand 25.10.:** Takt-Tabellen am 21.10. 17:57 geleert (Sicherung 5.2). „Max-Werte je Modus“ enthält zwei vom Nutzer beauftragte
Punkte (je 15 min bei 250 MHz, Endwert): Mode 04 252 MHz laut Miner, 121,8 TH/s, 1.961 W, 16,1 J/TH, DH 3,18 %; Mode 00 262 MHz,
140,3 TH/s, 3.062 W, 21,8 J/TH, DH 0,14 %. Karte „Manuell“ zeigt nur noch alte Punkte der früheren Drosselung (falls vorhanden).

### 7.5 Over Hot (`components/devices/OverHotBanner.jsx`)
- Banner auf jeder Seite (Daten aus `/api/live/`), Neustart mit Rückfrage (`POST /api/devices/<id>/restart/`). Messenger: später.

### 7.6 Lüfter – AC Infinity (`backend/api/acinfinity.py`, `fan_views.py`, `pages/FansPage.jsx`)
- API: Login `POST /user/appUserLogin` (`appEmail`, `appPasswordl`, max. 25 Zeichen), Token `data.appId` als Header `token`,
  Token-Cache `acinfinity_data/token`. Lesen `devInfoListAll`, `getdevModeSettingList`; Schreiben `addDevMode` (immer erst lesen,
  alle flachen Felder zurück, ohne `modeSetid/fieldSet/devSetting/ipcSetting`, ≥ 1,5 s Abstand). Stufe setzen = nur `onSpead`
  (`set_fan_level`). `atType` 1 Aus / 2 An.
- **Anmeldung Live/Test:** Umgebung über `MINERSENTINEL_ENV` (Standard „Live“). **Test** meldet sich nur über „Test aktivieren“ an,
  höchstens 5 min, nie automatisch neu. **Live** meldet sich nach einer Übernahme durch eine andere Anmeldung (10003) erst nach
  **5 min Pause** wieder an (`relogin_after`), oder sofort über „Live jetzt übernehmen“.
- Seite: Zugangsdaten-Formular (Nutzer trägt selbst ein), Sicherung/Zurückschreiben (Test-Buttons erst nach Sicherung),
  **An/Aus** neben dem Gerätenamen, **Port 1–4 als Buttons** (Auswahl in `localStorage`), **Stufe 0–10 als Buttons: ein Klick setzt**
  (Rücklesen nach `READ_BACK_S = 5` s); markiert ist die vom Controller gemeldete Stufe. Bei aktiver Automatik wählen die Buttons
  die Automatik-Stufe; An/Aus und Port gesperrt.
- Automatik-Bereich: **5 Karten** (Hashrate mit Fehlerrate wie Übersicht – erste Karte seit v2.0.0; Heißester Chip mit Regelwert
  und Tendenz; Takt laut Miner mit gesetzt · Modus · Standard; Lüfter läuft auf (aktive Stufe vs. Basisstufe, Korrektur/Ausnahme);
  Status mit Phase, laufendem Befehl, Beobachtungsfenster, Lernperiode). Kleine Zeile wiederholt den großen Wert nicht. Auf dem Handy
  untereinander. Darunter Lernwerte **je Lüfterstufe** und letzte Aktionen. Rückfrage „Automatik wirklich beenden? Der Takt bleibt,
  wie er ist.“

### 7.7 Pools / f2pool
- Pool-Einstellung je MinerGerät (`connection_config.pool_stats`) + globaler Fallback; Typen ckpool, publicpool, f2pool, btcpowlab, parasite.
- **f2pool alle 5 min** als eigener Job `poll_f2pool` (v2.1.4, Nutzer 26.10.; vorher in jeder Geräteabfrage alle 10 s mit 3 Anfragen à ~1 s,
  `pool_stats` wuchs ~6.500 Zeilen/Tag). `poll_all_sources` überspringt f2pool; Fehler → `check_pool_staleness(force_failure)` wie bisher
  (Warnung „Pool stats unavailable“ erst ab 30 min ohne frische Werte, Standard).

### 7.8 Temperatur-Automatik v2 – nur A15 (`data-service/fan_automation.py`, Tabelle `fan_automation`)
Seit **v1.0.7 (25.10.)** nach dem Plan des Nutzers vom 23./24.10. Diagramm: **`docs/claude/Regelungslogik-Luefter-Takt.drawio`**
(8 Seiten, vom Nutzer freigegeben). Läuft **nach jeder zentralen Abfrage (5 s)**, nur auf Live. Einschalten auf der Lüfter-Seite
(Port + Stufe; braucht Sicherung). Der Data-Service ist dann der einzige, der Lüfter-, Takt- und Modusbefehle sendet.
**Bei Änderungen an der Regelung: Plan/Diagramm zuerst mit dem Nutzer abstimmen** (Regeln 1).

**Grundprinzip:** Der Nutzer wählt die Lüfterstufe = **Basisstufe** (gewählt werden 1–7; 8–10 „nie als Basisstufe“). Die Automatik
regelt den **Takt** (gesetzter Takt, 100–440 MHz), damit die **Chiptemperatur** (TMax, heißester Chip, Einzelwert) im
**Zielbereich L–U** liegt; **dort keine Taktänderung** (v2.0.6, Nutzer 25.10.). **Seit v2.2.0 stellt der Nutzer den Zielbereich
selbst ein** (Nutzer 27.10.; vorher fest 79–84 °C) – Zahlen in der Tabelle unten mit 79/80/84/85/70/75/60 gelten für den
alten Bereich 79–84 und wandern so mit, wie in der Zeile „Zielbereich“ steht. Der **Modus folgt dem Takt**. Der Lüfter weicht nur vorübergehend ab (Korrekturphase nach oben, Ausnahme nach unten).

| Regel | Vorgabe des Nutzers |
|---|---|
| **Zielbereich** (v2.2.0, Nutzer 27.10.) | Karte „Zielbereich“ auf der Lüfterseite (Variante „Skala antippen“: Kacheln 76 bis 92, erst ein Ende, dann das andere, „Übernehmen“). **L ab 76, U bis 92 °C, Breite 2–4 Grad, beide Grenzen mitgezählt** (82–83 = 2 Grad, 82–85 = 4 Grad; U−L = 1 bis 3). Gespeichert in `fan_automation.target_low/target_high` (Migration 0027, **Startwert 82–85 °C**, vom Nutzer gewählt). Einstellbar immer (auch Automatik aus, Wartung, Takt-Messung); Protokoll `zielbereich` „Nutzer: Zielbereich L–U °C gewählt (vorher …)“, Details `alt`/`neu`; ein laufender Temperatur-Test endet. **Was mitwandert:** keine Taktänderung, Lernband, Aufenthalt und Taktsperre = L–U · hohe Chiptemperatur ab U+1, Tabelle U+1/+2 (Lüfter +1, Aufenthaltsregel), U+4 −30, U+6 −50, U+8 −80, U+10 −100, **jede Schwelle höchstens 96 °C** (ab 96 °C immer +2/−100) · Rückkehr zur Basisstufe nach 1 min stabil in **L–U** oder sofort unter **L** (vorher 80–84 bzw. unter 80) · Plus-Schritte ab L−9 +5, ab L−19 +20, darunter +40; 3-min-Warten ab L−4 · Heizmodus und Startpause bis L · Ausnahme unter L−9 · Temperatur-Test beginnt bei ≤ L+1, **Abbruch fest 96 °C**. **Fest:** Lüfterschutz während der Takt-Messung wie bisher (ab 85 °C, Rückkehr 80–84). **Wechsel im Betrieb:** neue Grenzen ab der nächsten Runde, **kein Start, kein Taktsprung, kein Heizmodus**; Lernperiode endet, Aufenthalt neu gemessen; Korrekturphase, Taktsperre, Heizmodus, Startpause laufen mit den neuen Grenzen weiter. **Lernwerte je Bereich** (`learned` = aktiver Bereich, `learned_zb` = Archiv „L-U“, `zb` = übernommener Bereich): zurück im alten Bereich gelten wieder seine Lernwerte, ein neuer beginnt mit dem Initialwert; Zustand ohne `zb` (vor v2.2.0): Lernwerte gehören zu 82–85. STATE_VERSION unverändert (würde alle Lernwerte löschen). Warnungen an den Nutzer: bei 2 Grad Breite kann das Pendeln (Fehler 23) zurückkommen; über 85 °C keine Daten zur Fehlerrate; hoher Bereich → Heizmodus bis knapp darunter, Test bricht meist bei 96 °C ab. **Deploy: erst `migrate`, dann Data-Service neu starten** (der SELECT liest die neuen Spalten). |
| Start (Basisstufe gewählt, Automatik ein, Neustart des Miners, nach Wartung, nach Takt-Messung, nach Over Hot) | Starttakt = **Lernwert der Basisstufe**, sonst **Initialwert** (**seit v2.2.2 (Nutzer 28.10.): Stufe 1 = 170, 2 = 250, 3 = 330, 4 = 380, 5 = 400, 6 = 420, 7–10 = 440 MHz** (vorher 100 … 190, je +10; 5–7 geschätzt, ohne Messung); fehlt ein Lernwert im eingestellten Bereich: Lernwert derselben Stufe aus dem Bereich mit der nächstliegenden Mitte (Gleichstand: der kühlere), sonst Initialwert; Starttakt nie über der Obergrenze der Stufe); Modus nach Tabelle ohne Spielraum; Lüfter = Basisstufe – **Heizmodus** (v2.1.7, Nutzer 26.10.; **seit v2.2.0 bis zur unteren Grenze L** (zuerst 79 °C), Nutzer 27.10.: „-5 grad rausnehmen“, vorher 74 °C): bei jedem Start Lüfter **Stufe 1**, bis die Chiptemperatur **79 °C** (untere Grenze des Zielbereichs) erreicht, dann Basisstufe (`heat`, Ereignisse `heizmodus` „Heizmodus: Lüfter Stufe 1 bis 79 °C“, `heizmodus_ende` „Heizmodus beendet: 79 °C erreicht, Lüfter Stufe X“, Status-Karte: Wert „Heizmodus“ statt „Normalbetrieb“, darunter „Lüfter Stufe 1 bis L °C“ (v2.2.1, Nutzer 27.10.)); schon 79 °C oder wärmer → sofort Basisstufe; ohne gültige Temperatur beim Start → Heizmodus, Ende beim ersten gültigen Wert ab 79 °C; kein Zeitlimit; Temperatur-Test oder Start während der Takt-Messung (Lüfter auf Basisstufe) beenden ihn ebenfalls; **ohne Regelung endet er sofort** – Automatik aus, Miner antwortet nicht, Over Hot → Lüfter auf die Basisstufe (Befehl wird bei Fehler wiederholt – bei Automatik aus fragt `run_fan_automation` die Zeile dafür weiter ab, solange `heat_fan_due`; Neustart über den Knopf, solange erwartet: bleibt; Nutzer 26.10.); ein Start während eines laufenden Temperatur-Tests schaltet ihn nicht ein (v2.1.8). Braucht keine gültige Temperatur. **Startpause** (v2.0.7, Nutzer 25.10.): nach einem Start mit kalten Chips (**Neustart des Miners, nach Wartung, nach Over Hot** – Flag `start_cold`, gilt auch, wenn in derselben Runde ein anderer Startgrund dazukommt) **keine Takterhöhung bis die Chiptemperatur 79 °C erreicht, höchstens 10 min** ab dem Start (v2.0.8, Nutzer 26.10.; v2.0.7: 3 min – zu kurz, die A15 wird bei 253 MHz nur ~1 °C/min wärmer und bekam bei 77–78 °C noch +5) (`start_hold`, Ereignis `startpause` „Startpause: keine Takterhöhung bis 79 °C, spätestens HH:MM“, Ende `startpause_ende` „Startpause beendet: 79 °C erreicht (nach X min)“ bzw. „… 10 min vorbei (Chiptemperatur Y °C)“ (v2.1.0), Seite „keine Takterhöhung bis 79 °C, spätestens HH:MM“); Absenkungen ab 85 °C und Lüfter wie immer. Grund: 25.10. 23:39 meldete der Miner nach dem Neustart 26 °C → +40 MHz → 85 °C nach 5 min. Nicht bei Basisstufe gewählt, Automatik ein, nach Takt-Messung (Chips warm). |
| Hohe Chiptemperatur | ab 85 °C: Lüfter Basis +1 · ab 86: +1 · **Takt bei 85–87 °C nach dem Aufenthalt in 79–84 °C davor** (v2.0.6, Nutzer 25.10.: gemessen ab dem letzten Eintritt in 79–84 °C, von unten oder oben, bis zum ersten Wert über 84 °C; ohne Aufenthalt 0): **unter 5 min −10 MHz, sonst −5 MHz** (v2.1.6, Nutzer 26.10.; vorher 10 min); der Wert gilt für alle Absenkungen bei 85–87 °C bis zum Ende der Korrekturphase (`band_in`, `band_dwell_s`, `corr_dwell_s`; Protokoll „Chiptemperatur 85 °C nach 4 min in 79–84 °C: Takt −10 MHz“) · ab 88: +2, −30 · ab 90: +2, −50 · ab 92: +2, −80 · ab 94: +2, −100. Lüfter **sofort**, relativ zur Basisstufe, nie addieren; **niedrigere Korrekturstufe erst nach 30 s** Bedarf. Takt beim Eintritt in die Korrekturphase sofort, danach erst nach dem 30-s-Beobachtungsfenster und nur bei nicht fallender Tendenz, jeweils vom dann gesetzten Takt. |
| **Fehlerrate, Obergrenze, Ertrag, Neustart** (v2.2.2, Nutzer 28.10.) | Anlass: Nacht 27./28.10. kroch der Takt bei Stufe 2 in 23 Schritten von 243 auf 358 MHz (je ein 5-s-Wert von 81 °C), die Temperatur blieb 82–83 °C, die Fehlerrate (DH) stieg von 1,5 auf 42 %, Hashrate 131 → 50 TH/s. **Die Fehlerrate hängt am Takt, kaum an der Temperatur** (Stufe 2, Modus 04: ≤ 258 MHz ≤ 2 %, Hashrate-Maximum 270–285 MHz mit 133–134 TH/s, ab ~305 MHz steil); kleine Senkungen ohne Neustart halfen nicht zuverlässig, **ein Neustart setzte die Fehlerrate zurück** (27.10. 21:07 bei gleichem Takt 5 → 1,5 %). Regeln: **Obergrenze** je Basisstufe (`caps`, gilt in jedem Zielbereich, dauerhaft, Knopf „Obergrenzen löschen“ → Nutzer-Ereignis `obergrenzen_loeschen`, der SELECT holt die Zeile dafür auch bei Automatik aus): DH 5 min durchgehend **über 4 %** bei unverändertem Takt → Obergrenze = gesetzter Takt −5, **kein Senken** (Ereignis `obergrenze`); nicht in den ersten 10 min nach einem Start (`start_at`; alter Zustand ohne `start_at` bekommt ihn in der ersten Runde), nicht während Takt-Messung, Test, Wartung, Over Hot, ohne Antwort. **Ertrags-Prüfung** nach jeder vollen +5-MHz-Erhöhung (nicht +20/+40, nicht in den ersten 30 min nach einem Start): 10 min keine weitere Erhöhung, dann Ø Hashrate der letzten 5 min gegen die **15 min** vor dem Schritt (v2.2.4; vorher 5 min – der kurze Firmware-Einbruch etwa alle 30 min lag sonst im Vorher-Fenster und machte den Ertrag zu groß); unter +0,5 TH/s → Obergrenze = Takt davor; unter −0,5 TH/s → zusätzlich zurück auf den Takt davor (Ereignis `ertrag`; kleinere Unterschiede sind Messrauschen: bei festem Takt ±0,2 TH/s); Abbruch bei anderer Taktänderung oder Pause. **Automatischer Neustart** (`neustart_auto`, cgminer `ascset|0,reboot,0` aus dem data-service): **seit v2.2.4 zusammen mit der Obergrenze, also DH 5 min über 4 % bei unverändertem Takt** (Nutzer 28.10.: die eigene 10-%-Regel entfiel, „für die jetzigen Daten eine blöde Grenze“; eine Obergrenze allein senkt nicht, der Takt blieb bei 295 MHz mit 5 % Fehlern), frühestens 30 min nach einem Start, **höchstens einer je 3 h**, nicht während Wartung, Takt-Messung, Test, Over Hot, ohne Antwort; gesperrt → nur Obergrenze (Grund im Eintrag, `dh_block`); danach Start mit Lernwert ≤ Obergrenze. **Obergrenze begrenzt auch die Lernwerte** der Stufe (aktiver und gemerkte Bereiche, beim Setzen und beim Speichern eines neuen Lernwerts; v2.2.4). **Lernwerte nur bei Ø Fehlerrate unter 4 %** (sonst verworfen, `lernen_ende` „verworfen“), mit Ø Hashrate (`ths_avg`) und Effizienz (`j_th` = Ø Leistung / Ø Hashrate, J/TH; v2.2.3). Der schlechte Lernwert Stufe 2 = 358 MHz (82–85) wurde beim Deploy gelöscht. |
| **Erst Lüfter, dann Takt** (v2.2.5, Nutzer 28.10.) | Bei **U+1 bis U+3** (Tabelle ohne Taktwert) beginnt die Korrekturphase wie bisher mit **Lüfter +1 sofort**, der **Takt sinkt aber erst, wenn die Temperatur 60 s ununterbrochen über U liegt** (`HOT_CLOCK_WAIT_S`, gezählt ab dem Lüfter +1 = Beginn der Korrekturphase, `above_at`; eine Lücke ohne Messwerte setzt neu an). Ein einzelner Wert ≤ U vorher → **keine Taktsenkung** für diesen Ausflug (Hinweis „Chiptemperatur wieder X °C nach N s: keine Taktsenkung“), der Lüfter kehrt nach der Rückkehr-Regel zurück; ein neuer Ausflug in derselben Korrekturphase wartet wieder 60 s. Erste Senkung dann wie bisher (−10/−5 nach Aufenthalt), weitere wie bisher nach Fenster und Tendenz. **Ab U+4 sofort** nach Tabelle. Grund: alle 41 Hitze-Eingriffe seit 25.10. lagen bei U+1 bis U+3 und waren mit Lüfter +1 nach höchstens 60 s (40× unter 30 s, meist 10–15 s) wieder ≤ U – jede kostete unnötig 5–10 MHz (28.10. 13:59/14:04 nach dem Neustart: 275 → 255 MHz). **Die Lüfterstöße oben (v2.2.1) sind entfallen** (`drop_hot_pulses` räumt alte Zustände auf), die Stöße unten bleiben. Simulation (Schätzmodell, Raum +15 °C): der Lüfter wechselt dann öfter zwischen Basisstufe und +1 (~85 Befehle/h), Takt wird kaum gesenkt – im echten Betrieb begrenzen Ertrags-Prüfung und Fehlerrate den Takt; beobachten. |
| **Lüfterstoß** (v2.2.1, Nutzer 27.10.; **oben entfallen seit v2.2.5**) | Ab U+1 bis U+3 (Tabelle ohne Taktwert) **vor der Korrekturphase zwei Lüfterstöße**: Lüfter Basisstufe +1, **6 s ab der Bestätigung** der Lüftersteuerung (Lesewert nach dem Befehl; praktisch 10 s, Runde 5 s), zurück auf die Basisstufe, nach deren Bestätigung Stoß 2; **ohne Bestätigung nach 30 s weiter** (vom Nutzer freigegeben). Kein Takt während der Stöße; laufende Befehle (z. B. Starttakt) gehen weiter, verworfen wird erst beim Eintritt in die Korrekturphase (Review 27.10.: sonst liefen gesetzter Takt und Miner auseinander). **Zurück in L–U → Ende, keine Senkung**; **ab U+4 → Abbruch, sofort Tabelle**; nach beiden Stößen noch über U → Korrekturphase wie bisher (Lüfter +1, −10/−5 nach Aufenthalt). Auch direkt nach einem Start (Automatik ein, Basisstufe, Ende Takt-Messung/Test) – Nutzer: „Auch dann erst Stöße“. **Gegen Pendeln an U** (Nutzer: „Beides“): höchstens **ein Stoßpaar je 10 min** und neue Stöße erst nach einem Wert **≤ U−1**; sonst gleich die Korrekturphase (Simulation Raum +15 °C ohne Grenze: ~600 Lüfterbefehle/h und keine Senkung; mit: 1 Stoßpaar, 14 Lüfterbefehle). Nicht während Takt-Messung und Temperatur-Test (deren Beginn beendet laufende Stöße); start() beendet sie; Automatik ein vergisst Stöße und Sperre. Zustand `stoss` {n, step an/aus, at, level, ok}, `stoss_at`, `stoss_frei`; Ereignisse `luefterstoss` „Lüfterstoß 1/2: Stufe X für 6 s (Chiptemperatur T °C)“, `luefterstoss_ende` „… wieder im Zielbereich / abgebrochen: T °C – Tabelle / fertig: T °C, Takt wird gesenkt“; Status-Karte „Lüfterstoß n/2“. |
| Korrekturphase | aktive Stufe > Basisstufe. **Keine Plus-Schritte.** Rückkehr zur Basisstufe nach **1 min stabil in 80–84 °C** oder **sofort unter 80 °C**. |
| **Erhöhen erst nach 120 s, Lüfterstöße unten** (v2.2.2, Nutzer 28.10.) | Takterhöhung nur, wenn die Temperatur **mindestens 60 s durchgehend unter L** (v2.2.4; zuerst 120 s – am 28.10. lag die Temperatur nie länger als 45 s unter L, der Takt wäre nach Hitze-Senkungen bei 245 statt 270–275 MHz geblieben, rund 3,5 TH/s weniger) liegt (ein Wert ≥ L setzt zurück). **Bei genau L−1 vorher zwei Lüfterstöße nach unten** (Basisstufe −1, 6 s ab Bestätigung, ohne Bestätigung nach 30 s weiter, gleiche Regeln wie oben gespiegelt): wieder ≥ L → Ende ohne Erhöhung; ≤ L−2 → Abbruch und sofort Erhöhung; nach beiden noch L−1 → +5; höchstens ein Paar je 10 min, neue erst nach ≥ L+1; nicht bei Basisstufe 1, Heizmodus, Startpause, Korrekturphase, Ausnahme, Takt-Messung, Test, an der Obergrenze; ohne Regelung (Automatik aus, keine Antwort, Over Hot) enden sie wie der Heizmodus (Lüfter auf Basisstufe). |
| Feintuning (v2.0.6, Nutzer 25.10.) | **79–84 °C: keine Taktänderung** (die früheren −1/−2 bei steigender Tendenz und +1 bei 79 °C entfallen). **Kleinster Taktschritt 5 MHz**: unter 79 °C 70–78 → **+5** · 60–69 → +20 · < 60 → +40; Plus nur bei nicht steigender Tendenz, nicht in der Korrekturphase; „eine Größe kleiner“ entfällt. **Bei 75–78 °C frühestens 3 min nach der letzten Takterhöhung** (v2.1.6, Nutzer 26.10.; gezählt ab der Übernahme der letzten Erhöhung der Regelung, egal bei welcher Temperatur, nur wenn der Takt wirklich stieg, `last_plus_at` in `clock_done`; unter 75 °C wie bisher nach dem 30-s-Fenster). **Taktsperre:** war die letzte Taktänderung bei über 84 °C (Absenkung ab 85 °C) und fällt die Chiptemperatur unter 79 °C → **5 min keine Taktänderung**, gezählt ab dem ersten Unterschreiten, **einmal je Taktänderung** (`calm`, `calm_for`); über 84 °C endet sie sofort (Ereignisse `taktsperre` „Taktsperre 5 min bis HH:MM“, `taktsperre_ende`; Seite: „Taktsperre bis HH:MM“). Grund (Nutzer): häufige Taktwechsel erhöhen vermutlich die Fehlerrate und senken die Hashrate. |
| Beobachtungsfenster / Tendenz | **30 s** nach jeder bestätigten Taktänderung. Tendenz = jetzt gegen den Wert von vor höchstens 30 s (bei jüngerer Taktänderung gegen deren Wert). |
| Grenzen | 100–440 MHz; ein berechneter Takt unter 100 → **nicht senden**, Eintrag `fehler`, Takt bleibt. |
| Modus | 0–360 mode 04 · 361–380 03 · 381–400 02 · 401–420 01 · 421–440 00. **Spielraum 20 MHz nur nach unten** (abwärts erst bei 340/360/380/400; v2.1.7, Nutzer 26.10.; vorher 10 MHz). Normal: erst Modus, Übernahme, dann Takt. **Bei hoher Chiptemperatur: Takt sofort, dann Modus, dann Takt erneut** (der Moduswechsel stellt den Standardtakt ein). Moduswechsel durch den Nutzer: Automatik arbeitet weiter. |
| Senden / Übernahme | Vor jedem Taktbefehl Zielpunkte mit den gemeldeten SF0-Punkten vergleichen: **gleich → kein Befehl**. Übernommen: Modus = Miner meldet ihn; Takt = Miner meldet den gesetzten Wert **oder** einen anderen Wert als vorher. Ohne Übernahme **nach 20 s erneut, bis es klappt** (jeder Versuch im Protokoll). |
| Ausnahme | Takt = 440 MHz und **3 min** stabil unter 70 °C → aktive Stufe −1 (nicht unter 1), danach erneut 3 min; Ende über 70 °C, **Stufe bleibt reduziert**; zurück nach oben nur über „Hohe Chiptemperatur“. |
| Lernen | **15 min** auf derselben Lüfterstufe → Lernwert der **aktiven Stufe** = **Mittelwert des gesetzten Takts** der 15 min, Temperaturmittel, Zeit, Anzahl; jede weitere Periode ersetzt ihn. **Lernbereich 79–84 °C** (v2.0.6, Nutzer 25.10.: wandert mit dem Bereich ohne Taktänderung; vorher 80–84). **Taktschritte setzen die Lernzeit nicht zurück, Ausflüge aus 79–84 °C bis 60 s auch nicht** (Nutzer 25.10., v2.0.1: vorher entstand in 2 h kein Lernwert – 65 von 71 Schritten starteten die Periode neu, dazu 33 kurze Ausflüge auf 79 °C). Neu beginnt sie bei Lüfterwechsel, Moduswechsel, ab 85 °C oder > 60 s außerhalb 79–84 °C. Keine Übertragung zwischen Stufen, keine Jahreszeiten-Logik. Start ohne Lernwerte (alte Lernwerte je Modus beim Umstieg verworfen). |
| Situationen | Neustart → Start (**„nicht erreichbar“ rund um einen Neustart ist kein Fehler**, v2.0.8, Nutzer 25.10.: Knopf „A15 neu starten“ → bis 5 min `neustart_offline` „A15 startet neu – nicht erreichbar (erwartet)“ / `neustart_online` „… wieder erreichbar nach X s“, Status-Karte „Neustart · A15 startet neu · gesendet HH:MM“ (`restart_req` aus dem letzten `neustart_nutzer`); **antwortet die A15 auf den Neustart-Befehl nicht** (Verbindung stand, Befehl gesendet, dann Timeout/Abbruch), gilt er als gesendet: kein „Miner nicht erreichbar: timed out“, sondern Eintrag `neustart_nutzer` mit Antwort „keine Antwort – Miner startet neu“ (v2.2.1, Nutzer 27.10.; nur wenn die Verbindung gar nicht zustande kommt: Fehler); Neustart auf anderem Weg → beim Erkennen werden `miner_offline`/`miner_online` der 3 min davor umbenannt (`net_log`; beim Knopf nur ab dem Klick; ein „wieder“ nimmt sein „antwortet nicht“ mit, auch wenn der Ausfall länger als 3 min war – Nutzer 26.10.); während der Startpause ebenso. Dauert „nicht erreichbar“ länger als erwartet (Knopf > 5 min, Startpause vorbei) → wie bisher roter `miner_offline`. Technische Meldung bei Neustart-Einträgen in `details.meldung`, nicht in `error` (die Protokoll-Seite zeigt `error` rot). Nach einer Takt-Messung keine Startpause, auch wenn deren Moduswechsel die Laufzeit zurückgesetzt haben (v2.0.9)) · Miner ändert den Takt selbst → erneut senden (frühestens 60 s nach der letzten Taktänderung **und 60 s nach dem letzten erneuten Senden**, v2.0.8, Nutzer 26.10.: nach dem Hochfahren setzt die Firmware erst 360 MHz und senkt dann schrittweise – vorher 3× in 10 s gesendet) · Over Hot → keine Entscheidung bis gültige Werte, danach Start · Miner antwortet nicht → Lüfter unverändert, warten · Lüftersteuerung antwortet nicht/andere Stufe → erneut senden, bis es klappt · Takt-Messung → Taktregelung pausiert, Lüfter nur nach „Hohe Chiptemperatur“, danach Start · Wartung → wie bisher (unten) · Automatik aus → **kein Takt gesendet, Takt bleibt** · Drosselung abgeschafft. |

**Technische Festlegungen (vom Nutzer bestätigt bzw. aus v1.0.2 übernommen):**
- „Miner ändert den Takt selbst“ = Takt laut Miner **> gesetzt + 25 MHz**, frühestens **60 s** nach der letzten Taktänderung
  (`OVERRIDE_MHZ`, `OVERRIDE_AFTER_S`; v1.0.8 – vorher hielt v1.0.7 die Rampe der Firmware für eine Änderung, Fehler 20).
- Hohe Chiptemperatur während eines laufenden Befehls → die Taktsenkung **ersetzt** ihn (Priorität Sicherheit).
- Neustart durch den **eigenen** Moduswechsel (bis 120 s danach) ist kein „Neustart des Miners“ (`OWN_SWITCH_S`).
- Nach einem Moduswechsel wartet der nächste Taktbefehl, bis die Firmware den Standardtakt eingestellt hat (Punkte geändert,
  Neustart oder 120 s; `settle`). Standardtakt eines Modus: beim eigenen Wechsel gemerkt (`state.stock`), sonst Throttle-Profil –
  nur wenn ±15 MHz am Tabellenwert; fehlt er, aus den gemeldeten Punkten geschätzt (Eintrag `fehler`, Grund `standardtakt_fehlt`).
- Lüfter: neue Stufe sofort; erneut senden frühestens 20 s nach dem letzten Versuch; Rücklesen alle 60 s bzw. 10 s nach dem Setzen,
  nach Lesefehler erst wieder nach 60 s. Lüfter wird vor den (langsamen) Miner-Befehlen gesetzt.
- **Einstellungsseite der A15** (v2.1.2, Nutzer 26.10.; vorher „Miner antwortet nicht“ – 26.10. 04:31–04:41 war die Automatik
  9,5 min blind, die Mining-Schnittstelle antwortete): Antwortet nur die Seite nicht, wirft `calibration.read_miner` nicht mehr,
  sondern liefert die zuletzt gelesenen Einstellungen (`_last_conf`, als `_stale` markiert, `conf_fresh`=False, `web_error`; ohne
  gelesene Einstellungen seit dem Start `conf`/`mode_key` None – die Automatik nimmt dann ihren letzten `mode_key` aus dem Zustand).
  Die Automatik regelt normal weiter. **Ein Moduswechsel wartet außerhalb der Befehlsfolge** (`mode_wait` = gewünschte Stufe,
  Ereignis `modus_wartet` „Moduswechsel wartet: Einstellungsseite nicht erreichbar“ einmal je Ausfall); die Taktbefehle laufen
  weiter, auch weitere Absenkungen ab 85 °C (Review 26.10.: in der Folge blockierte er sie); ein Takt über dem Standardtakt des
  jetzigen Modus wird auf diesen begrenzt und Plus-Schritte darüber warten (sonst stieg der gesetzte Takt, während der Miner beim
  Standardtakt blieb). Antwortet die Seite wieder, wird der Modus passend zum dann gesetzten Takt nachgeholt. Nach einem gesendeten
  Umschaltbefehl tragen die zuletzt gelesenen Einstellungen den neuen Modus. HTTP-Fehler der Seite (z. B. IncompleteRead) gelten wie
  eine Zeitüberschreitung (sonst brach der ganze Live-Zyklus ab). Takt-Messung in `wait_mode`: ohne lesbaren Modus kein Urteil und
  keine Zeitüberschreitung, sie wartet. `a15_set_mode` lehnt alte Einstellungen ab – nie alte Pool-Daten. Takt-Messung:
  wartet nur dort, wo ein Umschaltbefehl gesendet würde (`_send_mode`, Wiederholung in `wait_mode`; Meldung „Moduswechsel wartet …“),
  Abbrechen/Ende ohne Moduswechsel laufen durch. Protokoll erst nach **2 min**: `einstellungsseite_aus` „A15-Einstellungsseite
  antwortet nicht – Mining-Schnittstelle ok“, zurück `einstellungsseite_an` „… wieder erreichbar nach X min“ (nicht rot). Rot
  „Miner antwortet nicht“ nur, wenn die Mining-Schnittstelle (estats/summary) selbst nicht antwortet.

**Temperatur-Test** (v2.0.6, Nutzer 25.10.): Button „Temperatur-Test starten“ auf der Lüfter-Seite (`fan_automation.temp_test_at`,
Migration 0025). Ablauf: **Takt auf den Lernwert der Basisstufe** (sonst Initialwert wie beim Start), warten bis TMax **≤ 80 °C**;
dann **zweimal +30 MHz** (z. B. 319 → 349 → 379), je **nach Übernahme des Takts 2 min warten, dann 20 min messen**; danach
Normalbetrieb (Start „nach Temperatur-Test“ mit dem Lernwert). Modus nach der allgemeinen Regel (379 MHz = mode 03: erst Modus,
dann Takt; Zieltakt = Standardtakt → kein Taktbefehl); die 2 min beginnen erst nach der Übernahme des Takts nach dem Moduswechsel.
Während des Tests: Takt fest (keine Taktregelung), Lüfter auf der Basisstufe (keine Korrektur), kein Lernen. Abbruch ab **96 °C**,
Over Hot, Takt-Messung, **Neustart des Miners**, **Wahl der Basisstufe** (beide nur bei laufendem Test – sonst setzt der Start mitten
in der Messung den Lernwert; vor dem Beginn setzen Start und Test-Beginn denselben Lernwert),
Automatik aus, Wartung oder „Test abbrechen“; nach einem Abbruch gelten sofort die normalen Regeln (ab 85 °C Korrekturphase).
Zustand `state.test`: `step` (0 = Beginn, 1, 2), `clock`, `phase` (`einstellen`, `beginn`, `warten`, `messen`), `wait_ends`,
`measure_from`, `measure_ends`. Ereignisse `test_start`, `test_schritt` („Schritt 1/2: Takt 349 MHz (+30 MHz)“), `test_messung`
(„Messung läuft bis HH:MM“, Details `testschritt`, `takt`, `von`, `bis`), `test_ende` (Grund; ein Abbruchgrund geht vor „fertig“). Auswertung je Schritt: `automation_cycle` von `measure_from` bis
`measure_ends` (DH, GHSspd, Leistung, J/TH, TMax).
Vorgeschichte: v2.0.4/2.0.5 regelten auf feste Ziele 84–92 °C mit 1-MHz-Schritten. Ergebnis 25.10. (Mittel): vorher 80,5 °C/320 MHz
DH 13,8 %, 123 TH/s, 18,0 J/TH; 83,1 °C/340 MHz DH 22,3 %, 113 TH/s, 20,5 J/TH; 84,1 °C/352 MHz DH 27,8 %, 100 TH/s, 23,8 J/TH.
Temperatur und Takt stiegen dabei gemeinsam (Lüfter fest) – nicht trennbar. Nutzer: „diese einzelne Taktänderung ist nicht gesund“
→ Test abgebrochen, neuer Ablauf v2.0.6.

**Wartung** (unverändert): Button „Wartung starten (A15 ausschalten)“ (`maintenance_at`, Migration 0023; ein laufender Takt-Lauf
wird pausiert). Antwortet der A15 nicht mehr → Lüfter **aus** (`atType` 1); antwortet er wieder → Lüfter **an auf die Stufe von
vorher**, Wartung endet, dann Start. Keine offline/online-Meldungen während der Wartung und 15 min danach (`maint_ended_at`).

**A15 neu starten** (v2.0.6, Nutzer 25.10.): Button neben „Wartung starten (A15 aus)“ mit Rückfrage; nutzt den vorhandenen
`POST /api/devices/<id>/restart/` (`ascset|0,reboot,0`, im Canaan-Quellcode der A15 vorhanden, Verzögerung 0–300 s; die A15 meldet
„Privileged access OK“). Für die A15 der Automatik Nutzer-Ereignis `neustart_nutzer` „Nutzer: A15-Neustart gesendet“. Danach erkennt
der Data-Service den Neustart (Laufzeit zurückgesetzt) → Start mit dem Lernwert; ein laufender Temperatur-Test bricht ab.

**Zustand** `fan_automation.state` (nur der Data-Service schreibt; `v` = 2): `phase` wird je Runde abgeleitet (messung > korrektur >
ausnahme > normal; wartung, aus), Flags `corr`, `exc_active`, `cal`, `start_reason`, dazu `t`, `tendency`, `clock_set`,
`clock_changed_at`, `clock_now`, `mode_key`, `stock_mhz`, `fan_target`, `fan_set`, `fan_read`, `pending`, `queue`, `settle`,
`window_until`, `learn_since`, `learned` (je Stufe), `log`. Einstellungen (`enabled`, `ac_dev_id`, `ac_port`, `level`, `level_at`,
`maintenance_at`) schreibt das Backend. Endpunkt `GET/POST /api/fans/automation/` (`enable`, `level`, `disable`,
`maintenance_start`, `maintenance_end`).

**Simulation:** `data-service/tests/test_fan_automation.py` enthält zwei Simulationen mit einem **Schätzmodell** (keine Messwerte;
Nutzerentscheidung 24.10.). 60 Regel-Tests, je einer pro Regel/Befund.

### 7.9 Zentrale Abfrage & Live-API (`data-service/live_reader.py`, `miner_cache.py`, `backend/api/live_views.py`, `frontend/src/lib/LiveContext.jsx`)
- **Ein Leser alle 5 s**: `estats` + `summary` je Avalon (parallel, ein ausgeschaltetes Gerät bremst nichts), A15-Webinterface alle 30 s
  bzw. sofort nach eigenem Umschalten. Werte in `miner_cache` (je Gerät, mit Zeit; **eine Anfrage gleichzeitig je Gerät**; jeder
  Schreibbefehl löscht die Werte des Geräts).
- Direkt danach: Automatik (jede Runde), Drosselung + Takt-Tabellen (jede 3. Runde, eigener Worker). Niemand liest sonst selbst;
  Rückfall: eigene Abfrage, wenn der Wert > 15 s alt ist.
- Last am A15 (gemessen 21.10., 60 s): **24 cgminer + 1 Web** pro Minute (ursprünglich 32 + 10, kurz vor dem Umbau ~38 + 6).
- **`GET /api/live/`** (nur DB, ~120 ms, ~15 KB): Geräte, mining/hardware/system latest, Drosselung je Avalon, Automatik,
  Takt-Lauf. Browser fragt alle 5 s einmal für die ganze Oberfläche.


### 7.10 Protokoll (`data-service/automation_journal.py`, `backend/api/automation_log_views.py`, `pages/ProtocolPage.jsx`)
Nutzer 24.10.: **alles sammeln**, 360 Tage behalten.
- Tabelle **`automation_cycle`**: ein Eintrag **je 5-s-Zyklus** (Phase, Chiptemperatur, Boards, Tendenz, gesetzter Takt, Takt laut
  Miner, Punkte, Modus, Basis-/aktive/gelesene Lüfterstufe, Leistung, GHSspd, DH, Status, Fenster, Lernperiode, laufender Befehl,
  Entscheidung, Verweis auf den Ereignis-Eintrag). ~17.000 Zeilen/Tag, ~1,5 GB/Jahr.
- Tabelle **`automation_log`**: ein Eintrag je **Entscheidung, Befehl und Versuch** (Arten siehe `kind`: start, takt, modus, luefter,
  hohe_temperatur, rueckkehr, ausnahme(_ende), stabilitaet_start/_ende, lernen_start/_ende, lernwert, neustart, over_hot,
  miner_offline/_online, firmware_takt, messung_start/_ende, fehler; Nutzer: basisstufe, automatik_ein/_aus, wartung_start/_ende).
  Mit Auslöser, Chiptemperatur, alt → neu, Befehl/Endpunkt, Antwort (**nie Zugangsdaten**: Modusbefehl ohne Pool-Felder),
  Ergebnis, **API-Zeit**, **Übernahmezeit** (erste Meldung des Miners/der Lüftersteuerung; abgelöste Befehle „nicht gemessen“) und
  **thermischem Verlauf** (10 Werte, alle 30 s ab der Übernahme).
- Schreiben: eigene dauerhafte DB-Verbindung (autocommit); fehlt eine Tabelle → eine Warnung, 10 min Pause; Fehler stoppen nie die
  Regelung. Aufräumen jede Nacht 03:30 (> 360 Tage, in Blöcken).
- API (angemeldet): `GET /api/automation/log/` (Filter `kind`, `ok=true|false|null`, `from`, `to`, 100 je Seite),
  `/api/automation/log/<id>/`, `/api/automation/log/stats/` (Moduswechsel, Taktwerte, Lüfterstufen: Anzahl, API-/Übernahmezeit
  Mittel/Median, Δ Temperatur 1/3/5 min), `/api/automation/cycles/` (> 6 h je Minute verdichtet), `/api/automation/export.csv?table=log|cycle`.
- Seite **„Protokoll“** (Menü nach „Lüfter“): Reiter **Ereignisse** (Karten/Tabelle, Filter Art/Ergebnis/Zeitraum, Detailansicht,
  alle 10 s), **Reaktionszeiten**, **Verlauf** (Kurven Chiptemperatur, Takt gesetzt/laut Miner, Lüfterstufe; Zustand als Hintergrund,
  Ereignisse als Linien; alle 30 s; „frei“ höchstens 7 Tage) und **CSV-Export**.

---

### 7.11 Stundenwerte & Aufräumen (`data-service/stats_rollup.py`, Tabellen `device_stats_hourly`, `pool_stats_hourly`; v2.1.3, Nutzer 26.10.)
- Anlass: Analytics mit 30 Tagen brauchte beim ersten Aufruf **11,8 s** (Rohwerte alle 10 s, ~1,2 Mio. Zeilen je Aufruf), danach 2–3 s.
- **Stundenwerte je MinerGerät** (Ø/Max/Min Hashrate, beste Difficulty, Shares min/max, Ø/Max Temperatur, Ø/Max Chiptemperatur,
  Ø/Max Leistung, Ø Effizienz, Anzahl Werte, erster/letzter Zeitpunkt; je Mittel die Zahl der Werte ohne NULL `*_n` als Gewicht – live
  hatten 34 Stunden fehlende Chiptemperaturen) und **je Pool-Konto** (Ø Hashrate 1 min/1 h/24 h, Worker max, Anzahl). Data-Service-Job
  `stats_rollup` alle 5 min, ab einer Stunde vor der letzten gespeicherten (verspätete Werte; die laufende Stunde wird später ergänzt); beim ersten Lauf alle Rohwerte (Nachrechnen ~2,6 s auf dem VPS). Geprüft 26.10.: Stundenwerte = Mittel der Rohwerte.
- **Analytics** (`views.device_analytics`) liest über 24 h Zeitraum nur noch die Stundenwerte (`_hourly_device_view`, Mittel nach Anzahl
  gewichtet; ab 7 Tagen je Tag zusammengefasst; ganze Stunden ab der Stunde des Beginns bis vor die Stunde des Endes); bis 24 h wie bisher
  aus den Rohwerten.
- **Rohwerte nur 30 Tage** (`device_mining_stats`, `device_hardware_stats`, `pool_stats`): Job `prune_raw_stats` jede Nacht 03:40 – erst
  Stundenwerte nachrechnen, dann nur Rohwerte löschen, deren Stunde in den Stundenwerten steht (in Blöcken). Die alte
  Aufbewahrungs-Einstellung (`metrics_retention_days`, 90 Tage) gilt weiter für alle Tabellen, also wirksam für `device_system_info`
  (Chiptemperaturen je Board, Fehlerrate – nicht in Stundenwerten, darum nicht nach 30 Tagen gelöscht).

## 8. Integration des Originals 1.2.0

Übersicht = eigene Einzelansicht + `FleetAlertsBar`; Original-Gerätesteuerung bleibt, während Takt-Lauf gesperrt; Navigation
Original + „Activity“ + „Takt-Tabellen“ + „Lüfter“; Dockerfiles/Compose Original mit voll qualifizierten Images.

---

## 9. Datenbank und Migrationen

- Eigene Tabellen: `calibration_runs`, `calibration_points` (0020), **`fan_automation`** (**0021**, handgeschrieben, hängt an 0020;
  **0022** `level_at`, **0023** `maintenance_at`, **0025** `temp_test_at`), **`automation_log`, `automation_cycle`** (**0024**, mit Datenmigration: alte
  A15-Drosselung entfernen). Der Device-FK der beiden Protokoll-Tabellen hat keinen eigenen Index (`db_index=False`, der
  Verbundindex device + Zeit deckt ihn ab).
- `calibration_points.kind`: `max`, `step`, `manual` (nur noch alte Punkte); `data` enthält u. a. `fields`, `summary`, `web`,
  `estats_raw`, **`fan_level`**, **`power_spread_pct`**.
- `makemigrations --check` meldet eine ausstehende Migration des **Originals** (Index-Umbenennungen) – nicht ungefragt erzeugen.
  Eigene Migrationen deshalb von Hand schreiben.

---

## 10. Fehlerhistorie und Lehren

1. Erzwungene Takte bei der Max-Messung → falsche Werte. Firmware-Takte nie überschreiben, wenn „Standard“ gemessen wird.
2. „Verbindungsabbruch = gesendet“ angenommen → Umschalten ging nicht mehr. Immer zurücklesen und wiederholen.
3. Abbrechen sendete alle 15 s erneut → `state.restoring`.
4. 2. Max-Messung ohne Wartezeit. 5. Stabilitätsband halb so groß. 6. Drosselungs-Karte falscher Modus. 7. Falscher „Tipp“.
8. Schaukeln der Drosselung. 9. Selbst Messungen gestartet (verboten). 10. Testskript auf dem Server vergessen. 11. Shell-Quoting.
12. **„Drosselung aufheben“ bei aktiver Automatik sprang auf Standardtakt** (297 → 369 MHz) → TMax 95 °C. Damals: kein Takt beim Aufheben mit Automatik; seit v1.0.7 gibt es die A15-Drosselung nicht mehr.
13. **Lüfter-Rückmeldung zu früh gelesen** (5 s statt > 5 s) → Anzeige hing bis ~1 min, Stufe doppelt gesendet. Jetzt Rücklesen nach 10 s.
14. Automatik-Seite zeigte „Standardtakt“, obwohl ein anderer Takt lief (zeigte nur den selbst gesetzten). Jetzt immer Wert laut Miner.
15. Automatik/Lüfter-Seite hinkten der Übersicht hinterher (60-s-Median, eigene Timer) → zentrale Abfrage + Live-API.
16. **Frontend-Deploy hinterließ hängenden Web-Container** (virtfs) → Seite ~2 min 503. Nach jedem Deploy `podman ps` prüfen; Lösung Abschnitt 13.
17. Lokale Entwicklungsserver wurden von der App beendet → bei „lokal nicht erreichbar“ neu starten (Abschnitt 14).
18. **Abbruch der Messreihe holte Firmware-Takte bei Lüfter Stufe 1–2** (weg/zurück 04→03→04) → 3.355 W, TMax 100 °C. Mit Automatik kein Weg/zurück mehr, Lernwert sofort, Schutz im Regelmodus sofort.
19. **„Ausschalten“ (Automatik) mit „A15 ausschalten“ verwechselt** → Automatik aus setzte den Standardtakt bei Lüfter Stufe 2 → 3.670 W, 111 °C, Over Hot (21.10. 13:24). Jetzt: Button „Automatik beenden“ mit Rückfrage, Takt bleibt, wie er ist, „Wartung starten“ oben daneben.
20. **v1.0.7 hielt die Rampe der Firmware für „Miner ändert den Takt selbst“** (25.10.: 183 → 152 → 100 MHz, Takt doppelt gesendet). Die 60-s-Sperre aus v1.0.2 war beim Neuschreiben verloren gegangen → v1.0.8. Lehre: beim Neuschreiben alle bisherigen Festlegungen (dieser Abschnitt, 7.8) abhaken.
21. Plan der Regelung: falsche Verknüpfungen, Abkürzungen und eigene Annahmen im Plan kosteten mehrere Runden (23./24.10.) → Regel „Pläne/Logik“ in Abschnitt 1.
22. Hintergrund-Agenten brachen am Sitzungslimit ab (25.10.): Teilergebnisse und „widerlegte“ Befunde ohne Stimmen selbst prüfen – 12 von 16 waren echt.
23. **Kein Lernwert in 2 h** (25.10.): Die Chiptemperatur stand an der Grenze 80 °C, das ±1-°C-Rauschen löste alle 1,2 min einen ±1-MHz-Schritt aus, jeder startete die Lernzeit neu. Lösung v2.0.1 (7.8 Lernen); die Unruhe an der 80-°C-Grenze ab v2.0.2 (7.8 Feintuning).

---

## 11. Offene Punkte

1. **Over-Hot-Benachrichtigung per Messenger** (Telegram/Discord/ntfy des Originals prüfen).
2. **±3 % oder ±2 %**: nach einigen Punkten Spalte „Schwankung“ auswerten.
3. Regelung v2 beobachten (Protokoll-Seite): Plus-Schritte, Taktsperre, Korrekturphase, Lernwerte; Stabilitätszeit 1 min (Rückkehr) anhand der
   gemessenen Reaktionszeiten prüfen (Nutzerwunsch aus dem Plan).
4. **Unruhe an der 80-°C-Grenze**: seit v2.0.6 keine Taktänderung in 79–84 °C, kleinster Schritt 5 MHz, Taktsperre 5 min nach Absenkung – live beobachten (Taktwechsel je Stunde, Fehlerrate).
5. Takt laut Miner liegt 13–20 MHz über dem gesetzten (Nachziehen der Firmware); ab > 25 MHz sendet die Regelung neu – beobachten.
6. Einstellungsseite des A15 nicht erreichbar: seit v2.1.2 regelt die Automatik weiter (7.8) – beobachten, wie oft `einstellungsseite_aus` kommt.
7. Allgemeine Zeitraumauswahl („Last 30 days“) erscheint auch auf Protokoll- und Lüfter-Seite, ohne Wirkung dort.
8. Ausstehende Original-Migration – nur nach Rücksprache.

---

## 12. Dateikarte (eigene bzw. stark geänderte Dateien)

```
backend/api/
  views.py           DeviceViewSet: system-latest, calibration (+Automatik beim Start/Fortsetzen), restart, workmode,
                     throttle (Nano/Q; A15 nur Anzeige, set_mhz/set_by), Sperren
  models.py          CalibrationRun, CalibrationPoint, FanAutomation, AutomationLog, AutomationCycle   migrations/0020 … 0024
  automation_log_views.py  /api/automation/log/, …/stats/, /api/automation/cycles/, /api/automation/export.csv
  acinfinity.py      AC-Infinity-Client (Login, Test-Sitzung, Live-Pause, set_fan_level, Sicherungen) – auch vom Data-Service geladen
  fan_views.py       /api/fans/* (status, credentials, backup, restore, test, session, automation)
  live_views.py      /api/live/
data-service/
  app.py             Jobs (live_cycle, poll_all_sources …)
  live_reader.py     zentrale Abfrage 5 s
  miner_cache.py     Zwischenspeicher + Sperre je Gerät
  fan_automation.py  Temperatur-Automatik v2 (Regelung nach Plan, Lernwerte je Stufe)
  automation_journal.py   Protokoll (automation_log, automation_cycle, Aufräumen 360 Tage)
  calibration.py     Takt-Tabellen
  throttle_controller.py  Drosselung Nano/Q (TH/s); A15 nur Profile/Beobachtung
  collectors/avalon_collector.py   Erfassung aus der zentralen Abfrage
frontend/src/
  lib/LiveContext.jsx              gemeinsame Live-Abfrage 5 s
  pages/OverviewDashboard.jsx, CalibrationPage.jsx, FansPage.jsx, ProtocolPage.jsx
  components/devices/ThrottleCard.jsx, OverHotBanner.jsx   components/ui/info-tip.jsx
docs/claude/Regelungslogik-Luefter-Takt.drawio   freigegebenes Diagramm der Regelung (8 Seiten)
docker-compose.yml   Data-Service-Mounts acinfinity_data + backend/api (ro)
ms-build-frontend.sh, ms-recreate.sh   VPS-Hilfsskripte (virtfs)
```

---

## 13. Schnellreferenz Befehle (VPS, als root über `ssh lb-db`)

```bash
cd /opt/minersentinel
podman ps --format '{{.Names}} {{.Status}}' | grep minersentinel          # alle 4 müssen laufen
podman logs --since 5m minersentinel-data-service 2>&1 | grep -E " - ERROR - |Traceback" | grep -v 192.168.178.137
podman logs --since 30m minersentinel-data-service 2>&1 | grep "Temperatur-Automatik:"   # Aktionen der Automatik
podman exec minersentinel-postgres psql -U minersentinel -d minersentinel -tAc "select id,status,step_index,message from calibration_runs order by id desc limit 3"
podman exec minersentinel-postgres psql -U minersentinel -d minersentinel -tAc "select enabled, level, state - 'temps' - 'log' from fan_automation"
# Protokoll der Regelung: letzte Ereignisse / Zyklen
podman exec minersentinel-postgres psql -U minersentinel -d minersentinel -tAc "select recorded_at, kind, ok, trigger, clock_old, clock_new, fan_new, takeover_state, takeover_s, api_ms from automation_log order by id desc limit 20"
podman exec minersentinel-postgres psql -U minersentinel -d minersentinel -tAc "select recorded_at, phase, chip_temp, clock_set, clock_reported, fan_level, decision from automation_cycle order by id desc limit 12"
podman restart -t 5 minersentinel-data-service
rm -f /root/fe-build7.status; (nohup ./ms-build-frontend.sh > /root/fe-build7.status 2>&1 &)
# Anfragen an den A15 zählen (60 s)
timeout 60 tcpdump -i wg0 -nn -q "dst host 192.168.178.149 and tcp[tcpflags] & tcp-syn != 0" | awk '{print $5}' | sed -E 's/.*\.([0-9]+):?$/\1/' | sort | uniq -c
# API ohne Browser (Django-Shell, nichts auf dem Server liegen lassen)
podman exec minersentinel-backend python manage.py shell -c "from rest_framework.test import APIClient; from django.contrib.auth.models import User; c=APIClient(); c.force_authenticate(User.objects.get(username='satoshi')); print(c.get('/api/live/', HTTP_HOST='localhost').status_code)"
```

**Web-Container hängt nach dem Frontend-Build** (`podman ps` ohne `minersentinel-web`, Seite 503, `/root/fe-recreate7.log`
meldet „name … already in use“): virtfs-Spiegel der Container-ID und des Overlay-Layers aushängen
(`grep /home/virtfs/ /proc/self/mountinfo | grep <id|layer> | awk '{print $5}' | xargs -r -n1 umount -l`, ggf. auch
`umount -l /var/lib/containers/storage/overlay/<layer>/merged`), dann `podman rm -f --storage <id>` und
`podman-compose up -d --no-deps frontend`. Nur den eigenen Web-Container anfassen.

---

## 14. Lokale Test-Umgebung (iMac)

- **Datenbank:** Colima-Container `ms-local-postgres` (postgres:17-alpine, `127.0.0.1:5433`, trust, Volume `ms-local-pgdata`).
  Das Homebrew-Postgres 14 startet nicht (icu4c fehlt) und gehört evtl. anderen Projekten – nicht anfassen. Alte gestoppte
  Container `minersentinel-*` in Colima ebenfalls nicht anfassen.
- **Kopie der VPS-DB auffrischen:** `pg_dump -Fc` über ssh (nur lesend am VPS), lokal `drop database … with (force)`, neu
  anlegen, `pg_restore --no-owner`. Das lokale Passwort von `satoshi` (vom Nutzer selbst per `changepassword` gesetzt) vorher
  per `\copy` sichern und danach zurückschreiben (nicht anzeigen). Danach `manage.py migrate` für neue Migrationen.
- **Starten:** `.claude/launch.json` (lokal per `.git/info/exclude` ausgeschlossen): `ms-backend-local` (runserver `127.0.0.1:8000`,
  Umgebung über Variablen, `.env` unverändert, `MINERSENTINEL_ENV=Test`, `ACINFINITY_DATA_DIR=<repo>/backend/acinfinity_data`)
  und `ms-frontend-local` (Vite **build --watch + preview** auf `localhost:3000`, weil der Vite-Dev-Server im Original-Code abbricht;
  Ordner `frontend/dist-local`, ebenfalls ausgeschlossen). Die Python-Umgebung liegt im Session-Scratchpad – in einer neuen
  Session neu anlegen (3.10).
- **Kein lokaler Data-Service** (würde die echten Geräte abfragen und eine zweite Regelung betreiben). Folge: lokal sind alle
  MinerGeräte „offline“, Takt-Karte ausgeblendet. **Lokal nichts setzen, was auf echte Geräte wirkt** (Drosselung Nano/Q, Neustart,
  Workmode schicken das lokale Backend direkt an die Miner).
- Im Browser prüfen: die Live-Seite im Browser-Panel, wenn der Nutzer sich dort angemeldet hat (nie selbst Passwörter eingeben).
- AC Infinity lokal: Zugangsdaten trägt der Nutzer selbst ein; „Test aktivieren (5 min)“, Live übernimmt danach (7.6).

---

## 15. Rückfragen an die Cowork-Session

Bisherige Arbeit: Cowork-Session „MinerSentinel Docker setup“ (https://claude.ai/code/session_018fS1Z9eLgVGhGqvY6DNrgG).
Fragen in `docs/claude/FRAGEN-AN-COWORK.md` schreiben, Nutzer bitten, dort „Schau in die Fragen-Datei“ zu schreiben;
Antwort steht danach in derselben Datei. Entscheidungen des Nutzers direkt beim Nutzer erfragen.
