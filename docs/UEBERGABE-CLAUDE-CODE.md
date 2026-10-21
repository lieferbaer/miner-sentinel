# MinerSentinel – Übergabe an Claude Code

Stand: 21.10.2026, Branch `vps-stand` (Commit `f627a85`), deployt auf dem VPS lb-db.de.

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
   7.1 Datenerfassung · 7.2 Übersicht · 7.3 Drosselung · 7.4 Takt-Tabellen (A15) · 7.5 Over-Hot ·
   7.6 Lüfter (AC Infinity) · 7.7 Pools/f2pool · 7.8 Temperatur-Automatik · 7.9 Zentrale Abfrage & Live-API
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
| `check_devices`, `network_data` (30 min), `check_settings` (2 min), `prune_retention` (24 h) | – | Original |

Die früheren Einzel-Jobs `throttle_controller`, `calibration` (je 15 s) und `fan_automation` gibt es nicht mehr.

**Browser:** eine gemeinsame Abfrage **`GET /api/live/` alle 5 s** (`frontend/src/lib/LiveContext.jsx`, eingebunden in `Layout.jsx`),
solange die Seite offen ist. Steuerung läuft immer auf dem Server, unabhängig vom Browser.

---

## 5. Deploy, Tests, Rollback

### 5.1 Arbeitsablauf
1. Auf dem iMac in `vps-stand` ändern, Tests (5.3), Nutzer sagt „committen“ → Claude committet und pusht (`git push origin vps-stand`).
   **Vor jedem Push die eigene Versionsnummer `frontend/src/lbVersion.js` um 1 erhöhen** (letzte Stelle; Anzeige „v1.0.x“ neben
   „MinerSentinel“ oben links). Start 1.0.0 am 21.10.
2. Nutzer sagt „VPS auf Stand bringen“ (oft zusammen mit „committen“).
3. VPS: `git ls-remote origin vps-stand` (iMac) prüfen, dann auf dem VPS
   `cd /opt/minersentinel && git status --short && git fetch -q fork vps-stand && git checkout -q -f vps-stand && git reset -q --hard fork/vps-stand`
   – vorher sicherstellen, dass `git status` nur `?? docker-compose.yml.imac` zeigt.
4. Je nach Teil: Backend (Autoreload; Migration → `migrate`), Data-Service (`podman restart -t 5 …`; Compose → `ms-recreate.sh`),
   Frontend (`rm -f /root/fe-build7.status; (nohup ./ms-build-frontend.sh > /root/fe-build7.status 2>&1 &)`, fertig bei `BUILT`+`FIN`).
5. Prüfen: `podman ps` (alle 4 Container!), Data-Service-Log auf `ERROR`/`Traceback`, `curl https://miners.lb-db.de/` = 200.
   Ein Data-Service-Neustart unterbricht einen laufenden Takt-Lauf nur ~10 s (Zustand in der DB).

### 5.2 Sicherungen / Rollback
- `/root/ms-deploy-bak-20261020-2213/` – vor 1.2.0-Deploy (Tarball, `db.dump`, alte `0015_calibration.py`); Images `:pre-deploy`.
- `/root/ms-cache-bak-20261020-2255/` – Data-Service-Dateien vor dem Zwischenspeicher.
- **`/root/ms-fan-auto-bak-20261021-0047/`** – vor der Temperatur-Automatik: `docker-compose.yml`, `podman inspect` von
  Data-Service/Backend, `db.dump`, `git-head.txt`; Images `localhost/minersentinel_{backend,data-service,frontend}:pre-fan-automation`.
  Rückweg: Compose zurückkopieren, Images verwenden, `migrate api 0020`.
- `/root/thr6/bak21 … bak42` – alte Einzel-Patches der Cowork-Zeit.

