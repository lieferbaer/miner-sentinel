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
   7.1 Datenerfassung · 7.2 Übersicht · 7.3 Drosselung (nur Nano/Q) · 7.4 Takt-Tabellen (entfernt) · 7.5 Over-Hot ·
   7.6 Lüfter (AC Infinity) · 7.7 Pools/f2pool · 7.8 Temperatur-Automatik (Regelwerk vom 06.11.) · 7.9 Zentrale Abfrage & Live-API ·
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
| **`live_cycle`** | **5 s** | **Zentrale Abfrage** aller Avalons (`live_reader.py`), danach sofort **Temperatur-Automatik**; jede 3. Runde (15 s) **Drosselung** in eigenem Worker (7.9; Takt-Tabellen seit v2.2.7 entfernt) |
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
   „MinerSentinel“ oben links). Start 1.0.0 am 21.10.; seit 25.10. Zählung 2.0.x (Nutzer: „ab sofort Version 2.0.0“); **seit 26.10. 2.1.x** (Nutzer: „Version 2.1.0“, nach 2.0.9); **seit 27.10. 2.2.x** (Nutzer: „als Version 2.2.0 committen“, nach 2.1.8, mit dem einstellbaren Zielbereich). **v2.2.7 (06.11.):** Regelwerk des Nutzers (7.8). **v2.2.8 (08.11.):** Moduswechsel abwärts erst unter der Schwelle, Bereich 1 bis 5 min −10. **v2.2.9 (08.11.):** Moduswechsel abwärts mit 25 MHz Spielraum, Lernwert = gesetzter Takt am Ende + Ø Takt laut Miner, Lernwerte einzeln löschen (auch andere Zieltemperaturen, auch bei Automatik aus), Sicherungen löschen (letzte bleibt), Analytics „Balance und Pool-Hashrate je Tag (160 Tage)“ (Migration 0028). **v2.2.10 (09.11.):** Lernwerte nach Stufe gruppiert + Spalte „Modus“, Lernwert auf Lüfterstufe 1 bis Fehlerrate unter 7 %, bei Basisstufe 1 nach −5 30 min keine Erhöhung bei ZTU−2…ZTU−1. **v2.2.11 (09.11.):** Sperre nach einer Erhöhung endet auch mindestens 3 °C unter der Temperatur bei der Erhöhung; neue Erhöhungstabelle (+5/3 min, +15, +20, +25, +40 je 2 min). **v2.2.14 (09.11.):** Moduswechsel abwärts mit 10 MHz Spielraum (vorher 25).
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
- Stand 28.10. (v2.2.6): **Backend 218 ✓, Data-Service 363 ✓ (7 übersprungen), Frontend 171 ✓**, Lint 0 Fehler (15 Warnungen aus dem Original).
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
  Überhitzungsschutz der Firmware: TMax > 108 °C. Zusammenhang Temperatur ↔ DH bei festem Takt ist nirgends gemessen (der Temperatur-Test dafür ist seit v2.2.7 entfernt).
- `SYSTEMSTATU` mit `Over Hot` = Boards abgeschaltet. `FanR`/`Fan1..` = interne A15-Lüfter: **nur Anzeige (Übersicht; seit 06.11. auch
  Lüfterseite, Karte „Lüfter läuft auf“), sonst nirgends verwenden** (Nutzer: „Lüfter-Simulatoren“).
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
  Tausender mit Punkt wie auf der Lüfterseite („1.520 rpm“, Nutzer 06.11.).
- Hashrate-Karte: „… GH/s · Fehlerrate x %“ (aktuelle `DH` des Miners, `details.error_rate_pct`, alle 10 s); dieselbe Karte ist
  seit v2.0.0 die **erste Karte auf der Lüfter-Seite** (dort steht die Fehlerrate nicht mehr in „Takt laut Miner“).
- Darüber Problem-/Highlight-Leiste des Originals (`FleetAlertsBar`), darunter je Avalon die Takt-/Drosselungs-Karte
  (A15: „Takt und Modus“ ohne Eingabe, 7.3).

### 7.3 Drosselung – nur Nano 3S / Avalon Q (`throttle_controller.py`, Backend `DeviceViewSet.throttle`, `components/devices/ThrottleCard.jsx`)
- **A15: Drosselung abgeschafft (v1.0.7, Nutzer 24.10.).** `POST /api/devices/<A15>/throttle/` → 400 („Die Drosselung des A15 ist
  abgeschafft; den Takt regelt die Temperatur-Automatik.“). Die A15-Karte heißt „Takt und Modus“: groß der **Takt laut Miner**,
  darunter „gesetzt X MHz (Automatik) · Standard Y MHz“, Modus-Buttons und Modus-Tabelle; keine Eingabe.
  „gesetzt“ = `_last_set_clock` (nur noch die Automatik; bei ausgeschalteter Automatik nur, solange der Miner den Takt
  noch fährt – sonst „—“). Migration 0024 entfernt eine noch vorhandene alte A15-Drosselung (ohne Taktbefehl).
