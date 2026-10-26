"""Temperatur-Automatik (A15 + AC Infinity fan), right after every central reading (live_reader.py, 5 s).

Regelungslogik nach dem Plan vom 24.10.2026 (docs/claude/Regelungslogik-Luefter-Takt.drawio):
  * Der Nutzer wählt die Lüfterstufe = Basisstufe. Die Automatik regelt den Takt, damit die Chiptemperatur
    (heißester Chip, TMax) bei 79-84 °C liegt; in diesem Bereich keine Taktänderung (Nutzer 25.10.).
  * Start (Basisstufe gewählt, Automatik ein, Neustart des Miners, nach Wartung, nach Takt-Messung, nach Over Hot):
    Starttakt = Lernwert der Basisstufe, sonst Initialwert (Stufe 1 = 100 ... Stufe 10 = 190 MHz).
  * Schritt 1 - hohe Chiptemperatur (ab 85 °C): Lüfter = Basisstufe + 1/+2 (Korrekturphase), Takt -5 ... -100 MHz;
    bei 85-87 °C nach dem Aufenthalt in 79-84 °C davor: unter 10 min -10, sonst -5 (Nutzer 25.10.).
  * Schritt 2 - Korrekturphase: keine Plus-Schritte; zurück zur Basisstufe nach 1 min stabil in 80-84 °C oder unter 80 °C.
  * Schritt 3 - Feintuning: unter 79 °C 70-78 +5; 60-69 +20; unter 60 +40 (kleinster Schritt 5 MHz, Nutzer 25.10.).
    Taktsperre: war die letzte Taktänderung über 84 °C und fällt die Chiptemperatur unter 79 °C, 5 min keine Taktänderung
    (einmal je Änderung; endet über 84 °C).
  * Schritt 4 - Takt 100-440 MHz, Modus folgt dem Takt (Spielraum 10 MHz nach unten).
  * Schritt 5 - Ausnahme: Takt 440 und 3 min stabil unter 70 °C -> Lüfter -1 (stufenweise); Ende über 70 °C.
  * Startpause: nach Neustart, Wartung oder Over Hot keine Takterhöhung bis 79 °C, höchstens 10 min (kalte Chips,
    Nutzer 25.10.). „Nicht erreichbar“ rund um einen Neustart ist ein Neustart-Eintrag, kein Fehler.
  * Lernen: 15 min in 79-84 °C (Nutzer 25.10.) bei unverändertem Lüfter -> Lernwert der aktiven Stufe.
  * Jeder Zyklus und jede Entscheidung wird protokolliert (automation_journal.py).
Takt-Messung (Takt-Tabellen): Taktregelung pausiert, der Lüfter reagiert nur nach Schritt 1/2.
Live only (MINERSENTINEL_ENV != Test). The AC Infinity client is the backend's (backend/api/acinfinity.py,
mounted read-only), so both use the same stored session. The data-service writes only fan_automation.state.
"""
import importlib.util
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor

import automation_journal
import calibration
import miner_cache

logger = logging.getLogger(__name__)

STATE_VERSION = 2
RUN_S = 5                     # one round per central reading
# --- Vorgaben des Nutzers (Plan) ---
T_LOW, T_HIGH = 80, 84        # Zieltemperatur
T_HOT = 85                    # ab hier Tabelle „Hohe Chiptemperaturen“
HIGH_TABLE = [(94, 2, 100), (92, 2, 80), (90, 2, 50), (88, 2, 30), (86, 1, None), (85, 1, None)]   # (ab °C, Lüfter +, Takt −)
DWELL_S, DWELL_MINUS = 600, (10, 5)   # 85/86 °C (None): vorher unter 10 min in 79-84 °C → −10, sonst −5 (Nutzer 25.10.)
START_CLOCK = {1: 100, 2: 110, 3: 120, 4: 130, 5: 140, 6: 150, 7: 160, 8: 170, 9: 180, 10: 190}
CLOCK_MIN, CLOCK_MAX = 100, 440
MODE_TOP = {4: 360, 3: 380, 2: 400, 1: 420, 0: 440}   # oberer Takt je Modus (mode 04 ... mode 00)
MODE_DOWN_MARGIN = 10         # Spielraum nach unten: Wechsel in den tieferen Modus erst 10 MHz unter dessen Obergrenze
WINDOW_S = 30                 # Beobachtungsfenster nach jeder Taktänderung
TENDENCY_S = 30               # Tendenz: gegen den Wert von vor höchstens 30 s (bzw. bei der letzten Taktänderung)
CALM_LOW = 79                 # 79-84 °C: keine Taktänderung (Nutzer 25.10.)
CALM_AFTER_HOT_S = 5 * 60     # Taktsperre: letzte Taktänderung über 84 °C, dann unter 79 °C → 5 min keine Taktänderung
START_HOLD_S = 10 * 60        # Startpause: nach einem Start mit kalten Chips keine Takterhöhung bis 79 °C, höchstens 10 min
RESTART_EXPECT_S = 5 * 60     # Neustart über den Knopf: so lange gilt „nicht erreichbar“ als erwartet (Nutzer 25.10.)
RESTART_RELABEL_S = 3 * 60    # Neustart auf anderem Weg: „antwortet nicht/wieder“ der 3 min davor gehören zum Neustart
WEB_DOWN_LOG_S = 2 * 60       # Einstellungsseite antwortet nicht (Mining-Schnittstelle ok): Eintrag erst nach 2 min
RETURN_STABLE_S = 60          # Rückkehr zur Basisstufe nach 1 min stabil in 80-84 °C
FAN_DOWN_DELAY_S = 30         # niedrigere Korrekturstufe erst, wenn die Tabelle sie 30 s lang verlangt
EXC_BELOW = 70                # Ausnahme: unter 70 °C ...
EXC_STABLE_S = 180            # ... 3 min stabil (Nutzer 24.10.: 3 min für die Ausnahme)
LEARN_S = 15 * 60             # Lernperiode
LEARN_OUT_S = 60              # Ausflug aus 79-84 °C bis 60 s bricht die Lernperiode nicht ab (Nutzer 25.10.)
FAN_MIN, FAN_MAX = 1, 10
# --- bestehende Technik (unverändert übernommen) ---
RESEND_S = 20                 # Modus-/Taktbefehl ohne Übernahme: erneut senden (wie Takt-Tabellen)
OVERRIDE_MHZ = 25             # Takt laut Miner so weit über dem gesetzten: Miner hat den Takt selbst geändert (v1.0.2)
OVERRIDE_AFTER_S = 60         # ... frühestens so lange nach der letzten Taktänderung (Rampe der Firmware, v1.0.2)
FAN_READ_S = 60
FAN_CONFIRM_S = 10            # the controller reports a new level only after > 5 s: read back 10 s after a write
FAN_WRITE_GAP_S = 20
TEMP_KEEP_S = 60
OWN_SWITCH_S = 120            # so lange nach einem eigenen Modusbefehl gilt ein Neustart als Folge davon
TEST_START_AT = 80            # Temperatur-Test (Nutzer 25.10.): Beginn am Lernwert, warten bis Chiptemperatur ≤ 80 °C ...
TEST_STEP_MHZ, TEST_STEPS = 30, 2     # ... dann zweimal +30 MHz ...
TEST_WAIT_S = 120             # ... je nach Übernahme 2 min warten ...
TEST_MEASURE_S = 20 * 60      # ... und 20 min messen
TEST_ABORT_AT = 96            # Abbruch ab 96 °C (Nutzer 25.10.)
LOCAL_TZ = ZoneInfo('Europe/Berlin')  # Uhrzeiten in Protokolltexten
STOCK_PLAUSIBLE_MHZ = 15      # gemerkter Standardtakt nur, wenn er nahe am Standardtakt des Modus (Tabelle) liegt
THERMAL_EVERY_S, THERMAL_N = 30, 10   # thermischer Verlauf je Befehl: alle 30 s, 10 Werte
LOG_KEEP = 40

_ac = []


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _age(value, now):
    try:
        return (now - datetime.fromisoformat(value)).total_seconds()
    except (TypeError, ValueError):
        return float('inf')