### 5.3 Tests (wie CI)
- Backend: `PYTHONPATH=backend DJANGO_SETTINGS_MODULE=minersentinel.settings_test python -m pytest backend/api/tests -c pyproject.toml --ds=minersentinel.settings_test`
- Data-Service: `PYTHONPATH=data-service python -m pytest data-service/tests -c pyproject.toml -m "not integration"`
- Frontend: `cd frontend && npm ci && npm run lint && npm run test:run && npm run build`
- Python ≥ 3.10 nötig (System-Python 3.9 reicht nicht): `/opt/homebrew/bin/python3.10 -m venv <dir>` + requirements von backend und data-service + pytest, pytest-django.
- Stand: **Backend 170 ✓, Data-Service 119 ✓ (5 übersprungen), Frontend 144 ✓**, Lint 0 Fehler (15 Warnungen aus dem Original).
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
- Die Firmware **zieht gesetzte Takt-Stufen langsam nach oben** (280 → 286, 228 → 237, 220 → 228 MHz). Regelungen rechnen deshalb
  immer mit dem **gemeldeten** Takt, nicht mit dem gesetzten.
- `GHSspd` = gemessene Hashrate, aktualisiert ~alle 130 s, mittelt diese Zeit. `GHSmm` ≈ 481,5 × Freq. `DH` = Fehlerrate %.
- Leistung: `WALLPOWER` (A15), sonst `PS[6]`. `MPO` ist nur ein Limit.
- **`TMax` = heißester Chip = max(`MTmax[a b c]`)**, alle 10 s neu, schwankt ±1 °C. Heißestes Board ist immer **Board 1**
  (4–6 °C über Board 2). Nach Neustart kurz `-273` = kein Wert.
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
- Hashrate-Karte: „… GH/s · Fehlerrate x %“ (aktuelle `DH` des Miners, `details.error_rate_pct`, alle 10 s); auch auf der
  Lüfter-Seite in der Karte „Takt laut Miner“.
- Darüber Problem-/Highlight-Leiste des Originals (`FleetAlertsBar`), darunter je Avalon die Drosselungs-Karte.

### 7.3 Drosselung (`throttle_controller.py`, Backend `DeviceViewSet.throttle`, `components/devices/ThrottleCard.jsx`)
- **A15: manueller Takt in MHz** (15–100 % des Standardtakts im aktuellen Modus). Nach **15 min unverändert** speichert der
  Data-Service beim nächsten frischen GHSspd **einmal** einen Messpunkt `kind='manual'` (Takt-Tabellen › „Manuell“) – nicht bei
  Over Hot oder Moduswechsel. Die frühere TH/s-Regelung gibt es beim A15 nicht mehr.
- **Nano 3S / Avalon Q: unverändert** (Ziel-Hashrate, Geraden-Methode, Totband ±2 %, max. 20 % Schritt).
- Sperren: nur während ein Takt-Lauf **läuft** (409). **Mit Automatik erlaubt**: der manuelle Takt übersteuert die Automatik
  (letzte Eingabe gilt), die Automatik regelt dann den **Lüfter** auf ~80 °C (7.8), bis die Drosselung aufgehoben wird.
- „Drosselung aufheben“: ohne Automatik → Standardtakt wird gesendet; **mit Automatik → kein Takt** (Automatik regelt vom aktuellen
  Takt weiter). Grund: Sprung auf Standardtakt brachte TMax auf 95 °C (Fehler 12).
- Anzeige A15: groß der **Wert laut Miner** (statt „Ungedrosselt“), darunter „laut Miner · gesetzt X MHz (Drosselung|Takt-Messung|
  Automatik) · Standard Y MHz“ – „gesetzt“ = der **zuletzt** gesetzte Takt (`_last_set_clock`), „—“ = Firmware-Takte; keine
  Beschreibungszeile und kein Modusname in der Kachel (Nutzerwunsch).
- Während Takt-Lauf oder Automatik liest der Controller nur (Beobachtungsmodus) – lernt dann keine „Standardtakte“; eine manuelle
  Drosselung in der Pause wird trotzdem überwacht und gemessen.
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
| **Start und Fortsetzen schalten die Temperatur-Automatik ein**; ohne eingerichteten Lüfter kein Start (409). | Lüfter hält im Lauf 80 °C. |
| **Fortsetzen:** erst Drosselung aufheben (ohne Takt), dann letzten Punkt neu messen; **Max-Messung schaltet dabei kurz weg und zurück**. | In der Pause können Drosselung/Automatik Takte gesetzt haben. |
| Abbrechen lässt den aktuellen Modus stehen. Normales Ende: zurück zum Startmodus. Messreihe **ohne** Automatik: kurz weg/zurück für Firmware-Takte; **mit Automatik nicht** – Takt bleibt, die Automatik übernimmt sofort (Lernwert). | 21.10.: Weg/zurück holte bei Lüfter Stufe 1–2 die Firmware-Takte → 100 °C. |
| Umschaltbefehl alle 20 s wiederholen bis Rückmeldung, Abbruch nach 300 s. | Fehler 2. |
| Over Hot → automatische Pause. | – |
| Anzeige: Modusname, zwei Countdowns (Modus/gesamt); Status kommt aus `/api/live/`, Tabellen laden bei Fortschritt bzw. jede Minute. | – |