- **Nano 3S / Avalon Q: unverändert** (Ziel-Hashrate, Geraden-Methode, Totband ±2 %, max. 20 % Schritt; „Aufheben“ sendet Standardtakt).
- Keine Sperren mehr (die Sperre während eines Takt-Laufs entfiel mit den Takt-Tabellen, v2.2.7).
- Während die Automatik läuft, liest der Controller nur (Beobachtungsmodus) – lernt dann keine „Standardtakte“.
- **A15-Standardtakte (`profiles`) werden nie mehr überschrieben**, sobald bekannt, und nicht gelernt, nachdem die Automatik Takte
  gesetzt hat (`auto_clock_set`): sonst würde der Takt nach „Automatik aus“ als Standardtakt gelernt.
- Zustand: `devices.connection_config -> 'throttle'` (mit `rev`; Controller schreibt nur bei unverändertem `rev`, sonst Rollback).

### 7.4 Takt-Tabellen – entfernt (v2.2.7, Nutzer 06.11.)
Nutzer: „wir werden die Seite Takt-Tabellen nicht benötigen, die kannst du löschen“. Entfernt: Seite `CalibrationPage.jsx`
(Route, Menü), Messlauf (Max-Messung, Messreihe) in `calibration.py`/`live_reader.py`/`app.py`, Backend-Endpunkt
`/api/devices/<id>/calibration/`, alle Sperren „Takt-Messung läuft“ (Modus-Buttons, Gerätesteuerung, Drosselung), Live-Schlüssel
`calibration`, Takt-Messung in der Automatik (fester Lüfterschutz 80/84/85 °C, Phase `messung`). **`calibration.py` bleibt** mit dem
gemeinsamen A15-Zugriff (`read_miner`, `a15_conf`, `a15_set_mode`, `set_frequency`, `reboot`, `mode_key`), den Automatik,
Drosselung und zentrale Abfrage nutzen. **Daten bleiben** (Nutzer: behalten): Tabellen `calibration_runs` (leer) und
`calibration_points` mit den zwei beauftragten Punkten (je 15 min bei 250 MHz): Mode 04 252 MHz laut Miner, 121,8 TH/s, 1.961 W,
16,1 J/TH, DH 3,18 %; Mode 00 262 MHz, 140,3 TH/s, 3.062 W, 21,8 J/TH, DH 0,14 %. Modelle bleiben ungenutzt (keine Migration).
Alte Protokolleinträge `messung_start`/`messung_ende` und Phase `messung` bleiben lesbar.

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
- **Reihenfolge der Seite (v2.2.12, Nutzer 09.11.):** oben Zieltemperatur, Temperatur-Automatik, Port/Stufe-Buttons und Ergebnisse; unten AC-Infinity-Konto, Controller-Name mit An/Aus und Messwerten, Port-Tabelle und Sicherung (Meldungen der Sicherung stehen dort). Umsetzung: Flex-Spalte mit `order-1…4` (der Controller rendert zwei Karten).
- Seite: Zugangsdaten-Formular (Nutzer trägt selbst ein), Sicherung/Zurückschreiben/**Löschen** (Test-Buttons erst nach Sicherung; „Löschen“ nur bei mehr als einer Sicherung des Controllers, Backend `POST /api/fans/backup/delete/` {file}: nur Dateien aus der Sicherungsliste, die letzte je Controller → 409, Sperre `backup.lock` gegen zwei gleichzeitige Löschungen; v2.2.9, Nutzer 08.11.),
  **An/Aus** neben dem Gerätenamen, **Port 1–4 als Buttons** (Auswahl in `localStorage`), **Stufe 0–10 als Buttons: ein Klick setzt**
  (Rücklesen nach `READ_BACK_S = 5` s); markiert ist die vom Controller gemeldete Stufe. Bei aktiver Automatik wählen die Buttons
  die Automatik-Stufe; An/Aus und Port gesperrt.
- Automatik-Bereich: **5 Karten** (Hashrate mit Fehlerrate wie Übersicht – erste Karte seit v2.0.0; Heißester Chip mit den Temperaturen der weiteren Boards, Zieltemperatur
  und zuletzt Tendenz (seit v2.2.13 ohne die Wörter „weitere Boards“ und ohne „Regelwert“, Nutzer 09.11.); Takt laut Miner mit gesetzt · Modus · Standard; Lüfter läuft auf (aktive Stufe vs. Basisstufe, Kühlmodus +n / Heizmodus −n, Raum (v2.1.5) · Fan speed der A15 in rpm (Nutzer 06.11.; in der Wartung und solange die A15
  nicht antwortet „unbekannt“ statt des letzten Werts));
  Status mit Modus (Heiz-, Basis-, Kühlmodus), laufendem Befehl, Startpause, Takterhöhungssperren mit Uhrzeit und Grund, Bereich über ZTO,
  Lernperiode; v2.2.7). Kleine Zeile wiederholt den großen Wert nicht. Auf dem Handy
  untereinander. Darunter Lernwerte **je Lüfterstufe** (Tabelle mit Spalte „Zieltemperatur“, ohne Obergrenze seit v2.2.7; **nach Stufe gruppiert** (v2.2.10, Nutzer 09.11.): je Stufe oben die aktuelle Zieltemperatur, darunter grau die anderen aus `learned_zb` nach ZTU/ZTO; Spalten u. a. „Takt laut Miner (Ø)“ und „Modus“; **auf dem Handy je Stufe ein Block, Bezeichnung über dem Wert, zwei
  Werte je Zeile** – v2.2.6, Nutzer 28.10.: „Tabelle ist nicht mobil freundlich“; ab `sm` Tabelle, seitlich verschiebbar) und letzte Aktionen. Rückfrage „Automatik wirklich beenden? Der Takt bleibt,
  wie er ist.“

### 7.7 Pools / f2pool
- Pool-Einstellung je MinerGerät (`connection_config.pool_stats`) + globaler Fallback; Typen ckpool, publicpool, f2pool, btcpowlab, parasite.
- **f2pool alle 5 min** als eigener Job `poll_f2pool` (v2.1.4, Nutzer 26.10.; vorher in jeder Geräteabfrage alle 10 s mit 3 Anfragen à ~1 s,
  `pool_stats` wuchs ~6.500 Zeilen/Tag). `poll_all_sources` überspringt f2pool; Fehler → `check_pool_staleness(force_failure)` wie bisher
  (Warnung „Pool stats unavailable“ erst ab 30 min ohne frische Werte, Standard).

### 7.8 Temperatur-Automatik – nur A15 (`data-service/fan_automation.py`, Tabelle `fan_automation`)
**Seit v2.2.7 (06.11.) gilt das Regelwerk des Nutzers vom 06.11.** (Heiz-, Basis-, Kühlmodus für den Lüfter; Tabellen für
Takterhöhung und Taktsenkung mit Sperren). Alle früheren Takt- und Lüfterregeln sind entfallen (Nutzer: „nur die neuen werden
gültig sein“). Das Diagramm `docs/claude/Regelungslogik-Luefter-Takt.drawio` und die Seite „Regelablauf“ zeigen noch v2.2.6
(Nutzer: weitere Regeländerungen folgen, erst dann anpassen). Läuft **nach jeder zentralen Abfrage (5 s)**, nur auf Live.
Einschalten auf der Lüfter-Seite (Port + Stufe; braucht Sicherung). Der Data-Service ist dann der einzige, der Lüfter-, Takt- und
Modusbefehle sendet. **Bei Änderungen an der Regelung:** erst in eigenen Worten mit Beispiel zurückgeben (ZTU 80, ZTO 85,
Basisstufe 3 – Wunsch des Nutzers), Rückfragen, Plan, Freigabe (Regeln 1).

**Begriffe:** **Zieltemperatur** (bis v2.2.6 „Zielbereich“) mit **ZTU** = Zieltemperatur-Untergrenze und **ZTO** =
Zieltemperatur-Obergrenze; Temperatur = TMax (heißester Chip, ganze Grad); **Basisstufe** = vom Nutzer gewählte Lüfterstufe;
**ZeitImZieltemperatur** = ununterbrochene Zeit in ZTU…ZTO. Zahlen in Klammern: Beispiel ZTU 80, ZTO 85, Basisstufe 3.

| Regel | Vorgabe des Nutzers (06.11.) |
|---|---|
| **Zieltemperatur** | Karte „Zieltemperatur“ auf der Lüfterseite (Kacheln **77 bis 87** in einer Reihe, erst ein Ende, dann das andere, „Übernehmen“; zu breit → ab dem ersten Tipp auf 5 Grad gekürzt, zu schmal → auf 4 Grad ergänzt). **ZTU ab 77, ZTO bis 87 °C, Breite 4–5 Grad, beide Grenzen mitgezählt** (81–84 = 4 Grad; Backend: 3 ≤ ZTO−ZTU ≤ 4, Meldung „Zieltemperatur 77–87 °C mit 4 bis 5 Grad wählen.“). Gespeichert in `target_low/target_high` (Migration 0027). Einstellbar immer; Protokoll `zielbereich` (interner Name bleibt) „Nutzer: Zieltemperatur L–U °C gewählt (vorher …)“, Details `alt`/`neu`. Wechsel im Betrieb: neue Grenzen ab der nächsten Runde, kein Start; Lernperiode endet, ZeitImZieltemperatur wird neu gemessen. **Lernwert löschen** (v2.2.9, Nutzer 08.11.): Knopf je Zeile, Backend-Aktion `learned_delete` {zb, level, at} schreibt nur den Protokolleintrag `lernwert_loeschen` (409 wenn neu gelernt/fehlt), der Data-Service löscht in der nächsten Runde (auch bei Automatik aus; nur wenn `at` noch gleich; Marke `learned_del_seen`); eine laufende Lernperiode läuft weiter. **Lernwerte je Zieltemperatur** (`learned` = aktive, `learned_zb` = Archiv „L-U“, `zb` = übernommene): zurück in der alten gelten wieder ihre Lernwerte. STATE_VERSION unverändert (würde alle Lernwerte löschen). |
| **Heizmodus** (Lüfter) | Temperatur **unter ZTU−10** (≤ 69) **und** Lüfterstufe seit **mehr als 1 min** unverändert (`fan_changed_at`; jede Änderung zählt, auch die Wahl der Basisstufe) → aktuelle Stufe **−1**, jede weitere Minute erneut, **mindestens Stufe 1**. ZTU−10 bis ZTU−5 (70–75): Stufe bleibt. **Über ZTU−5** (ab 76) → Basisstufe, Heizmodus endet, **3 min keine Takterhöhung**. Takterhöhungen im Heizmodus erlaubt. Ereignisse `heizmodus`, `heizmodus_ende`. Ersetzt den alten Heizmodus (beim Start Stufe 1 bis L). |
| **Basismodus** (Lüfter) | Sonst Lüfter = Basisstufe (Bereich über ZTU bis ZTO+2 und alle Fälle ohne Heiz-/Kühlmodus). |
| **Kühlmodus** (Lüfter) | **Über ZTO+2** (ab 88) → Basisstufe **+1**; **über ZTO+7** (ab 93) → **+2** (höchstens Stufe 10). +2 bleibt bis zum Ende (kein Zwischenschritt). **Unter ZTO−3** (ab 81 abwärts) → Basisstufe, Ende, **3 min keine Takterhöhung**. Alle Wechsel sofort. Ereignisse `kuehlmodus` (Beginn und +1 → +2), `kuehlmodus_ende`. Ersetzt Korrekturphase (Lüfterteil), Ausnahme und Lüfterstöße. |
| **Start** | Anlässe: Basisstufe gewählt, Automatik ein, Neustart des Miners, nach Wartung, nach Over Hot. Starttakt = **Lernwert der Basisstufe**, sonst Lernwert derselben Stufe aus der gemerkten Zieltemperatur mit der nächsten Mitte (Gleichstand: die kühlere), sonst **Initialwert** (1 = 170, 2 = 250, 3 = 330, 4 = 380, 5 = 400, 6 = 420, ab 7 = 440 MHz). Lüfter = Basisstufe. **Ein Start beendet Heiz- und Kühlmodus ohne Sperre**, danach gelten die Regeln neu; laufende Sperren, `cut_at` und ZeitImZieltemperatur bleiben. |
| **Startpause** | Nach einem Start mit kalten Chips (Neustart, nach Wartung, nach Over Hot; `start_cold`) **keine Takterhöhung, bis ZTU−5 erreicht ist** (75), **höchstens 10 min** (`start_hold`, Ereignisse `startpause` „Startpause: keine Takterhöhung bis 75 °C, spätestens HH:MM“, `startpause_ende`). Grund (25.10.): nach dem Neustart meldete der Miner 26 °C → +40 MHz → 85 °C nach 5 min. |
| **Takterhöhung** (unter ZTU) | ZTU−1 bis −3 (77–79) **+5**, danach **3 min** keine Erhöhung · −4 bis −5 (75–76) **+15**, **2 min** · −6 bis −10 (70–74) **+20**, 2 min · −11 bis −20 (60–69) **+25**, 2 min · −21 und kälter (≤ 59) **+40**, 2 min (v2.2.11, Nutzer 09.11.; vorher +10/3 min, +15, +20). **Sofort beim ersten Wert** (keine 60 s unter L, keine Tendenz-Bedingung, kein Beobachtungsfenster). Nicht während einer Sperre, der Startpause oder eines laufenden Befehls, höchstens 440 MHz. Protokoll über den Taktbefehl `takt`, z. B. „Chiptemperatur 77 °C (ZTU −3): +5 MHz, keine Takterhöhung bis HH:MM“. |
| **Takterhöhungssperren** (`locks`) | `erhoehung` (2 bzw. 3 min nach einer Erhöhung, nach der Zeile des Schritts), `heizmodus` und `kuehlmodus` (3 min nach dem Ende), `senkung` (5 min nach jeder Senkung). **Die ersten drei enden vorzeitig über ZTU+3** (ab 84); **`senkung` nie vorzeitig** (Nutzer 06.11., v: „1, 2, 3“). **`erhoehung` endet außerdem, sobald t ≤ Temperatur bei der Erhöhung − 3** (`raise_t`, bleibt wie die Sperre über Start/Automatik ein; Beispiel: +5 bei 79, bei 76 Ende → sofort +15 mit Bezug 76; nur diese Sperre; v2.2.11, Nutzer 09.11.: av, aw ja, ax nein). **Nur bei Basisstufe 1:** nach einer Senkung um genau −5 (`cut5_at`) 30 min keine Erhöhung bei ZTU−2…ZTU−1 (78–79), bei ZTU−3 und kälter normal; jeder Start hebt sie auf (v2.2.10, Nutzer 09.11.: ar, as, at nein); „Läuft gerade“ zeigt „keine Takterhöhung bei 78–79 °C bis HH:MM (−5 um HH:MM)“. |
| **Taktsenkung** (über ZTO) | Bereich 1 = ZTO+1…+3 (86–88) nach ZeitImZieltemperatur: **bis 5 min −10, über 5 min −5** (Nutzer 08.11.; bis v2.2.7 unter 1 min −15; ein laufender Ausflug folgt der Regel sofort, `minus1` aus `dwell_s`) · Bereich 2 = ZTO+4…+6 (89–91) **−20** · Bereich 3 = ZTO+7…+9 (92–94) **−20** · Bereich 4 = ab ZTO+10 (ab 95) **−50**. Nutzer: „in dem Temperaturbereich nur einmal senken, nicht bei jedem Gradwechsel“: Eintritt von unten oder Anstieg in einen höheren Bereich → **sofort** (auch 30 s nach der letzten Senkung); im selben Bereich **alle 2 min**; Abstieg in einen tieferen Bereich → **2 min nach der Ankunft**; nach kurzem Rückfall in die Zieltemperatur → frühestens **3 min nach der letzten Senkung** (`cut_at`), außer der neue Bereich ist höher als bei der letzten Senkung (`cut_band`) → **sofort** (Nutzer 08.11.). Danach jeweils **5 min keine Erhöhung**. Die 96-°C-Grenze aller Schwellen ist entfallen (Firmware-Schutz über 108 °C). Beispiel: 10:00 85 → 86 nach 3 min im Ziel −10; 10:02 noch 87 −10; 10:03 89 −20; 10:04 zurück 88 → 10:06 −10; 10:07 85 → nichts. Zustand `hot` {band, since, band_at, next_cut_at, dwell_s, minus1}. |
| **ZeitImZieltemperatur** | Ununterbrochene Zeit in ZTU…ZTO seit dem letzten Eintritt (von unten oder oben; `zt_since`); ein Wert unter ZTU oder über ZTO beendet sie. Beim Überschreiten von ZTO gemessen und für den ganzen Ausflug festgehalten (Bereich 1 wiederholt denselben Wert). Beispiele: 4 min in 80–85, dann 86 → −10; direkt 79 → 86 → −15. |
| **Takt laut Miner weicht ab** | Takt laut Miner **mehr als 15 MHz über oder unter** dem gesetzten, **ununterbrochen 15 min** → gesetzten Takt erneut senden (Modus vorher, falls nötig), Ereignis `firmware_takt`. Die 15 min beginnen neu bei Abweichung ≤ 15 MHz, bei eigener Taktänderung bzw. laufendem Befehl, nach dem erneuten Senden und ohne Wert (`fw_dev`). Beispiel: gesetzt 300, gemeldet 284 zählt, 285 nicht. Ersetzt v1.0.8 (über +25 MHz nach 60 s). |
| **Fehlerrate** (DH) | **Über 10 % ununterbrochen 30 min und seit 6 h kein automatischer Neustart wegen Fehlerrate** → automatischer Neustart (`neustart_auto`, cgminer `ascset|0,reboot,0` aus dem Data-Service). Keine Obergrenze mehr, kein Bezug zum gesetzten Takt, keine Sperren nach einem Start. Nicht während Wartung und Over Hot; fehlender Wert oder ≤ 10 % setzt die 30 min zurück; Sendefehler → nach 5 min erneut (zählt nicht für die 6 h); gesperrt durch die 6 h → nur Hinweis. Für die 6 h zählen nur diese Neustarts (`auto_restart_at`), nicht der Knopf. |
| **Lernen** | **15 min** stabil in ZTU…ZTO bei unveränderter Lüfterstufe → Lernwert der **aktiven Stufe** (**gesetzter Takt am Ende der 15 min** `mhz` – seit v2.2.9, Nutzer 08.11. „ag C“, vorher Ø gesetzter Takt; **Ø Takt laut Miner** `mhz_miner` nur aus Runden ohne offenen Befehl, alte Lernwerte ohne → „—“; **Modus** `mode` (z. B. „2/L4“, in einer Lernzeit konstant; v2.2.10, alte ohne → „—“); Ø Temperatur, Ø Hashrate `ths_avg`, Effizienz `j_th` = Ø Leistung / Ø Hashrate, Zeit, Anzahl); jede weitere Periode ersetzt ihn. **Nur bei Ø Fehlerrate unter 5 %** (Nutzer 06.11.; vorher 4 %), **auf Lüfterstufe 1 unter 7 %** (die gelernte Stufe, nicht die Basisstufe; v2.2.10, Nutzer 09.11.), sonst verworfen (`lernen_ende` „verworfen“). Taktschritte setzen die Lernzeit nicht zurück, Ausflüge aus ZTU…ZTO bis 60 s auch nicht (v2.0.1). |
| Grenzen | 100–440 MHz; ein berechneter Takt unter 100 → **nicht senden**, Eintrag `fehler`, Takt bleibt. |
| Modus | 0–360 mode 04 · 361–380 03 · 381–400 02 · 401–420 01 · 421–440 00. **Spielraum 10 MHz nur nach unten** (abwärts erst **unter** 350/370/390/410, also ab 345/365/385/405; v2.1.7, Nutzer 26.10.: 20 MHz; v2.2.8 „nur kleiner“; v2.2.9 Nutzer 08.11.: 25 MHz; seit v2.2.14 Nutzer 09.11.: mode 00 < 410, 01 < 390, 02 < 370, 03 < 350). Normal: erst Modus, Übernahme, dann Takt. **Bei einer Senkung über ZTO: Takt sofort, dann Modus, dann Takt erneut** (der Moduswechsel stellt den Standardtakt ein). Moduswechsel durch den Nutzer: Automatik arbeitet weiter. |
| Senden / Übernahme | Vor jedem Taktbefehl Zielpunkte mit den gemeldeten SF0-Punkten vergleichen: **gleich → kein Befehl**. Übernommen: Modus = Miner meldet ihn; Takt = Miner meldet den gesetzten Wert **oder** einen anderen Wert als vorher. Ohne Übernahme **nach 20 s erneut, bis es klappt** (jeder Versuch im Protokoll). Eine Erhöhung kommt erst, wenn der vorige Befehl übernommen ist. |
| Situationen | Neustart → Start (**„nicht erreichbar“ rund um einen Neustart ist kein Fehler**, v2.0.8: Knopf „A15 neu starten“ → bis 5 min `neustart_offline`/`neustart_online`, Status-Karte „Neustart“; antwortet die A15 auf den Neustart-Befehl nicht, gilt er als gesendet (v2.2.1); Neustart auf anderem Weg → `miner_offline`/`miner_online` der 3 min davor werden umbenannt; länger als erwartet → roter `miner_offline`) · Over Hot → keine Entscheidung bis gültige Werte, danach Start · Miner antwortet nicht → Lüfter unverändert, warten · Lüftersteuerung antwortet nicht/andere Stufe → erneut senden, bis es klappt · Wartung → wie unten · Automatik aus → **kein Takt gesendet, Takt bleibt**, Lüfter bleibt auf der letzten Stufe · Drosselung abgeschafft. |

**Entfallen mit v2.2.7 (Nutzer 06.11.):** Obergrenzen je Stufe (samt Knopf „Obergrenzen löschen“, Aktion `caps_clear`),
Ertrags-Prüfung, Korrekturphase mit Tabelle U+1…U+10 und 60-s-Regel, Aufenthaltsregel −10/−5, Taktsperre (5 min), 30-s-Beobachtungsfenster
und Tendenz-Bedingung (die Tendenz wird nur noch angezeigt), Plus-Schritte nach 60 s unter L mit 3-min-Warten, Lüfterstöße,
Ausnahme, alter Heizmodus, 96-°C-Grenze, Takt-Messung (7.4) und **Temperatur-Test** (Aktionen `temp_test_start/abort`; Feld
`temp_test_at` bleibt ungenutzt, keine Migration). Alte Protokolleinträge dieser Arten und alte Phasen (`normal`, `korrektur`,
`ausnahme`, `messung`) bleiben mit deutschem Namen und Filter lesbar. Einzelheiten der alten Regeln: Git-Verlauf bis v2.2.6
(Commit a058f67).

**Technische Festlegungen (vom Nutzer bestätigt bzw. aus v1.0.2 übernommen):**
- Eine fällige Taktsenkung über ZTO **ersetzt** einen laufenden Befehl (Priorität Sicherheit).
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
  weiter, auch weitere Senkungen über ZTO (Review 26.10.: in der Folge blockierte er sie); ein Takt über dem Standardtakt des
  jetzigen Modus wird auf diesen begrenzt und Plus-Schritte darüber warten (sonst stieg der gesetzte Takt, während der Miner beim
  Standardtakt blieb). Antwortet die Seite wieder, wird der Modus passend zum dann gesetzten Takt nachgeholt. Nach einem gesendeten
  Umschaltbefehl tragen die zuletzt gelesenen Einstellungen den neuen Modus. HTTP-Fehler der Seite (z. B. IncompleteRead) gelten wie
  eine Zeitüberschreitung (sonst brach der ganze Live-Zyklus ab). `a15_set_mode` lehnt alte Einstellungen ab – nie alte
  Pool-Daten. Protokoll erst nach **2 min**: `einstellungsseite_aus` „A15-Einstellungsseite
  antwortet nicht – Mining-Schnittstelle ok“, zurück `einstellungsseite_an` „… wieder erreichbar nach X min“ (nicht rot). Rot
  „Miner antwortet nicht“ nur, wenn die Mining-Schnittstelle (estats/summary) selbst nicht antwortet.

**Wartung** (unverändert): Button „Wartung starten (A15 ausschalten)“ (`maintenance_at`, Migration 0023). Antwortet der A15 nicht mehr → Lüfter **aus** (`atType` 1); antwortet er wieder → Lüfter **an auf die Stufe von
vorher**, Wartung endet, dann Start. Keine offline/online-Meldungen während der Wartung und 15 min danach (`maint_ended_at`).

**A15 neu starten** (v2.0.6, Nutzer 25.10.): Button neben „Wartung starten (A15 aus)“ mit Rückfrage; nutzt den vorhandenen
`POST /api/devices/<id>/restart/` (`ascset|0,reboot,0`, im Canaan-Quellcode der A15 vorhanden, Verzögerung 0–300 s; die A15 meldet
„Privileged access OK“). Für die A15 der Automatik Nutzer-Ereignis `neustart_nutzer` „Nutzer: A15-Neustart gesendet“. Danach erkennt
der Data-Service den Neustart (Laufzeit zurückgesetzt) → Start mit dem Lernwert.

**Zustand** `fan_automation.state` (nur der Data-Service schreibt; `v` = 2): `phase` je Runde (`heizmodus`, `basismodus`,
`kuehlmodus`; `wartung`, `aus`), `heat` {since}, `cool` {since, plus}, `fan_changed_at`, `locks` {erhoehung, heizmodus,
kuehlmodus, senkung}, `start_hold`, `hot`, `cut_at`, `zt_since`, `fw_dev`, `dh_since`, dazu `t`, `tendency`, `clock_set`,
`clock_changed_at`, `clock_now`, `mode_key`, `stock_mhz`, `fan_target`, `fan_set`, `fan_read`, `pending`, `queue`, `settle`,
`learn_since`, `learned` (je Stufe), `learned_zb`, `zb`, `auto_restart_at`, `log`. Veraltete Schlüssel (Obergrenzen, Ertrag,
Stöße, Korrektur, Takt-Messung, Test …) werden in der ersten Runde entfernt. Einstellungen (`enabled`, `ac_dev_id`, `ac_port`,
`level`, `level_at`, `maintenance_at`, `target_low`, `target_high`) schreibt das Backend; an die Seite gehen nur `STATE_KEYS`
(`fan_views.py`). Endpunkt `GET/POST /api/fans/automation/` (`enable`, `level`, `disable`, `target`, `maintenance_start`,
`maintenance_end`).

**Tests:** `data-service/tests/test_fan_automation.py` prüft jede Regel mit dem Beispiel ZTU 80, ZTO 85 (Zieltemperatur der Tests).

### 7.9 Zentrale Abfrage & Live-API (`data-service/live_reader.py`, `miner_cache.py`, `backend/api/live_views.py`, `frontend/src/lib/LiveContext.jsx`)
- **Ein Leser alle 5 s**: `estats` + `summary` je Avalon (parallel, ein ausgeschaltetes Gerät bremst nichts), A15-Webinterface alle 30 s
  bzw. sofort nach eigenem Umschalten. Werte in `miner_cache` (je Gerät, mit Zeit; **eine Anfrage gleichzeitig je Gerät**; jeder
  Schreibbefehl löscht die Werte des Geräts).
- Direkt danach: Automatik (jede Runde), Drosselung (jede 3. Runde, eigener Worker). Niemand liest sonst selbst;
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
- **Balance und 24-h-Hashrate je Stunde** (v2.2.9, Nutzer 08.11.; Migration 0028): `pool_stats_hourly.balance_last` (f2pool-Kontostand, letzter Wert der Stunde, sicherer Cast – ein schlechter Wert ist NULL) und `hashrate_1d_last` (f2pool-24-h-Wert, letzter der Stunde). Einmaliges Nachtragen beim Start des Data-Service aus den noch vorhandenen Rohwerten (ab der ersten vollen Stunde; geprüft 08.11. mit Temp-Tabellen: 739 Stunden ab 09.10., Mittelwerte unverändert). Vorher stand die Balance nur in den Rohwerten (30 Tage). **Deploy: erst `migrate`, dann Data-Service neu starten.** Endpunkt `GET /api/analytics/pool-daily/`: die letzten 160 deutschen Kalendertage, je Tag Kontostand am Tagesende und f2pool-24-h-Hashrate am Tagesende, Tage ohne Daten null; Karte „Balance und Pool-Hashrate je Tag (160 Tage)“ auf Analytics (Balken Hashrate, Linie Balance, immer 160 Tage).
- **Rohwerte nur 30 Tage** (`device_mining_stats`, `device_hardware_stats`, `pool_stats`): Job `prune_raw_stats` jede Nacht 03:40 – erst
  Stundenwerte nachrechnen, dann nur Rohwerte löschen, deren Stunde in den Stundenwerten steht (in Blöcken). Die alte
  Aufbewahrungs-Einstellung (`metrics_retention_days`, 90 Tage) gilt weiter für alle Tabellen, also wirksam für `device_system_info`
  (Chiptemperaturen je Board, Fehlerrate – nicht in Stundenwerten, darum nicht nach 30 Tagen gelöscht).

## 8. Integration des Originals 1.2.0

Übersicht = eigene Einzelansicht + `FleetAlertsBar`; Original-Gerätesteuerung bleibt; Navigation
Original + „Activity“ + „Lüfter“ (Takt-Tabellen seit v2.2.7 entfernt); Dockerfiles/Compose Original mit voll qualifizierten Images.

---

## 9. Datenbank und Migrationen

- Eigene Tabellen: `calibration_runs`, `calibration_points` (0020; seit v2.2.7 ungenutzt, Daten bleiben), **`fan_automation`** (**0021**, handgeschrieben, hängt an 0020;
  **0022** `level_at`, **0023** `maintenance_at`, **0025** `temp_test_at`, seit v2.2.7 ungenutzt), **`automation_log`, `automation_cycle`** (**0024**, mit Datenmigration: alte
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
3. Regelwerk v2.2.7 beobachten (Protokoll-Seite): Heiz-/Kühlmodus, Takterhöhungen und -senkungen, Sperren, Lernwerte. Weitere
   Regeländerungen des Nutzers angekündigt (06.11.); danach Diagramm und Regelablauf-Seite anpassen.
5. Takt laut Miner liegt 13–20 MHz über dem gesetzten (Nachziehen der Firmware); seit v2.2.7 sendet die Regelung nach 15 min über ±15 MHz neu – beobachten.
6. Einstellungsseite des A15 nicht erreichbar: seit v2.1.2 regelt die Automatik weiter (7.8) – beobachten, wie oft `einstellungsseite_aus` kommt.
7. Allgemeine Zeitraumauswahl („Last 30 days“) erscheint auch auf Protokoll- und Lüfter-Seite, ohne Wirkung dort.
8. Ausstehende Original-Migration – nur nach Rücksprache.

---

## 12. Dateikarte (eigene bzw. stark geänderte Dateien)

```
backend/api/
  views.py           DeviceViewSet: system-latest, restart, workmode,
                     throttle (Nano/Q; A15 nur Anzeige, set_mhz/set_by)
  models.py          CalibrationRun, CalibrationPoint (ungenutzt), FanAutomation, AutomationLog, AutomationCycle   migrations/0020 … 0024
  automation_log_views.py  /api/automation/log/, …/stats/, /api/automation/cycles/, /api/automation/export.csv
  acinfinity.py      AC-Infinity-Client (Login, Test-Sitzung, Live-Pause, set_fan_level, Sicherungen) – auch vom Data-Service geladen
  fan_views.py       /api/fans/* (status, credentials, backup, restore, test, session, automation)
  live_views.py      /api/live/
data-service/
  app.py             Jobs (live_cycle, poll_all_sources …)
  live_reader.py     zentrale Abfrage 5 s
  miner_cache.py     Zwischenspeicher + Sperre je Gerät
  fan_automation.py  Temperatur-Automatik (Regelwerk vom 06.11., Lernwerte je Stufe und Zieltemperatur)
  automation_journal.py   Protokoll (automation_log, automation_cycle, Aufräumen 360 Tage)
  calibration.py     gemeinsamer A15-Zugriff (lesen, Modus, Takt, Neustart)
  throttle_controller.py  Drosselung Nano/Q (TH/s); A15 nur Profile/Beobachtung
  collectors/avalon_collector.py   Erfassung aus der zentralen Abfrage
frontend/src/
  lib/LiveContext.jsx              gemeinsame Live-Abfrage 5 s
  pages/OverviewDashboard.jsx, FansPage.jsx, ProtocolPage.jsx   components/fans/TargetRangeCard.jsx
  components/devices/ThrottleCard.jsx, OverHotBanner.jsx   components/ui/info-tip.jsx
docs/claude/Regelungslogik-Luefter-Takt.drawio   Diagramm der Regelung bis v2.2.6 (8 Seiten, noch nicht angepasst)
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