def load_ac():
    """The backend's AC Infinity client (stdlib only), from ACINFINITY_MODULE_DIR."""
    if not _ac:
        path = os.path.join(os.environ.get('ACINFINITY_MODULE_DIR') or '/backend_api', 'acinfinity.py')
        spec = importlib.util.spec_from_file_location('acinfinity_client', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ac.append(module)
    return _ac[0]


def _note(st, now, text):
    log = st.setdefault('log', [])
    log.append([_iso(now), text])
    del log[:-LOG_KEEP]
    logger.info('Temperatur-Automatik: %s', text)


# ---------------------------------------------------------------- Modus und Takt

def level_of(mode_key):
    """'2/L3' -> 3 (mode 03); other work modes (High Performance) -> None."""
    return int(mode_key[3:]) if mode_key and mode_key.startswith('2/L') else None


def table_level(mhz):
    """Modus nach der Tabelle, ohne Spielraum (Start)."""
    for level in (4, 3, 2, 1):
        if mhz <= MODE_TOP[level]:
            return level
    return 0


def required_level(mhz, current):
    """Modus für einen Takt: aufwärts ab Obergrenze + 1, abwärts erst 10 MHz unter der Obergrenze des tieferen Modus."""
    if current is None:
        return table_level(mhz)
    level = current
    while level > 0 and mhz >= MODE_TOP[level] + 1:
        level -= 1
    while level < 4 and mhz <= MODE_TOP[level + 1] - MODE_DOWN_MARGIN:
        level += 1
    return level


def points_for(stock, mhz):
    """The four clock points for an average clock, scaled from the stock points of the mode."""
    factor = mhz / (sum(stock) / len(stock))
    return [min(max(int(round(p * factor)), int(p * 0.15)), p) for p in stock]


def high_rule(t):
    for limit, fan_plus, minus in HIGH_TABLE:
        if t >= limit:
            return limit, fan_plus, minus
    return None


def plus_size(t):
    """Plus-Schritt unter 79 °C; kleinster Taktschritt 5 MHz (Nutzer 25.10.)."""
    if t >= 70:
        return 5
    if t >= 60:
        return 20
    return 40


# ---------------------------------------------------------------- Wartung

def _maintenance(row, st, now, ac, journal):
    """Wartung (A15 switched off by hand): normal regulation while the A15 still answers; once it stopped
    answering the fan goes off („Aus“, atType 1); when it answers again the fan goes back on at the level it had
    and the maintenance ends. Returns the miner reading, or None while the A15 is off."""
    mt = st.get('maint') or {}
    if mt.get('since') != str(row['maintenance_at']):
        mt = {'since': str(row['maintenance_at'])}
        _note(st, now, 'Wartung angemeldet: Lüfter geht aus, sobald der A15 ausgeschaltet ist.')
    st['maint'] = mt
    dev_id, fport = row['ac_dev_id'], row['ac_port']
    m = None
    if miner_cache.get(row['ip_address'], 'estats', calibration.CACHE_MAX_AGE):
        try:
            m = calibration.read_miner(row['ip_address'], row['port'])
        except (OSError, RuntimeError, ValueError):
            m = None
    if m is None:
        if not mt.get('seen_off'):
            mt['seen_off'] = True
            mt['fan_before'] = int(st.get('fan_set') or row['level'])
            _note(st, now, 'A15 aus (Wartung).')
        if not mt.get('fan_off') and _age(mt.get('fail_at'), now) >= FAN_WRITE_GAP_S:
            sent, t0 = _now(), time.monotonic()
            ok, err = True, ''
            try:
                ac.set_fan_power(dev_id, fport, False)
                mt['fan_off'] = True
                st['ac_error'] = None
                _note(st, now, 'Wartung: Lüfter aus.')
            except ac.ACError as exc:
                ok, err = False, str(exc)
                st['ac_error'] = err
                mt['fail_at'] = _iso(now)
            journal.event(kind='luefter', trigger='Wartung: Miner ausgeschaltet', phase='wartung',
                          fan_old=st.get('fan_set'), fan_new=0, target='lueftersteuerung',
                          endpoint='AC Infinity: Port %s aus (atType 1)' % fport, request='Lüfter aus',
                          ok=ok, error=err, api_sent_at=sent, api_reply_at=_now(),
                          api_ms=int((time.monotonic() - t0) * 1000), takeover_state='nicht gemessen')
        return None
    if not mt.get('seen_off'):
        return m
    level = mt.get('fan_before') or int(row['level'])
    if _age(mt.get('fail_at'), now) < FAN_WRITE_GAP_S:
        return None
    sent, t0 = _now(), time.monotonic()
    try:
        ac.set_fan_power(dev_id, fport, True, level)
    except ac.ACError as exc:
        st['ac_error'] = str(exc)
        mt['fail_at'] = _iso(now)
        journal.event(kind='luefter', trigger='Wartung beendet: Lüfter wieder an', phase='wartung', fan_new=level,
                      target='lueftersteuerung', endpoint='AC Infinity: Port %s an (atType 2)' % fport,
                      request='Lüfter an, Stufe %d' % level, ok=False, error=str(exc), api_sent_at=sent,
                      api_reply_at=_now(), api_ms=int((time.monotonic() - t0) * 1000), takeover_state='nicht gemessen')
        return None
    st.update({'fan_set': level, 'fan_target': level, 'fan_set_at': _iso(now), 'ac_error': None,
               'fan_read_at': _iso(now - timedelta(seconds=FAN_READ_S - FAN_CONFIRM_S)),
               'maint_end': True, 'maint_ended_at': _iso(now)})
    st.pop('maint', None)
    _note(st, now, 'A15 wieder an: Wartung beendet, Lüfter wieder an auf Stufe %d.' % level)
    journal.event(kind='wartung_ende', trigger='Miner antwortet wieder', phase='wartung', fan_new=level,
                  target='lueftersteuerung', endpoint='AC Infinity: Port %s an (atType 2)' % fport,
                  request='Lüfter an, Stufe %d' % level, ok=True, api_sent_at=sent, api_reply_at=_now(),
                  api_ms=int((time.monotonic() - t0) * 1000), takeover_state='nicht gemessen')
    return m


# ---------------------------------------------------------------- one round

class Round:
    """One regulation round for one automation row.

    Flags in the state: corr (Korrekturphase), exc_active (Ausnahme läuft), cal (Takt-Messung), start_reason (Start
    nach Kapitel 4 steht aus). The shown phase is derived from them at the end of the round."""

    def __init__(self, row, now, ac, journal):
        self.row, self.now, self.ac, self.j = row, now, ac, journal
        self.st = dict(row['state'] or {})
        self.base = int(row['level'])
        self.decisions = []
        self.cycle_log = None
        self.m = None
        self.t = None
        self.ref = None
        self.restarted = False                           # in dieser Runde erkannt (beendet einen Temperatur-Test)
        self.level_changed = False

    # --- helpers
    def note(self, text):
        _note(self.st, self.now, text)

    def decide(self, text, log_id=None):
        self.decisions.append(text)
        if log_id and not self.cycle_log:
            self.cycle_log = log_id

    def phase(self):
        st = self.st
        if st.get('cal'):
            return 'messung'
        if st.get('corr'):
            return 'korrektur'
        if st.get('exc_active'):
            return 'ausnahme'
        return 'normal'

    def base_event(self, **fields):
        st = self.st
        fields.setdefault('phase', self.phase())
        fields.setdefault('chip_temp', self.t)
        fields.setdefault('tendency', st.get('tendency') if self.t is not None else None)
        fields.setdefault('base_old', self.base)
        fields.setdefault('base_new', self.base)
        fields.setdefault('fan_old', st.get('fan_target'))
        fields.setdefault('fan_new', st.get('fan_target'))
        fields.setdefault('clock_old', st.get('clock_set'))
        fields.setdefault('clock_new', st.get('clock_set'))
        fields.setdefault('clock_reported', st.get('clock_now'))
        fields.setdefault('mode_old', st.get('mode_key') or '')
        fields.setdefault('mode_new', st.get('mode_key') or '')
        fields.setdefault('recorded_at', self.now)
        return self.j.event(**fields)

    def plausible(self, key, pts):
        level = level_of(key)
        if not pts or level is None:
            return bool(pts) and level is None          # High Performance: no table value to check against
        return abs(sum(pts) / len(pts) - MODE_TOP[level]) <= STOCK_PLAUSIBLE_MHZ

    def stock(self, key):
        """Standardtakt (vier Punkte) eines Modus: beim eigenen Moduswechsel gemerkt, sonst Profil des
        Throttle-Controllers – beide nur, wenn plausibel (nahe am Standardtakt der Tabelle)."""
        own = (self.st.get('stock') or {}).get(key)
        if own and self.plausible(key, own):
            return own
        prof = ((((self.row['connection_config'] or {}).get('throttle') or {}).get('profiles') or {}).get(key) or {})
        if prof.get('points') and self.plausible(key, prof['points']):
            return prof['points']
        return None

    def stock_for_send(self):
        """Standardtakt des gemeldeten Modus; fehlt er, aus der Form der gemeldeten Punkte auf den Standardtakt
        der Tabelle hochgerechnet (einmal protokolliert), damit ein Taktbefehl nie hängen bleibt."""
        m, key = self.m, self.m['mode_key']
        stock = self.stock(key)
        if stock:
            return stock
        pts = list(m['points'] or [])
        if not pts:
            return None
        level = level_of(key)
        est = [int(round(p * MODE_TOP[level] / (sum(pts) / len(pts)))) for p in pts] if level is not None else pts
        if self.st.get('stock_missing') != key:
            self.st['stock_missing'] = key
            self.base_event(kind='fehler', trigger='Standardtakt von %s unbekannt: aus den gemeldeten Punkten geschätzt' % key,
                            ok=None, details={'grund': 'standardtakt_fehlt', 'modus': key, 'geschaetzt': est})
        return est

    def tendency(self):
        """(Tendenz, Bezugswert): Chiptemperatur jetzt gegenüber dem Wert von vor höchstens 30 s; liegt die letzte
        Taktänderung weniger als 30 s zurück, gegenüber dem Wert bei dieser Änderung."""
        st, now = self.st, self.now
        since = _age(st.get('clock_changed_at'), now)
        if since < TENDENCY_S and st.get('clock_changed_t') is not None:
            ref = st['clock_changed_t']
        else:
            ref = None
            for at, value in st.get('temps') or []:
                if _age(at, now) <= TENDENCY_S + 0.5:
                    ref = value
                    break
        if ref is None:
            ref = self.t
        return int(round(self.t - ref)), ref

    # --- start (Kapitel 4)
    def start(self, reason):
        st = self.st
        learned = (st.get('learned') or {}).get(str(self.base))
        target = int(round(learned['mhz'])) if learned else START_CLOCK.get(self.base, START_CLOCK[1])
        source = 'Lernwert' if learned else 'Initialwert'
        target = min(max(target, CLOCK_MIN), CLOCK_MAX)
        old_fan, old_clock = st.get('fan_target'), st.get('clock_set')
        self.drop_pending('Start (%s)' % reason)
        st.update({'corr': False, 'corr_dwell_s': None, 'exc_active': False, 'clock_set': target, 'queue': [], 'window_until': None,
                   'band_since': None, 'cold_since': None, 'down_since': None,
                   'clock_changed_at': _iso(self.now), 'clock_changed_t': self.t, 'start_reason': None})
        self.set_fan_target(self.base, 'Start: Basisstufe %d' % self.base)
        self.end_learning(None)
        level = table_level(target)
        queue = []
        if level_of(self.m['mode_key']) != level:
            queue.append({'kind': 'modus', 'value': level, 'why': 'Start: Modus für %d MHz' % target})
        queue.append({'kind': 'takt', 'value': target, 'from': old_clock, 'why': 'Start: %s Stufe %d' % (source, self.base)})
        st['queue'] = queue
        log_id = self.base_event(kind='start', trigger=reason, fan_old=old_fan, fan_new=self.base, clock_old=old_clock,
                                 clock_new=target, mode_new=calibration.mode_key(2, level),
                                 details={'quelle': source, 'starttakt': target, 'basisstufe': self.base})
        self.note('%s: Starttakt %d MHz (%s Stufe %d), Lüfter Stufe %d.' % (reason, target, source, self.base, self.base))
        self.decide('Start (%s): %d MHz' % (reason, target), log_id)
        if st.pop('start_cold', False):
            # die Chips waren aus (Neustart, Wartung, Over Hot – auch wenn in derselben Runde ein anderer Startgrund
            # dazukam): der erste Messwert ist kalt (25.10.: 26 °C → +40 MHz → 85 °C)
            until = self.now + timedelta(seconds=START_HOLD_S)
            st['start_hold'] = {'since': _iso(self.now), 'until': _iso(until)}
            text = 'Startpause: keine Takterhöhung bis 79 °C, spätestens %s' % until.astimezone(LOCAL_TZ).strftime('%H:%M')
            self.base_event(kind='startpause', trigger=text, details={'bis': _iso(until), 'grund': reason})
            self.note(text + '.')

    # --- commands (Kapitel 8)
    def drop_pending(self, why):
        """Ein offener Befehl wird abgelöst oder erneut gesendet: Übernahme „nicht gemessen“."""
        p = self.st.get('pending')
        if p and p.get('log_id'):
            self.j.update(p['log_id'], takeover_state='nicht gemessen')
        self.st['pending'] = None
        self.st['settle'] = None

    def confirm_pending(self):
        """Übernahme prüfen (Modus: Miner meldet den Modus; Takt: Miner meldet den gesetzten Wert oder einen anderen
        Wert als vorher). Nach einem Moduswechsel wartet der nächste Taktbefehl, bis die Firmware ihren Standardtakt
        eingestellt hat (gemeldete Punkte geändert oder Neustart, höchstens OWN_SWITCH_S)."""
        st, m, now = self.st, self.m, self.now
        p = st.get('pending')
        if p and p['ok']:
            if p['kind'] == 'modus':
                confirmed = level_of(m['mode_key']) == p['value']
            else:
                pts = list(m['points'] or [])
                confirmed = bool(pts) and (pts == p['points'] or pts != p.get('prev_points'))
            if confirmed:
                took = _age(p['sent_at'], now)
                self.j.update(p['log_id'], takeover_at=now, takeover_s=round(took, 1), takeover_state='gemessen')
                self.follow(p['log_id'])
                st['pending'] = None
                if st.get('queue'):
                    st['queue'].pop(0)
                if p['kind'] == 'modus':
                    st['settle'] = {'key': calibration.mode_key(2, p['value']), 'points_before': p.get('prev_points'),
                                    'since': _iso(now), 'elapsed': m['elapsed']}
                    self.note('Modus mode %02d übernommen (%.0f s).' % (p['value'], took))
                else:
                    st['sent_points'] = p['points']
                    self.clock_done(p['cmd'])
        se = st.get('settle')
        if se:
            pts = list(m['points'] or [])
            restarted = se.get('elapsed') is not None and m['elapsed'] is not None and m['elapsed'] < se['elapsed']
            changed = bool(pts) and pts != se.get('points_before')
            if changed or restarted or _age(se['since'], now) >= OWN_SWITCH_S:
                key = se['key']
                if changed and not self.stock(key) and self.plausible(key, pts):
                    st.setdefault('stock', {})[key] = pts     # der Moduswechsel stellt den Standardtakt ein: merken
                st['settle'] = None
            else:
                se['elapsed'] = m['elapsed']

    def resend_or_send(self):
        """Ohne Übernahme nach RESEND_S erneut senden (bis es klappt); sonst den nächsten Befehl der Folge senden."""
        st = self.st
        p = st.get('pending')
        if p:
            if _age(p['sent_at'], self.now) < RESEND_S:
                return
            self.drop_pending('erneut senden')
            cmd = dict(p['cmd'], attempt=p['attempt'] + 1)
            st['queue'] = [cmd] + [c for c in (st.get('queue') or [])[1:]]
        if st.get('settle'):
            return
        self.send_queue_head()

    def send_queue_head(self):
        st, m = self.st, self.m
        while st.get('queue'):
            cmd = st['queue'][0]
            if cmd['kind'] == 'modus':
                if level_of(m['mode_key']) == cmd['value']:
                    st['queue'].pop(0)
                    continue
                if not m.get('conf_fresh', True):
                    # der Umschaltbefehl braucht die Einstellungsseite (Pool-Felder): nie mit alten Daten. Er wartet
                    # außerhalb der Folge, die Taktbefehle laufen weiter – auch weitere Absenkungen ab 85 °C (Nutzer 26.10.)
                    st['queue'].pop(0)
                    current = level_of(m['mode_key'])
                    if current is not None:
                        # der jetzige Modus kann höchstens seinen Standardtakt: ein höherer Takt wartet mit dem Modus
                        # (sonst stiege der gesetzte Takt, während der Miner beim Standardtakt bleibt – Review 26.10.)
                        for c in st['queue']:
                            if c['kind'] == 'takt' and c['value'] > MODE_TOP[current]:
                                c['value'] = MODE_TOP[current]
                        if (st.get('clock_set') or 0) > MODE_TOP[current]:
                            st['clock_set'] = MODE_TOP[current]
                    if st.get('mode_wait') is None:
                        self.base_event(kind='modus_wartet', trigger='Moduswechsel wartet: Einstellungsseite nicht erreichbar',
                                        ok=None, mode_new=calibration.mode_key(2, cmd['value']))
                        self.note('Moduswechsel auf mode %02d wartet: Einstellungsseite nicht erreichbar.' % cmd['value'])
                    st['mode_wait'] = cmd['value']
                    continue
                self.send_mode(cmd)
                return
            stock = self.stock_for_send()
            if not stock or not m['points']:
                return                                      # the miner reports no clocks yet (booting)
            pts = points_for(stock, cmd['value'])
            if list(m['points']) == pts:
                # Zieltakt = Takt laut Miner: kein Taktbefehl, gilt als übernommen
                st['queue'].pop(0)
                st['sent_points'] = pts
                self.base_event(kind='takt', trigger=cmd.get('why') or '', target='miner-takt', ok=None,
                                clock_old=cmd.get('from'), clock_new=cmd['value'], takeover_state='',
                                details={'kein_befehl': 'Takt laut Miner = Zieltakt', 'punkte': pts})
                self.clock_done(cmd)
                continue
            self.send_clock(cmd, pts)
            return

    def send_clock(self, cmd, pts):
        st, m = self.st, self.m
        attempt = cmd.get('attempt', 1)
        request = 'ascset 0,frequency,%s-0-0' % ':'.join(str(p) for p in pts)
        sent, t0 = _now(), time.monotonic()
        ok, reply, err = True, '', ''
        try:
            reply = calibration.set_frequency(self.row['ip_address'], self.row['port'], pts) or 'set OK'
        except (OSError, RuntimeError) as exc:
            ok, err = False, str(exc)
        ms = int((time.monotonic() - t0) * 1000)
        details = dict(cmd.get('details') or {}, punkte=pts)
        log_id = self.base_event(kind='takt', trigger=cmd.get('why') or '', target='miner-takt',
                                 endpoint='cgminer %s:%s ascset frequency' % (self.row['ip_address'], self.row['port'] or 4028),
                                 request=request, response=reply if ok else '', status='set OK' if ok else 'Fehler',
                                 ok=ok, error=err, attempt=attempt, api_sent_at=sent, api_reply_at=_now(), api_ms=ms,
                                 clock_old=cmd.get('from'), clock_new=cmd['value'], temp_before=self.t,
                                 takeover_state='offen' if ok else 'nicht gemessen', details=details)
        st['pending'] = {'kind': 'takt', 'value': cmd['value'], 'points': pts, 'prev_points': list(m['points']),
                         'sent_at': _iso(self.now), 'attempt': attempt, 'log_id': log_id, 'ok': ok, 'cmd': cmd}
        self.decide('Takt %d MHz gesendet%s' % (cmd['value'], '' if ok else ' (Fehler)'), log_id)
        if not ok:
            self.note('Taktbefehl %d MHz fehlgeschlagen: %s' % (cmd['value'], err))

    def send_mode(self, cmd):
        st, m = self.st, self.m
        level, attempt = cmd['value'], cmd.get('attempt', 1)
        conf = m.get('conf')
        sent, t0 = _now(), time.monotonic()
        ok, err = True, ''
        if not conf:
            ok, err = False, 'A15-Einstellungsseite nicht gelesen'
        else:
            try:
                calibration.a15_set_mode(self.row['ip_address'], conf, 2, level)
            except (OSError, RuntimeError) as exc:
                ok, err = False, str(exc)
        ms = int((time.monotonic() - t0) * 1000)
        key = calibration.mode_key(2, level)
        log_id = self.base_event(kind='modus', trigger=cmd.get('why') or '', target='miner-modus',
                                 endpoint='http://%s/cgconf.cgi' % self.row['ip_address'],
                                 request='mode=2, power_level=%02d (Pool-Felder unverändert, nicht protokolliert)' % level,
                                 status='gesendet' if ok else 'Fehler', ok=ok, error=err, attempt=attempt,
                                 api_sent_at=sent, api_reply_at=_now(), api_ms=ms, mode_new=key, temp_before=self.t,
                                 takeover_state='offen' if ok else 'nicht gemessen')
        st['pending'] = {'kind': 'modus', 'value': level, 'sent_at': _iso(self.now), 'attempt': attempt,
                         'prev_points': list(m['points'] or []), 'log_id': log_id, 'ok': ok, 'cmd': cmd}
        st['mode_sent_at'] = _iso(self.now)
        self.decide('Modus %02d gesendet%s' % (level, '' if ok else ' (Fehler)'), log_id)
        self.note('Modus → mode %02d%s.' % (level, '' if ok else ' (Fehler: %s)' % err))

    def clock_done(self, cmd):
        """Takt übernommen: Beobachtungsfenster beginnt."""
        st = self.st
        st['window_until'] = _iso(self.now + timedelta(seconds=WINDOW_S))

    def follow(self, log_id):
        """Thermischen Verlauf nachtragen (alle 30 s, 10 Werte)."""
        if log_id:
            fl = self.st.setdefault('thermal', [])
            fl.append({'id': log_id, 'at': _iso(self.now), 'vals': []})
            del fl[:-50]

    def update_thermal(self):
        keep = []
        for f in self.st.get('thermal') or []:
            if _age(f['at'], self.now) >= THERMAL_EVERY_S * (len(f['vals']) + 1):
                f['vals'].append(self.t)
                self.j.update(f['id'], thermal=f['vals'])
            if len(f['vals']) < THERMAL_N:
                keep.append(f)
        self.st['thermal'] = keep

    def queue_clock(self, value, why, hot=False, step=None):
        """Takt setzen (Schritt 4): Grenzen, Modus nach Regel; bei hoher Chiptemperatur erst Takt, dann Modus, dann Takt."""
        st, m = self.st, self.m
        old = st.get('clock_set')
        if value < CLOCK_MIN:
            log_id = self.base_event(kind='fehler', trigger=why, ok=False,
                                     error='Berechneter Takt %d MHz liegt unter %d MHz: nicht gesendet' % (value, CLOCK_MIN),
                                     details={'grund': 'takt_unter_100', 'berechnet': value})
            self.note('Takt %d MHz unter %d MHz: nicht gesendet, Takt bleibt %s MHz.' % (value, CLOCK_MIN, old))
            self.decide('Fehler: Takt unter 100 MHz', log_id)
            return False
        value = min(value, CLOCK_MAX)
        current = level_of(m['mode_key'])
        level = required_level(value, current)
        common = {'from': old, 'why': why, 'step': step, 't0': self.t,
                  'details': {'schritt': step} if step is not None else {}}
        queue = []
        if hot:
            queue.append(dict(common, kind='takt', value=value))
            if level != current:
                queue.append({'kind': 'modus', 'value': level, 'why': why + ' (Modus danach)'})
                queue.append(dict(common, kind='takt', value=value, why=why + ' (erneut nach Moduswechsel)'))
        else:
            if level != current:
                queue.append({'kind': 'modus', 'value': level, 'why': why + ' (Modus zuerst)'})
            queue.append(dict(common, kind='takt', value=value))
        self.drop_pending('abgelöst: ' + why)
        st['queue'] = queue
        st['clock_set'] = value
        st['clock_changed_at'] = _iso(self.now)
        st['clock_changed_t'] = self.t
        st['window_until'] = None
        return True

    # --- fan (Lüftersteuerung)
    def set_fan_target(self, level, why):
        level = min(max(int(level), FAN_MIN), FAN_MAX)
        st = self.st
        if level != st.get('fan_target'):
            fp = st.get('fan_pending')
            if fp and fp.get('log_id'):
                self.j.update(fp['log_id'], takeover_state='nicht gemessen')
            st['fan_pending'] = None
            st['fan_target'] = level
            st['fan_why'] = why

    def write_fan(self):
        st, now, ac = self.st, self.now, self.ac
        fport = self.row['ac_port']
        target = st.get('fan_target')
        if target is None:
            return
        read_after_write = _age(st.get('fan_read_at'), now) <= _age(st.get('fan_set_at'), now) - FAN_CONFIRM_S
        fp = st.get('fan_pending')
        if fp and st.get('fan_read') == target and read_after_write:
            self.j.update(fp['log_id'], takeover_at=now, takeover_s=round(_age(fp['at'], now), 1),
                          takeover_state='gemessen')
            self.follow(fp['log_id'])
            st['fan_pending'] = fp = None
        # eine neue Lüfterstufe sofort (Plan: „Lüfter sofort“); erneut senden (Fehler oder andere Rückmeldung)
        # frühestens FAN_WRITE_GAP_S nach dem letzten Versuch
        new_level = target != st.get('fan_set') and _age(st.get('fan_fail_at'), now) >= FAN_WRITE_GAP_S
        mismatch = (st.get('fan_read') is not None and st['fan_read'] != target and read_after_write
                    and _age(st.get('fan_set_at'), now) >= FAN_WRITE_GAP_S)
        if not new_level and not mismatch:
            return
        old = st.get('fan_set')
        attempt = 1 + (fp['attempt'] if fp and fp.get('level') == target else 0)
        if fp and fp.get('log_id'):
            self.j.update(fp['log_id'], takeover_state='nicht gemessen')
        sent, t0 = _now(), time.monotonic()
        ok, err, reply = True, '', ''
        try:
            res = ac.set_fan_level(self.row['ac_dev_id'], fport, target)
            reply = 'Code %s' % ((res or {}).get('code') if isinstance(res, dict) else 'ok')
            st['ac_error'] = None
        except ac.ACError as exc:
            ok, err = False, str(exc)
            st['ac_error'] = err
        why = st.get('fan_why') or 'Lüfterstufe %s → %d' % (old, target)
        log_id = self.base_event(kind='luefter', trigger=why, target='lueftersteuerung',
                                 endpoint='AC Infinity: Port %s Stufe (onSpead)' % fport,
                                 request='Stufe %d' % target, response=reply, status=reply or ('Fehler' if not ok else ''),
                                 ok=ok, error=err, attempt=attempt, api_sent_at=sent, api_reply_at=_now(),
                                 api_ms=int((time.monotonic() - t0) * 1000), fan_old=old, fan_new=target,
                                 temp_before=self.t, takeover_state='offen' if ok else 'nicht gemessen')
        st['fan_set_at'] = _iso(now)
        st['fan_pending'] = {'level': target, 'at': _iso(now), 'attempt': attempt, 'log_id': log_id if ok else None}
        if ok:
            if target != old:
                self.note('Lüfter Stufe %d gesetzt.' % target)
            st['fan_set'] = target
            st['fan_fail_at'] = None
            st['fan_why'] = None
            st['fan_read_at'] = _iso(now - timedelta(seconds=FAN_READ_S - FAN_CONFIRM_S))
        else:
            st['fan_fail_at'] = _iso(now)
            self.note('Lüfterbefehl Stufe %d fehlgeschlagen: %s' % (target, err))
        self.decide('Lüfter Stufe %d%s' % (target, '' if ok else ' (Fehler)'), log_id)

    def read_fan(self):
        """Lüfterstufe lesen: alle 60 s (10 s nach dem Setzen), auch nach einem Fehler erst wieder nach 60 s."""
        st, now = self.st, self.now
        if _age(st.get('fan_read_at'), now) < FAN_READ_S or _age(st.get('fan_read_fail_at'), now) < FAN_READ_S:
            return
        try:
            st['fan_read'] = self.ac.speed_of(self.row['ac_dev_id'], self.row['ac_port'])
            st['fan_read_at'] = _iso(now)
            st['fan_read_fail_at'] = None
        except self.ac.ACError as exc:
            st['ac_error'] = str(exc)
            st['fan_read_fail_at'] = _iso(now)

    # --- learning (Kapitel 7)
    def end_learning(self, reason):
        st = self.st
        if st.get('learn_since') and reason:
            self.base_event(kind='lernen_ende', trigger=reason, details={'ergebnis': 'abgebrochen', 'grund': reason,
                                                                         'seit': st['learn_since']})
        st['learn_since'] = None
        st['learn_sum'], st['learn_n'], st['learn_clock_sum'] = 0, 0, 0.0

    def learn(self):
        """Lernperiode 15 min auf derselben Lüfterstufe. Taktschritte setzen sie nicht zurück, kurze Ausflüge
        aus 79-84 °C bis 60 s auch nicht (Nutzer 25.10.: sonst entstand nie ein Lernwert). Neu beginnt sie bei Wechsel der
        Lüfterstufe, Moduswechsel, hoher Chiptemperatur (ab 85 °C) oder länger als 60 s außerhalb 79-84 °C (Lernbereich
        = Bereich ohne Taktänderung, Nutzer 25.10.).
        Gespeichert: Mittelwert des gesetzten Takts und der Chiptemperatur der 15 min."""
        st, t, now = self.st, self.t, self.now
        level = st.get('fan_target')
        mode_cmd = any(c.get('kind') == 'modus' for c in (st.get('queue') or [])) or \
            (st.get('pending') or {}).get('kind') == 'modus' or st.get('settle')
        reason = None
        if t >= T_HOT:
            reason = 'Chiptemperatur %d °C (hohe Chiptemperatur)' % t
        elif st.get('corr'):
            reason = 'Korrekturphase'
        elif level != st.get('fan_set'):
            reason = 'Lüfterstufe ändert sich'
        elif mode_cmd:
            reason = 'Moduswechsel'
        elif st.get('learn_since') and st.get('learn_level') != level:
            reason = 'Lüfterstufe geändert'
        if not (CALM_LOW <= t <= T_HIGH) and not reason:
            st['learn_out_since'] = st.get('learn_out_since') or _iso(now)
            if _age(st['learn_out_since'], now) > LEARN_OUT_S:
                reason = 'Chiptemperatur länger als 60 s außerhalb 79–84 °C (%d °C)' % t
        elif CALM_LOW <= t <= T_HIGH:
            st['learn_out_since'] = None
        if reason:
            self.end_learning(reason)
            st['learn_out_since'] = None
            return
        if not st.get('learn_since'):
            if not (CALM_LOW <= t <= T_HIGH):
                return                                   # eine Lernperiode beginnt nur im Zielbereich
            st.update({'learn_since': _iso(now), 'learn_level': level, 'learn_sum': 0, 'learn_n': 0,
                       'learn_clock_sum': 0.0, 'learn_out_since': None})
            self.base_event(kind='lernen_start', trigger='Chiptemperatur %d °C in 79–84 °C' % t,
                            details={'takt': st.get('clock_set'), 'luefterstufe': level})
        st['learn_sum'] += t
        st['learn_clock_sum'] = (st.get('learn_clock_sum') or 0.0) + float(st.get('clock_set') or 0)
        st['learn_n'] += 1
        if _age(st['learn_since'], now) >= LEARN_S:
            n = max(st['learn_n'], 1)
            avg_t = round(st['learn_sum'] / n, 1)
            mhz = round(st['learn_clock_sum'] / n, 1)
            key = str(level)
            prev = (st.get('learned') or {}).get(key)
            entry = {'mhz': mhz, 'temp_avg': avg_t, 'at': _iso(now), 'count': (prev or {}).get('count', 0) + 1}
            st.setdefault('learned', {})[key] = entry
            log_id = self.base_event(kind='lernwert', trigger='15 min auf Stufe %s, Ø %.1f °C' % (key, avg_t),
                                     details={'luefterstufe': level, 'takt': mhz, 'temp_mittel': avg_t,
                                              'anzahl': entry['count'], 'vorher': prev})
            self.base_event(kind='lernen_ende', trigger='15 min erreicht', details={'ergebnis': 'gespeichert'})
            self.note('Lernwert Stufe %s: %.1f MHz (Mittel), Ø %.1f °C (%d. Mal).' % (key, mhz, avg_t, entry['count']))
            self.decide('Lernwert gespeichert', log_id)
            st.update({'learn_since': _iso(now), 'learn_sum': 0, 'learn_n': 0, 'learn_clock_sum': 0.0})

    # --- Neustart sauber anzeigen (Nutzer 25.10.)
    def restart_expected(self):
        """„Nicht erreichbar“ gehört zu einem Neustart: Knopf „A15 neu starten“ vor höchstens 5 min oder Startpause."""
        hold = self.st.get('start_hold')
        return bool(self.st.get('restart_req') or (hold and _age(hold['until'], self.now) < 0))

    def remember_net(self, log_id, kind, took=None, err='', partner=None):
        """„Antwortet nicht/wieder“ merken, um sie bei einem später erkannten Neustart umzubenennen; ein „wieder“ merkt
        sich sein „antwortet nicht“ (partner: [id, Meldung, Zeit]), damit das Paar gemeinsam umbenannt wird."""
        net = [x for x in self.st.get('net_log') or [] if _age(x[2], self.now) <= RESTART_RELABEL_S]
        if log_id:
            net.append([log_id, kind, _iso(self.now), took, err, partner])
        self.st['net_log'] = net

    def relabel_restart(self, since=None):
        """Neustart erkannt: „Miner antwortet nicht/wieder“ der 3 min davor sind Teil des Neustarts – beim Neustart über
        den Knopf nur die ab dem Klick (since). Die technische Meldung wandert in die Einzelheiten (nicht rot)."""
        def after_click(at):
            return not since or _age(at, self.now) <= _age(since, self.now)

        def offline(log_id, err):
            self.j.update(log_id, kind='neustart_offline', trigger='A15 startet neu – nicht erreichbar (erwartet)', ok=None,
                          error='', details={'meldung': err or ''})

        for log_id, kind, at, took, *rest in self.st.get('net_log') or []:
            if _age(at, self.now) > RESTART_RELABEL_S or not after_click(at):
                continue
            if kind == 'miner_offline':
                offline(log_id, rest[0] if rest else '')
            elif kind == 'miner_online':
                self.j.update(log_id, kind='neustart_online', trigger='A15 nach Neustart wieder erreichbar'
                              + (' nach %d s' % took if took is not None else ''))
                partner = rest[1] if len(rest) > 1 else None
                if partner and after_click(partner[2]):
                    offline(partner[0], partner[1])      # das Paar gemeinsam, auch wenn „antwortet nicht“ älter als 3 min ist
        self.st['net_log'] = []

    # --- Einstellungsseite der A15 (Nutzer 26.10.)
    def web_status(self, m):
        """Antwortet nur die Einstellungsseite nicht, regelt die Automatik mit den zuletzt gelesenen Einstellungen weiter;
        erst nach 2 min ein Eintrag (nicht rot), beim Zurückkommen die Dauer."""
        st, now = self.st, self.now
        if m.get('web_error'):
            st['web_down_since'] = st.get('web_down_since') or _iso(now)
            if not st.get('web_down_logged') and _age(st['web_down_since'], now) >= WEB_DOWN_LOG_S:
                st['web_down_logged'] = True
                self.base_event(kind='einstellungsseite_aus', trigger='A15-Einstellungsseite antwortet nicht – Mining-Schnittstelle ok',
                                ok=None, details={'seit': st['web_down_since'], 'meldung': m['web_error']})
                self.note('A15-Einstellungsseite antwortet nicht – Regelung läuft weiter.')
        elif st.get('web_down_since'):
            if st.get('web_down_logged'):
                text = 'A15-Einstellungsseite wieder erreichbar nach %d min' % int(_age(st['web_down_since'], now) // 60)
                self.base_event(kind='einstellungsseite_an', trigger=text, details={'seit': st['web_down_since']})
                self.note(text + '.')
            st['web_down_since'], st['web_down_logged'] = None, False

    # --- the rules (Kapitel 5)
    def window_over(self):
        wu = self.st.get('window_until')
        return _age(wu, self.now) >= 0 if wu else True

    def busy(self):
        return bool(self.st.get('pending') or self.st.get('queue') or self.st.get('settle'))

    def high_temperature(self):
        """Schritt 1: Lüfter = Basisstufe + 1/+2 sofort (niedrigere Korrekturstufe erst nach 30 s), Takt − Tabellenwert:
        beim Eintritt in die Korrekturphase sofort, danach erst nach dem Beobachtungsfenster und nur bei nicht
        fallender Tendenz."""
        st, t, now = self.st, self.t, self.now
        limit, fan_plus, minus = high_rule(t)
        first = not st.get('corr')
        if first:
            # Aufenthalt in 79-84 °C vor dem Überschreiten von 84 °C: gilt für die ganze Korrekturphase (Nutzer 25.10.)
            st['corr_dwell_s'] = st.get('band_dwell_s') or 0
        dwell = st.get('corr_dwell_s') or 0
        if minus is None:
            minus = DWELL_MINUS[0] if dwell < DWELL_S else DWELL_MINUS[1]
        want = min(self.base + fan_plus, FAN_MAX)
        active = st.get('fan_target') or self.base
        if want > active:
            self.set_fan_target(want, 'Chiptemperatur %d °C: Basisstufe %d + %d' % (t, self.base, fan_plus))
            st['down_since'] = None
        elif want < active and st.get('corr'):
            st['down_since'] = st.get('down_since') or _iso(now)
            if _age(st['down_since'], now) >= FAN_DOWN_DELAY_S:
                self.set_fan_target(want, 'Chiptemperatur %d °C seit 30 s: Basisstufe %d + %d' % (t, self.base, fan_plus))
                st['down_since'] = None
        else:
            st['down_since'] = None
        if st.get('band_since'):
            self.stability_end('rueckkehr', 'abgebrochen', 'Chiptemperatur %d °C' % t)
        if first:
            st['corr'] = True
            st['exc_active'] = False
            if st.get('cold_since'):
                self.stability_end('ausnahme', 'abgebrochen', 'Chiptemperatur %d °C' % t)
            self.base_event(kind='hohe_temperatur', trigger='Chiptemperatur %d °C (Tabelle ab %d °C)' % (t, limit),
                            fan_new=st['fan_target'], details={'ab': limit, 'luefter_plus': fan_plus, 'takt_minus': minus,
                                                               'aufenthalt_s': int(dwell)})
            self.note('Chiptemperatur %d °C: Korrekturphase, Lüfter Stufe %d.' % (t, st['fan_target']))
        if st.get('cal'):
            return                                       # Takt-Messung: nur der Lüfter reagiert
        old = st.get('clock_set')
        if old is None:
            return
        if first or (self.window_over() and not self.busy() and st['tendency'] >= 0):
            why = 'Chiptemperatur %d °C (ab %d °C): Takt −%d MHz' % (t, limit, minus)
            if high_rule(t)[2] is None:
                why = 'Chiptemperatur %d °C nach %d min in 79–84 °C: Takt −%d MHz' % (t, int(dwell // 60), minus)
            if self.queue_clock(int(round(old)) - minus, why, hot=True, step=-minus):
                self.note('Chiptemperatur %d °C: Takt %s → %d MHz.' % (t, old, int(round(old)) - minus))

    def correction_phase(self):
        """Schritt 2: in der Korrekturphase zurück zur Basisstufe (1 min stabil in 80-84 °C oder unter 80 °C)."""
        st, t, now = self.st, self.t, self.now
        if t < T_LOW:
            self.stability_end('rueckkehr', 'erfuellt', 'Chiptemperatur unter 80 °C')
            self.return_to_base('Chiptemperatur %d °C unter 80 °C' % t)
            return
        if not st.get('band_since'):
            st['band_since'] = _iso(now)
            self.base_event(kind='stabilitaet_start', trigger='Rückkehr: Chiptemperatur %d °C in 80–84 °C' % t,
                            details={'pruefung': 'rueckkehr', 'dauer_s': RETURN_STABLE_S})
        elif _age(st['band_since'], now) >= RETURN_STABLE_S:
            self.stability_end('rueckkehr', 'erfuellt', '1 min stabil in 80–84 °C')
            self.return_to_base('1 min stabil in 80–84 °C')

    def stability_end(self, check, result, why):
        st = self.st
        key = 'band_since' if check == 'rueckkehr' else 'cold_since'
        if st.get(key):
            self.base_event(kind='stabilitaet_ende', trigger=why,
                            details={'pruefung': check, 'ergebnis': result, 'seit': st[key]})
        st[key] = None

    def return_to_base(self, why):
        st = self.st
        st['corr'] = False
        st['corr_dwell_s'] = None                        # beim nächsten Überschreiten von 84 °C neu gemessen
        st['band_since'] = None
        st['down_since'] = None
        self.set_fan_target(self.base, 'Rückkehr zur Basisstufe: ' + why)
        log_id = self.base_event(kind='rueckkehr', trigger=why, fan_new=self.base)
        self.note('Rückkehr zur Basisstufe %d (%s).' % (self.base, why))
        self.decide('Rückkehr zur Basisstufe', log_id)

    def fine_tuning(self):
        """Schritt 3: 79-84 °C keine Taktänderung; darunter +5/+20/+40 MHz (nicht in der Korrekturphase, nicht bei
        steigender Tendenz, nicht während der Taktsperre oder der Startpause)."""
        st, t, d = self.st, self.t, self.st['tendency']
        old = st.get('clock_set')
        if old is None or t >= CALM_LOW or st.get('corr') or st.get('calm') or d > 0:
            return
        if st.get('start_hold'):
            return                                       # Startpause nach einem Start mit kalten Chips
        current = level_of(self.m['mode_key'])
        if st.get('mode_wait') is not None and current is not None and old >= MODE_TOP[current]:
            return                                       # mehr geht erst mit dem wartenden Moduswechsel
        old = int(round(old))
        if old < CLOCK_MAX:
            size = plus_size(t)
            self.queue_clock(old + size, 'Chiptemperatur %d °C: +%d MHz' % (t, size), step=size)

    def calm_after_hot(self):
        """Taktsperre (Nutzer 25.10.): war die letzte Taktänderung über 84 °C und fällt die Chiptemperatur unter 79 °C,
        5 min keine Taktänderung – gezählt ab dem ersten Unterschreiten, einmal je Taktänderung; über 84 °C endet sie."""
        st, t, now = self.st, self.t, self.now
        calm = st.get('calm')
        if calm:
            if t > T_HIGH:
                st['calm'] = None
                self.base_event(kind='taktsperre_ende', trigger='Taktsperre beendet (Chiptemperatur %d °C, über 84 °C)' % t,
                                details={'seit': calm['since']})
                self.note('Taktsperre beendet (%d °C).' % t)
            elif _age(calm['until'], now) >= 0:
                st['calm'] = None                        # abgelaufen: der Eintrag zum Beginn nennt das Ende
            return
        changed_at = st.get('clock_changed_at')
        if (t < CALM_LOW and (st.get('clock_changed_t') or 0) > T_HIGH and changed_at
                and st.get('calm_for') != changed_at):
            until = now + timedelta(seconds=CALM_AFTER_HOT_S)
            st['calm'] = {'since': _iso(now), 'until': _iso(until)}
            st['calm_for'] = changed_at
            text = 'Taktsperre 5 min bis %s' % until.astimezone(LOCAL_TZ).strftime('%H:%M')
            self.base_event(kind='taktsperre', trigger='%s: letzte Taktänderung bei %d °C, jetzt %d °C'
                            % (text, st['clock_changed_t'], t), details={'bis': _iso(until)})
            self.note(text + '.')

    def exception(self):
        """Schritt 5: Takt 440 und 3 min stabil unter 70 °C → aktive Lüfterstufe − 1; Ende über 70 °C."""
        st, t, now = self.st, self.t, self.now
        if st.get('exc_active') and t > EXC_BELOW:
            st['exc_active'] = False
            self.base_event(kind='ausnahme_ende', trigger='Chiptemperatur %d °C über 70 °C' % t,
                            details={'luefterstufe_bleibt': st.get('fan_target')})
            self.note('Ausnahme beendet (%d °C), Lüfter bleibt auf Stufe %s.' % (t, st.get('fan_target')))
        at_max = int(round(st.get('clock_set') or 0)) >= CLOCK_MAX and not self.busy()
        if at_max and t < EXC_BELOW:
            if not st.get('cold_since'):
                st['cold_since'] = _iso(now)
                self.base_event(kind='stabilitaet_start', trigger='Ausnahme: Takt 440 MHz, Chiptemperatur %d °C' % t,
                                details={'pruefung': 'ausnahme', 'dauer_s': EXC_STABLE_S})
            elif _age(st['cold_since'], now) >= EXC_STABLE_S:
                active = st.get('fan_target') or self.base
                self.stability_end('ausnahme', 'erfuellt', '3 min stabil unter 70 °C')
                if active > FAN_MIN:
                    self.set_fan_target(active - 1, 'Ausnahme: 440 MHz und 3 min unter 70 °C')
                    st['exc_active'] = True
                    log_id = self.base_event(kind='ausnahme', trigger='Takt 440 MHz, 3 min stabil unter 70 °C',
                                             fan_old=active, fan_new=active - 1)
                    self.note('Ausnahme: Lüfter Stufe %d → %d.' % (active, active - 1))
                    self.decide('Ausnahme: Lüfter −1', log_id)
                    st['cold_since'] = _iso(now)         # erneut 3 min Stabilität vor der nächsten Senkung
                    self.base_event(kind='stabilitaet_start', trigger='Ausnahme: erneut 3 min prüfen',
                                    details={'pruefung': 'ausnahme', 'dauer_s': EXC_STABLE_S})
        elif st.get('cold_since'):
            self.stability_end('ausnahme', 'abgebrochen',
                               'Chiptemperatur %d °C' % t if t >= EXC_BELOW else 'Takt nicht mehr 440 MHz')

    def rules(self):
        st = self.st
        hold = st.get('start_hold')
        if hold and (self.t >= CALM_LOW or _age(hold['until'], self.now) >= 0):
            st['start_hold'] = None                      # Startpause endet bei 79 °C, spätestens nach 10 min
            since = hold.get('since') or _iso(datetime.fromisoformat(hold['until']) - timedelta(seconds=START_HOLD_S))
            mins = int(_age(since, self.now) // 60)
            if self.t >= CALM_LOW:
                text = 'Startpause beendet: 79 °C erreicht (nach %d min)' % mins
            else:
                text = 'Startpause beendet: 10 min vorbei (Chiptemperatur %d °C)' % self.t
            self.base_event(kind='startpause_ende', trigger=text, details={'seit': since})
            self.note(text + '.')
        if st.get('test') and not st.get('cal'):
            self.test_rules()
            return
        if not st.get('cal'):
            self.calm_after_hot()                        # Takt-Messung: Taktregelung pausiert
        if self.t >= T_HOT:
            # Sicherheit zuerst: beim Eintritt in die Korrekturphase ersetzt die Taktsenkung einen laufenden Befehl
            if not st.get('corr') and not st.get('cal') and (st.get('queue') or st.get('pending')):
                self.drop_pending('abgelöst: hohe Chiptemperatur')
                st['queue'] = []
            self.high_temperature()
            return
        st['down_since'] = None
        if st.get('corr'):
            self.correction_phase()
        if st.get('cal'):
            return
        if not self.busy() and self.window_over():
            self.fine_tuning()
        if not st.get('corr'):
            self.exception()

    # --- Temperatur-Test (Nutzer 25.10., v2.0.6)
    def temp_test(self, valid, over_hot, cal):
        """Takt auf den Lernwert der Basisstufe, warten bis die Chiptemperatur ≤ 80 °C ist; dann zweimal +30 MHz (Modus
        nach der allgemeinen Regel), je nach Übernahme 2 min warten und 20 min messen; danach Normalbetrieb (Start mit
        dem Lernwert). Während des Tests fester Takt, Lüfter auf der Basisstufe, kein Lernen. Abbruch ab 96 °C, bei
        Over Hot, Takt-Messung, Neustart des Miners oder Wechsel der Basisstufe; danach gelten die normalen Regeln."""
        st, now = self.st, self.now
        at = self.row.get('temp_test_at')
        tt = st.get('test')
        if not at:
            if tt:
                st.pop('test', None)
                self.note('Temperatur-Test beendet (vom Nutzer bzw. durch Automatik aus/Wartung).')
            return
        started = at if isinstance(at, datetime) else datetime.fromisoformat(str(at))
        if tt and (tt.get('started') != started.isoformat() or 'step' not in tt):
            tt = None                                    # ein anderer Test (oder einer vor v2.0.6) beginnt neu
        reason = None
        if over_hot:
            reason = 'Over Hot'
        elif valid and self.t >= TEST_ABORT_AT:
            reason = 'Chiptemperatur %d °C (Grenze %d °C)' % (self.t, TEST_ABORT_AT)
        elif cal:
            reason = 'Takt-Messung gestartet'
        elif tt and self.restarted:                      # nur ein laufender Test: vor dem Beginn setzen Start und
            reason = 'Neustart des Miners'               # Test-Beginn denselben Lernwert
        elif tt and self.level_changed:
            reason = 'Basisstufe gewählt (Stufe %d)' % self.base
        done = (not reason and bool(tt) and tt['step'] == TEST_STEPS and tt['phase'] == 'messen'
                and _age(tt['measure_ends'], now) >= 0)
        if reason or done:
            self.base_event(kind='test_ende', trigger='Temperatur-Test fertig' if done else 'Temperatur-Test abgebrochen: ' + reason,
                            ok=None, details={'ergebnis': 'fertig' if done else 'abgebrochen', 'grund': reason or 'alle Schritte',
                                              'gestartet': started.isoformat()})
            self.note('Temperatur-Test %s.' % ('fertig: zurück zum Normalbetrieb' if done else 'abgebrochen: ' + reason))
            st.pop('test', None)
            st['test_clear'] = True                      # run_fan_automation löscht temp_test_at
            if done:
                st['start_reason'] = 'nach Temperatur-Test'
            return
        if tt:
            return
        # Beginn: Lernwert der Basisstufe (sonst Initialwert wie beim Start), Lüfter auf die Basisstufe
        self.end_learning('Temperatur-Test')
        if st.get('corr'):
            self.return_to_base('Temperatur-Test beginnt')
        self.stability_end('ausnahme', 'abgebrochen', 'Temperatur-Test')
        st['exc_active'] = False
        self.set_fan_target(self.base, 'Temperatur-Test: Basisstufe %d' % self.base)
        learned = (st.get('learned') or {}).get(str(self.base))
        clock = int(round(learned['mhz'])) if learned else START_CLOCK.get(self.base, START_CLOCK[1])
        why = 'Temperatur-Test Beginn: %s Stufe %d (%d MHz)' % ('Lernwert' if learned else 'Initialwert', self.base, clock)
        st['test'] = {'started': started.isoformat(), 'step': 0, 'steps': TEST_STEPS, 'clock': clock,
                      'phase': 'einstellen', 'wait_ends': None, 'measure_from': None, 'measure_ends': None}
        self.queue_clock(clock, why)
        self.note(why + ', dann warten bis ≤ %d °C.' % TEST_START_AT)

    def test_rules(self):
        """Ablauf des Temperatur-Tests (nach der Übernahme-Prüfung der Runde): einstellen → (Beginn: warten bis ≤ 80 °C)
        bzw. 2 min warten → 20 min messen → nächster Schritt; das Ende nach dem letzten Schritt prüft temp_test."""
        st, now = self.st, self.now
        tt = st['test']
        if tt['phase'] == 'einstellen':
            if self.busy():
                return                                   # Modus/Takt noch nicht übernommen
            if tt['step'] == 0:
                tt['phase'] = 'beginn'
            else:
                tt.update({'phase': 'warten', 'wait_ends': _iso(now + timedelta(seconds=TEST_WAIT_S))})
                self.note('Temperatur-Test Schritt %d/%d: %d MHz übernommen, 2 min warten.' % (tt['step'], TEST_STEPS, tt['clock']))
        if tt['phase'] == 'beginn' and self.t <= TEST_START_AT:
            self.test_step(1)
        elif tt['phase'] == 'warten' and _age(tt['wait_ends'], now) >= 0:
            ends = now + timedelta(seconds=TEST_MEASURE_S)
            tt.update({'phase': 'messen', 'measure_from': _iso(now), 'measure_ends': _iso(ends)})
            text = 'Temperatur-Test Schritt %d/%d: Messung läuft bis %s' % (tt['step'], TEST_STEPS,
                                                                            ends.astimezone(LOCAL_TZ).strftime('%H:%M'))
            self.base_event(kind='test_messung', trigger=text,
                            details={'testschritt': tt['step'], 'takt': tt['clock'], 'von': _iso(now), 'bis': _iso(ends)})
            self.note(text + '.')
        elif tt['phase'] == 'messen' and tt['step'] < TEST_STEPS and _age(tt['measure_ends'], now) >= 0:
            self.test_step(tt['step'] + 1)

    def test_step(self, step):
        """Nächster Schritt: Takt +30 MHz; Modus nach der allgemeinen Regel (erst Modus, dann Takt)."""
        tt = self.st['test']
        old = tt['clock']
        clock = min(old + TEST_STEP_MHZ, CLOCK_MAX)
        why = 'Temperatur-Test Schritt %d/%d: Takt %d MHz (+%d MHz)' % (step, TEST_STEPS, clock, clock - old)
        tt.update({'step': step, 'clock': clock, 'phase': 'einstellen', 'wait_ends': None, 'measure_from': None,
                   'measure_ends': None})
        self.base_event(kind='test_schritt', trigger=why, clock_old=old, clock_new=clock,
                        details={'testschritt': step, 'takt': clock})
        self.queue_clock(clock, why)
        self.note(why + '.')

    # --- main
    def run(self):
        st, row, now = self.st, self.row, self.now
        st.pop('test_clear', None)
        if not row['enabled']:
            if not st.get('off'):
                self.note('Automatik aus: Takt bleibt %s.' % ('%s MHz' % st['clock_set'] if st.get('clock_set') else 'wie er ist'))
                self.drop_pending('Automatik aus')
            st.update({'off': True, 'phase': 'aus', 'queue': [], 'settle': None})
            self.j.cycle(recorded_at=now, phase='aus', base_level=self.base, clock_set=st.get('clock_set'),
                         fan_level=st.get('fan_set'), decision='Automatik aus')
            return st
        if st.pop('off', False) or st.get('v') != STATE_VERSION:
            # Automatik ein bzw. Umstellung auf die neue Regelung (startet ohne Lernwerte, Plan Kapitel 7)
            keep = ('log', 'stock', 'fan_set', 'fan_read', 'fan_read_at', 'fan_set_at', 'elapsed', 'mode_key',
                    'maint', 'maint_ended_at', 'offline', 'over_hot', 'test', 'restart_req_seen')
            new = {k: st[k] for k in keep if k in st}
            new['learned'] = (st.get('learned') or {}) if st.get('v') == STATE_VERSION else {}
            if new.get('stock') is None:
                new['stock'] = {}
            new.update({'v': STATE_VERSION, 'level_seen': str(row.get('level_at')), 'start_reason': 'Automatik ein',
                        'fan_target': st.get('fan_set') or self.base})
            st = self.st = new
            self.note('Automatik ein (Basisstufe %d).' % self.base)

        req = row.get('restart_req_at')
        if req is not None and str(req) != st.get('restart_req_seen'):
            st['restart_req_seen'] = str(req)
            req = req if isinstance(req, datetime) else datetime.fromisoformat(str(req))
            if (now - req).total_seconds() < RESTART_EXPECT_S:
                st['restart_req'] = _iso(req)                # Knopf „A15 neu starten“: Neustart erwartet
        if st.get('restart_req') and _age(st['restart_req'], now) >= RESTART_EXPECT_S:
            st['restart_req'] = None

        if row.get('maintenance_at'):
            m = _maintenance(row, st, now, self.ac, self.j)
            if m is None:
                st['phase'] = 'wartung'
                self.j.cycle(recorded_at=now, phase='wartung', base_level=self.base, clock_set=st.get('clock_set'),
                             fan_level=0 if (st.get('maint') or {}).get('fan_off') else st.get('fan_set'),
                             decision='Wartung: Miner aus')
                return st
            if st.get('maint_end'):
                st['start_reason'] = 'nach Wartung'
                st['start_cold'] = True
        else:
            st.pop('maint', None)
            try:
                m = calibration.read_miner(row['ip_address'], row['port'])
            except (OSError, RuntimeError, ValueError) as exc:
                err = str(exc)[:500]
                if not st.get('offline') and self.restart_expected():
                    st['offline_since'] = _iso(now)
                    st['offline'] = 'neustart'
                    log_id = self.base_event(kind='neustart_offline', trigger='A15 startet neu – nicht erreichbar (erwartet)',
                                             ok=None, details={'meldung': err})
                    self.note('A15 startet neu: nicht erreichbar (erwartet).')
                    self.remember_net(log_id, 'neustart_offline')
                elif not st.get('offline') or (st['offline'] == 'neustart' and not self.restart_expected()):
                    # nicht erwartet – oder länger als erwartet (Knopf 5 min, Startpause vorbei): wie bisher ein Fehler
                    st['offline_since'] = st.get('offline_since') or _iso(now)
                    st['offline'] = True
                    log_id = self.base_event(kind='miner_offline', trigger='Miner antwortet nicht', ok=False, error=err)
                    self.note('Miner antwortet nicht: Lüfter unverändert, warten.')
                    self.remember_net(log_id, 'miner_offline', err=err)
                    st['offline_entry'] = [log_id, err, _iso(now)]
                self.j.cycle(recorded_at=now, phase=st.get('phase') or '', base_level=self.base,
                             clock_set=st.get('clock_set'), fan_level=st.get('fan_set'), fan_read=st.get('fan_read'),
                             decision='A15 startet neu' if st['offline'] == 'neustart' else 'Miner antwortet nicht')
                return st
        self.m = m
        if m.get('web_error') and m.get('mode_key') is None and st.get('mode_key'):
            m['mode_key'] = st['mode_key']                 # Einstellungsseite seit dem Start nie gelesen: letzter Modus
        self.web_status(m)
        off = st.pop('offline', False)
        if off:
            took = int(_age(st['offline_since'], now)) if st.get('offline_since') else None
            if off == 'neustart':
                log_id = self.base_event(kind='neustart_online', trigger='A15 nach Neustart wieder erreichbar'
                                         + (' nach %d s' % took if took is not None else ''), details={'nicht_erreichbar_s': took})
            else:
                log_id = self.base_event(kind='miner_online', trigger='Miner antwortet wieder', details={'nicht_erreichbar_s': took})
            self.remember_net(log_id, 'neustart_online' if off == 'neustart' else 'miner_online', took,
                              partner=st.get('offline_entry'))
        st.pop('offline_since', None)
        st.pop('offline_entry', None)
        st['seen_at'] = _iso(now)
        st['ac_error'] = None

        # Neustart (nicht durch den eigenen Moduswechsel), Stufenwahl, Takt-Messung, Over Hot
        own_switch = (st.get('pending') or {}).get('kind') == 'modus' or _age(st.get('mode_sent_at'), now) < OWN_SWITCH_S
        if (st.get('elapsed') is not None and m['elapsed'] is not None and m['elapsed'] < st['elapsed']
                and not own_switch):
            self.base_event(kind='neustart', trigger='Laufzeit des Miners zurückgesetzt (%s s → %s s)' % (st['elapsed'], m['elapsed']))
            st['start_reason'] = 'Neustart des Miners'
            st['start_cold'] = True
            self.restarted = True
            since = st.get('restart_req')                 # Knopf: nur Einträge ab dem Klick
            st['restart_req'] = None
            self.relabel_restart(since)
        st['elapsed'] = m['elapsed']
        level_at = row.get('level_at')
        if level_at is not None and str(level_at) != st.get('level_seen'):
            st['level_seen'] = str(level_at)
            st['start_reason'] = 'Basisstufe gewählt (Stufe %d)' % self.base
            self.level_changed = True
        cal = bool(row['cal_active'])
        if cal and not st.get('cal'):
            self.base_event(kind='messung_start', trigger='Takt-Messung läuft: Taktregelung pausiert')
            self.note('Takt-Messung: Taktregelung pausiert, Lüfter nur nach „Hohe Chiptemperaturen“.')
            self.drop_pending('Takt-Messung')
            st['queue'] = []
            self.end_learning('Takt-Messung')
        if not cal and st.get('cal'):
            self.base_event(kind='messung_ende', trigger='Takt-Messung beendet')
            st['start_reason'] = 'nach Takt-Messung'
            st.pop('start_cold', None)                   # Moduswechsel der Messung setzen die Laufzeit zurück: Chips warm
        st['cal'] = cal

        # readings for the page and the protocol
        st['mode_key'] = m['mode_key']
        st['points'] = list(m['points'] or [])
        st['clock_now'] = round(sum(m['points']) / len(m['points']), 1) if m['points'] else None
        stock = self.stock(m['mode_key'])
        st['stock_mhz'] = round(sum(stock) / len(stock), 1) if stock else (
            MODE_TOP.get(level_of(m['mode_key'])) if level_of(m['mode_key']) is not None else None)
        boards = [calibration._num(x) for x in str(m['fields'].get('MTmax') or '').split()]
        st['boards'] = [b for b in boards if b is not None]
        tmax = calibration._num(m['fields'].get('TMax'))
        over_hot = 'Over Hot' in (m['status'] or '')
        valid = tmax is not None and -50 < tmax < 200 and not over_hot
        if over_hot and not st.get('over_hot'):
            st['over_hot'] = True
            self.base_event(kind='over_hot', trigger='Miner meldet Over Hot: keine Entscheidung bis zu gültigen Werten')
            self.note('Over Hot gemeldet: keine Entscheidung bis zu gültigen Werten.')
        if not over_hot and st.get('over_hot'):
            st.pop('over_hot')
            st['start_reason'] = 'nach Over Hot'
            st['start_cold'] = True

        if valid:
            self.t = int(round(tmax))
        self.temp_test(valid, over_hot, cal)
        if valid:
            temps = [x for x in st.get('temps') or [] if _age(x[0], now) <= TEMP_KEEP_S]
            st['temps'] = temps
            st['t'] = self.t
            st['tendency'], self.ref = self.tendency()
            temps.append([_iso(now), self.t])
            # Aufenthalt in 79-84 °C seit dem letzten Eintritt (von unten oder oben); beim ersten Wert über 84 °C gemessen
            if self.t > T_HIGH:
                if not st.get('above'):
                    st['band_dwell_s'] = _age(st['band_in'], now) if st.get('band_in') else 0
                    st['above'] = True
                st['band_in'] = None
            else:
                st['above'] = False
                st['band_in'] = (st.get('band_in') or _iso(now)) if self.t >= CALM_LOW else None
            self.update_thermal()
        if st.get('fan_target') is None:
            st['fan_target'] = st.get('fan_set') or self.base

        # Start (Kapitel 4): braucht keine Temperatur, nur gemeldete Takte; nicht während Takt-Messung und Over Hot
        if st.get('start_reason') and not cal and not over_hot and m['points']:
            self.start(st['start_reason'])
        elif st.get('start_reason') and cal:
            self.set_fan_target(self.base, 'Takt-Messung: Basisstufe')

        if not cal and not over_hot:
            self.confirm_pending()
            if st.get('mode_wait') is not None and m.get('conf_fresh', True) and not self.busy():
                # Einstellungsseite wieder da: der gewartete Moduswechsel – passend zum jetzt gesetzten Takt
                st['mode_wait'] = None
                current = level_of(m['mode_key'])
                if st.get('clock_set') and current is not None:
                    level = required_level(int(round(st['clock_set'])), current)
                    if level != current:
                        st['queue'] = [{'kind': 'modus', 'value': level, 'why': 'Einstellungsseite wieder da: Modus'},
                                       {'kind': 'takt', 'value': int(round(st['clock_set'])),
                                        'why': 'Einstellungsseite wieder da: Takt im neuen Modus'}]
            # Miner hat den Takt selbst geändert (Takt laut Miner > gesetzt + 25 MHz, frühestens 60 s nach der letzten
            # Taktänderung – vorher stellt die Firmware den neuen Takt noch schrittweise ein): erneut senden
            # nach dem erneuten Senden ebenfalls 60 s warten (25./26.10.: nach dem Hochfahren 3× in 10 s gesendet,
            # während die Firmware von 360 MHz schon schrittweise heruntersetzte)
            if (not self.busy() and st.get('clock_set') and st['clock_now']
                    and st['clock_now'] > st['clock_set'] + OVERRIDE_MHZ
                    and _age(st.get('clock_changed_at'), now) >= OVERRIDE_AFTER_S
                    and _age(st.get('resent_at'), now) >= OVERRIDE_AFTER_S):
                self.base_event(kind='firmware_takt', trigger='Takt laut Miner %.0f MHz, gesetzt %s MHz'
                                % (st['clock_now'], st['clock_set']))
                self.note('Miner hat den Takt selbst geändert (%.0f MHz laut Miner): %s MHz erneut.'
                          % (st['clock_now'], st['clock_set']))
                current = level_of(m['mode_key'])
                level = required_level(st['clock_set'], current)
                q = []
                if level != current:
                    q.append({'kind': 'modus', 'value': level, 'why': 'Takt erneut: Modus'})
                q.append({'kind': 'takt', 'value': int(round(st['clock_set'])),
                          'why': 'Miner hat den Takt geändert: erneut senden'})
                st['queue'] = q
                st['resent_at'] = _iso(now)
        if valid:
            self.rules()
        self.write_fan()                                   # Lüfter vor den (möglicherweise langsamen) Miner-Befehlen
        if not cal and not over_hot:
            self.resend_or_send()
            if valid and not st.get('test'):
                self.learn()
        self.read_fan()
        st['phase'] = self.phase()
        st['window_active'] = bool(st.get('window_until')) and _age(st['window_until'], now) < 0
        p = st.get('pending')
        self.j.cycle(recorded_at=now, phase=st['phase'], chip_temp=self.t, boards=st.get('boards'),
                     tendency=st.get('tendency') if valid else None, clock_set=st.get('clock_set'),
                     clock_reported=st.get('clock_now'), points=st.get('points'), mode_key=m['mode_key'] or '',
                     base_level=self.base, fan_level=st.get('fan_set'), fan_read=st.get('fan_read'),
                     power_w=m.get('power'), ghsspd_ths=(m['ghsspd'] / 1000.0) if m.get('ghsspd') else None,
                     dh_pct=m.get('dh'), status=(m['status'] or '')[:40], window_active=st['window_active'],
                     learn_since=st.get('learn_since'),
                     pending=('%s %s' % (p['kind'], p['value'])) if p else '',
                     decision=' · '.join(self.decisions)[:200] if self.decisions else ('Over Hot' if over_hot else
                                                                                         ('' if valid else 'Chiptemperatur ungültig')),
                     log=self.cycle_log)
        return st


def step(row, now, ac, journal=None):
    """One round for one automation row; returns the new state."""
    return Round(row, now, ac, journal or automation_journal.MemoryJournal()).run()


def run_fan_automation(database_url, ac=None):
    if (os.environ.get('MINERSENTINEL_ENV') or 'Live').lower() == 'test':
        return
    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.Error as exc:
        logger.warning('Temperatur-Automatik: no database: %s', exc)
        return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT f.id, f.enabled, f.ac_dev_id, f.ac_port, f.level, f.level_at, f.maintenance_at, f.temp_test_at, f.state, "
                "d.id AS device_id, d.ip_address, d.port, "
                "(SELECT max(l.recorded_at) FROM automation_log l WHERE l.device_id = d.id AND l.kind = 'neustart_nutzer') "
                "AS restart_req_at, "
                "d.connection_config, EXISTS (SELECT 1 FROM calibration_runs r WHERE r.device_id = d.id "
                "AND r.status IN ('running', 'cancelling', 'finishing')) AS cal_active "
                "FROM fan_automation f JOIN devices d ON d.id = f.device_id "
                "WHERE f.enabled OR NOT COALESCE((f.state->>'off')::boolean, false)")
            rows = cur.fetchall()
        jconn = automation_journal.connect(database_url) if rows else None
        for row in rows:
            journal = automation_journal.Journal(jconn, row['device_id'])
            try:
                state = step(row, _now(), ac or load_ac(), journal)
                state.pop('last_error', None)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                logger.exception('Temperatur-Automatik: Fehler')
                state = dict(row['state'] or {})
                state['last_error'] = '%s: %s' % (_now().strftime('%H:%M:%S UTC'), str(exc)[:200])
                journal.event(kind='fehler', trigger='Fehler in der Regelung', ok=False, error=str(exc)[:2000])
            with conn.cursor() as cur:
                cur.execute("UPDATE fan_automation SET state = %s WHERE id = %s", (json.dumps(state), row['id']))
                if state.pop('test_clear', False):
                    # Temperatur-Test fertig oder abgebrochen: nur den Test löschen, der gelaufen ist
                    cur.execute("UPDATE fan_automation SET temp_test_at = NULL, state = %s WHERE id = %s "
                                "AND temp_test_at = %s", (json.dumps(state), row['id'], row['temp_test_at']))
                if state.pop('maint_end', False):
                    # the A15 is back on: end the maintenance (only the one that was announced)
                    cur.execute("UPDATE fan_automation SET maintenance_at = NULL, state = %s WHERE id = %s "
                                "AND maintenance_at = %s", (json.dumps(state), row['id'], row['maintenance_at']))
            conn.commit()
    finally:
        conn.close()