Typische Dauer Messreihe: **~4,5 min pro Punkt** bei stabilen Bedingungen (Lauf 7 Punkte 6–9), anfangs 11–13 min.
**Stand 21.10. ~01:20:** Lauf 7 (Messreihe Power Mode 04) **läuft**, Punkt 11 bei 260 MHz.

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
- Automatik-Bereich: 4 Karten wie in der Übersicht (Heißester Chip, Takt laut Miner, Lüfter läuft auf, Status – kleine Zeile
  wiederholt den großen Wert nicht), darunter Lernwerte und letzte Aktionen. Während einer Takt-Messung Hinweis, dass die
  Automatik den Lüfter regelt und die markierte Stufe danach gilt.

### 7.7 Pools / f2pool
- Pool-Einstellung je MinerGerät (`connection_config.pool_stats`) + globaler Fallback; Typen ckpool, publicpool, f2pool, btcpowlab, parasite.

### 7.8 Temperatur-Automatik – nur A15 (`data-service/fan_automation.py`, Tabelle `fan_automation`)
Läuft **nach jeder zentralen Abfrage (5 s)**, nur auf Live. Einschalten auf der Lüfter-Seite (Port + Stufe; braucht Sicherung;
hebt eine manuelle Drosselung auf). Der Data-Service ist der einzige, der dann Lüfterbefehle sendet.

| Situation | Verhalten |
|---|---|
| Normal | Lüfter **fest auf der gewählten Stufe (nie darunter)**; Takt hält **TMax 80–84 °C** (Median 30 s): > 84 → −10 MHz je 1,5 °C (max. 30), < 80 → +10 MHz (max. Standardtakt); nach jedem Schritt **3 min warten**. |
| Klick auf eine Stufe | **Takt sofort auf den Ausgangspunkt/Lernwert dieser Stufe, in beide Richtungen**, auch beim erneuten Klick auf dieselbe Stufe (`fan_automation.level_at`, Migration 0022). Lüfter: höher sofort, tiefer nach 3 min. |
| Stufe senken | Mit Lernwert: Takt sofort auf den Lernwert, Lüfter nach 3 min. Ohne: Takt senken bis TMax ≤ 80, 3 min warten, dann Lüfter. |
| Stufe erhöhen | Lüfter sofort; mit Lernwert Takt **60 s später** auf den Lernwert. |
| Schutz | Aktueller Wert: **> 84 °C** Lüfter **+1**, **≥ 88 °C** Lüfter **+2** und −30 MHz (höchstens alle 60 s), **≥ 90 °C** Lüfter **+3**; zurück auf die gewählte Stufe erst bei **≤ 80 °C** (Nutzer 21.10.; vorher +1 erst ab 88, zurück bei 84 → Pendeln). Max. Stufe 10. Im Lüfter-Regelmodus (Messung/manueller Takt) wirkt eine höhere Schutzstufe **sofort**, bleibt es heiß, nach 60 s erneut. |
| Nach Lauf / manuellem Takt | Sofort auf den **Ausgangspunkt** der gewählten Stufe (tiefer: Takt sofort; höher: Takt 60 s nach dem Lüfter); sonst 10-MHz-Schritte. |
| Ausgangspunkt einer Stufe | Eigener Lernwert, aber **nie über dem Lernwert einer höheren Stufe und nie unter dem einer niedrigeren** (Nutzer 21.10.: Stufe 2 darf nicht mit mehr Takt starten als Stufe 3). Ohne eigenen Lernwert: höchstens Lernwert der höheren bzw. mindestens der niedrigeren Stufe. Gilt bei Stufenwechsel, nach Lauf/manuellem Takt und nach dem Senken. |
| Takt anheben | Erst **3 min nach der letzten Takt- UND Lüfteränderung** (21.10.: direkt nach dem Senken des Lüfters wurde +10 MHz gesetzt) und **15 min nach dem letzten Schutz-Eingriff**. Absenken und Schutz bleiben sofort. |
| Bezug der Schritte | Vom **zuletzt gesetzten** Takt (nicht vom gemeldeten): die Firmware zieht gesetzte Takte in Minuten um ~+11 MHz nach oben; gerechnet vom gemeldeten Wert wurden alle −10-MHz-Schritte aufgefressen (21.10.: 40 Schutz-Eingriffe in 5 h, gesetzte Werte stiegen 218 → 224). |
| Schutz-Eingriff = Takt zu hoch | Beim Einsetzen des Schutzes: Takt **dauerhaft** um den üblichen Schritt **+10 MHz** senken und den Lernwert der Stufe auf diesen Takt setzen (`source: Schutz`) – **höchstens alle 10 min** (`PROTECT_CUT_GAP_S`; 21.10. 08:14–08:35 sonst 5 Senkungen in 21 min, 204 → 164 MHz). Ziel: nach 1–2 Eingriffen bleibt der Lüfter auf der gewählten Stufe. |
| Obergrenze je Stufe | Der Takt, bei dem der Schutz einsetzte, wird für diese Stufe **4 h** nicht wieder erreicht (Anheben höchstens bis 2 MHz darunter, `state.ceiling`, `CEILING_S`). |
| Anheben fein von unten | +2 MHz bei 78–80 °C, +5 bei 75–78, +10 darunter (`_raise_step`). Grund: bei niedrigem Takt ändert die Firmware die Leistung sprunghaft (21.10.: 164 → 174 MHz = +226 W, +6 °C; Lauf 7: 160 → 150 −151 W, 150 → 140 −270 W). |
| Simulation | Vor Änderungen an der Regelung simulieren (Modell in `tests/test_fan_automation.py::_power_model_run`, kalibriert auf 21.10.: 1.774 W ≈ 80 °C bei 164 MHz, Sprung +226 W, 0,03 °C/W, τ 95 s, Drift +11 MHz). |
| Takt-Tabellen-Lauf **oder manueller Takt (Drosselung)** | **Kein Takt-Eingriff**; Lüfter hält **~80 °C**: +1 bei **> 81 °C**, −1 bei **≤ 78 °C**, mindestens **60 s** zwischen zwei Änderungen, Stufen **1–10** (auch unter der gewählten), Schutz sofort. Danach zurück zur gewählten Stufe (höher: Senken-Regel, tiefer: sofort). Status „Takt-Messung“ bzw. „Manueller Takt“. |
| Lauf pausiert | Normalbetrieb (ohne Drosselung). |
| Lernwerte | Takt je **Modus + Lüfterstufe**, wenn TMax **10 min ruhig** bei 80–84 °C (kein Schutz, keine Drosselung, kein Wechsel); spätere ruhige Phasen überschreiben. Gespeichert wird der **von der Automatik gesetzte** Takt (`source: gesetzt`; die Firmware zieht den gemeldeten nach oben), nur wenn sie selbst nichts gesetzt hat der Wert laut Miner (`source: laut Miner`). Tabelle auf der Lüfter-Seite. Stand: **Power Mode 04, Stufe 3 → 231 MHz** (21.10. 00:54, noch laut Miner gespeichert). |
| Lüfter-Rückmeldung | Lesen alle 60 s; nach einem Schreibbefehl erst **10 s später** zurücklesen (Controller meldet nach > 5 s); Abweichung erzwingt nur bei bestätigtem Wert ein erneutes Senden. |
| Fehler | AC-Cloud weg → Lüfter bleibt, Takt regelt weiter. A15 aus (Wartung) → keine Werte, Automatik regelt nicht, Lüfter bleibt auf seiner Stufe. Automatik ausschalten → Standardtakt wieder gesetzt. |
| Neustart / Moduswechsel | **Sofort der Ausgangspunkt der gewählten Stufe**, sobald der Miner wieder Takte meldet (auch wenn `TMax` noch −273 ist). Vorher (21.10.) blieb der A15 nach dem Einschalten auf den vollen Standardtakten bei Lüfter Stufe 2 (Simulation: 93 °C, 5 min ≥ 88 °C; neu: max 85 °C). |
| Firmware überschreibt | Meldet der Miner > 25 MHz mehr als gesetzt (normales Nachziehen ~+11), wird der gesetzte Takt erneut gesendet (frühestens 60 s nach der letzten Taktänderung), z. B. wenn die Firmware beim Hochfahren ihre Standardtakte nachträglich setzt. |
| Anmeldung weg | Live wartet 5 min (Test-Umgebung aktiv), Takt regelt weiter. |

Zustand in `fan_automation.state` (nur der Data-Service schreibt), Einstellungen (`enabled`, `ac_dev_id`, `ac_port`, `level`) schreibt das Backend.
Endpunkt `GET/POST /api/fans/automation/` (`enable`, `level`, `disable`).

### 7.9 Zentrale Abfrage & Live-API (`data-service/live_reader.py`, `miner_cache.py`, `backend/api/live_views.py`, `frontend/src/lib/LiveContext.jsx`)
- **Ein Leser alle 5 s**: `estats` + `summary` je Avalon (parallel, ein ausgeschaltetes Gerät bremst nichts), A15-Webinterface alle 30 s
  bzw. sofort nach eigenem Umschalten. Werte in `miner_cache` (je Gerät, mit Zeit; **eine Anfrage gleichzeitig je Gerät**; jeder
  Schreibbefehl löscht die Werte des Geräts).
- Direkt danach: Automatik (jede Runde), Drosselung + Takt-Tabellen (jede 3. Runde, eigener Worker). Niemand liest sonst selbst;
  Rückfall: eigene Abfrage, wenn der Wert > 15 s alt ist.
- Last am A15 (gemessen 21.10., 60 s): **24 cgminer + 1 Web** pro Minute (ursprünglich 32 + 10, kurz vor dem Umbau ~38 + 6).
- **`GET /api/live/`** (nur DB, ~120 ms, ~15 KB): Geräte, mining/hardware/system latest, Drosselung je Avalon, Automatik,
  Takt-Lauf. Browser fragt alle 5 s einmal für die ganze Oberfläche.

---

## 8. Integration des Originals 1.2.0

Übersicht = eigene Einzelansicht + `FleetAlertsBar`; Original-Gerätesteuerung bleibt, während Takt-Lauf gesperrt; Navigation
Original + „Activity“ + „Takt-Tabellen“ + „Lüfter“; Dockerfiles/Compose Original mit voll qualifizierten Images.

---

## 9. Datenbank und Migrationen

- Eigene Tabellen: `calibration_runs`, `calibration_points` (0020), **`fan_automation`** (**0021**, handgeschrieben, hängt an 0020;
  **0022** `level_at`).
- `calibration_points.kind`: `max`, `step`, **`manual`**; `data` enthält u. a. `fields`, `summary`, `web`, `estats_raw`,
  **`fan_level`**, **`power_spread_pct`**.
- `makemigrations --check` meldet eine ausstehende Migration des **Originals** (Index-Umbenennungen) – nicht ungefragt erzeugen.
  Eigene Migrationen deshalb von Hand schreiben.

---

## 10. Fehlerhistorie und Lehren

1. Erzwungene Takte bei der Max-Messung → falsche Werte. Firmware-Takte nie überschreiben, wenn „Standard“ gemessen wird.
2. „Verbindungsabbruch = gesendet“ angenommen → Umschalten ging nicht mehr. Immer zurücklesen und wiederholen.
3. Abbrechen sendete alle 15 s erneut → `state.restoring`.
4. 2. Max-Messung ohne Wartezeit. 5. Stabilitätsband halb so groß. 6. Drosselungs-Karte falscher Modus. 7. Falscher „Tipp“.
8. Schaukeln der Drosselung. 9. Selbst Messungen gestartet (verboten). 10. Testskript auf dem Server vergessen. 11. Shell-Quoting.
12. **„Drosselung aufheben“ bei aktiver Automatik sprang auf Standardtakt** (297 → 369 MHz) → TMax 95 °C. Jetzt kein Takt beim Aufheben mit Automatik.
13. **Lüfter-Rückmeldung zu früh gelesen** (5 s statt > 5 s) → Anzeige hing bis ~1 min, Stufe doppelt gesendet. Jetzt Rücklesen nach 10 s.
14. Automatik-Seite zeigte „Standardtakt“, obwohl ein anderer Takt lief (zeigte nur den selbst gesetzten). Jetzt immer Wert laut Miner.
15. Automatik/Lüfter-Seite hinkten der Übersicht hinterher (60-s-Median, eigene Timer) → zentrale Abfrage + Live-API.
16. **Frontend-Deploy hinterließ hängenden Web-Container** (virtfs) → Seite ~2 min 503. Nach jedem Deploy `podman ps` prüfen; Lösung Abschnitt 13.
17. Lokale Entwicklungsserver wurden von der App beendet → bei „lokal nicht erreichbar“ neu starten (Abschnitt 14).
18. **Abbruch der Messreihe holte Firmware-Takte bei Lüfter Stufe 1–2** (weg/zurück 04→03→04) → 3.355 W, TMax 100 °C. Mit Automatik kein Weg/zurück mehr, Lernwert sofort, Schutz im Regelmodus sofort.

---

## 11. Offene Punkte

1. **Over-Hot-Benachrichtigung per Messenger** (Telegram/Discord/ntfy des Originals prüfen).
2. **±3 % oder ±2 %**: nach einigen Punkten Spalte „Schwankung“ auswerten.
3. Lernwerte hängen von der Raumtemperatur ab (bewusst nicht erfasst) – beobachten.
4. Lauf 7 läuft – Nutzer entscheidet.
5. Ausstehende Original-Migration – nur nach Rücksprache.

---

## 12. Dateikarte (eigene bzw. stark geänderte Dateien)

```
backend/api/
  views.py           DeviceViewSet: system-latest, calibration (+Automatik beim Start/Fortsetzen), restart, workmode,
                     throttle (A15 MHz, set_mhz/set_by), Sperren
  models.py          CalibrationRun, CalibrationPoint, FanAutomation   migrations/0020_calibration.py, 0021_fan_automation.py
  acinfinity.py      AC-Infinity-Client (Login, Test-Sitzung, Live-Pause, set_fan_level, Sicherungen) – auch vom Data-Service geladen
  fan_views.py       /api/fans/* (status, credentials, backup, restore, test, session, automation)
  live_views.py      /api/live/
data-service/
  app.py             Jobs (live_cycle, poll_all_sources …)
  live_reader.py     zentrale Abfrage 5 s
  miner_cache.py     Zwischenspeicher + Sperre je Gerät
  fan_automation.py  Temperatur-Automatik + Lernwerte
  calibration.py     Takt-Tabellen
  throttle_controller.py  Drosselung (A15 manuell, Nano/Q TH/s)
  collectors/avalon_collector.py   Erfassung aus der zentralen Abfrage
frontend/src/
  lib/LiveContext.jsx              gemeinsame Live-Abfrage 5 s
  pages/OverviewDashboard.jsx, CalibrationPage.jsx, FansPage.jsx
  components/devices/ThrottleCard.jsx, OverHotBanner.jsx   components/ui/info-tip.jsx
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
  MinerGeräte „offline“, Drosselungs-Karte ausgeblendet. **Lokal nichts setzen, was auf echte Geräte wirkt** (Drosselung, Neustart,
  Workmode schicken das lokale Backend direkt an die Miner).
- AC Infinity lokal: Zugangsdaten trägt der Nutzer selbst ein; „Test aktivieren (5 min)“, Live übernimmt danach (7.6).

---

## 15. Rückfragen an die Cowork-Session

Bisherige Arbeit: Cowork-Session „MinerSentinel Docker setup“ (https://claude.ai/code/session_018fS1Z9eLgVGhGqvY6DNrgG).
Fragen in `docs/claude/FRAGEN-AN-COWORK.md` schreiben, Nutzer bitten, dort „Schau in die Fragen-Datei“ zu schreiben;
Antwort steht danach in derselben Datei. Entscheidungen des Nutzers direkt beim Nutzer erfragen.
