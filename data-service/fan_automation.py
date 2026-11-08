"""Temperatur-Automatik (A15 + AC Infinity fan), right after every central reading (live_reader.py, 5 s).

Rules v2.2.7 (Nutzer 06.11.; they replace all fan and clock rules before – Korrekturphase, Ausnahme, Lüfterstöße,
Taktsperre, Obergrenzen, Ertrags-Prüfung, Temperatur-Test and Takt-Messung are gone):
  * The user chooses the fan level = Basisstufe and the Zieltemperatur ZTU–ZTO (target_low/target_high: ZTU from 77,
    ZTO up to 87 °C, 4 to 5 Grad; checked by the backend). t = hottest chip (TMax). No clock change in ZTU..ZTO.
  * Fan – exactly one mode at a time, every round with a valid temperature:
      Kühlmodus (checked first): t > ZTO+2 → fan = Basisstufe +1, t > ZTO+7 → +2 (at most level 10; the plus never
        goes down while it runs); ends at t < ZTO−3 (fan = Basisstufe); in between the level stays.
      Heizmodus: t < ZTU−10 and fan target unchanged for more than 1 min → fan −1 (at least level 1), every further
        minute again; ZTU−10..ZTU−5 the level stays; ends at t > ZTU−5 (fan = Basisstufe).
      Basismodus: otherwise fan = Basisstufe.
    After the end of the Heiz- or Kühlmodus 3 min no clock increase.
  * Clock increase below ZTU (d = ZTU − t), at the first such value: d 1–3 +5 MHz (then 3 min no increase), 4–5 +10
    (3 min), 6–10 +15 (2 min), 11–20 +20 (2 min), from 21 +40 (2 min). Not while a lock runs, during the Startpause,
    while a command is pending or queued, at 440 MHz or when more needs the waiting mode switch. The locks after an
    increase, after the Heizmodus and after the Kühlmodus end early at t > ZTU+3; the lock after a reduction (5 min)
    never does. At Basisstufe 1, for 30 min after a reduction by −5 MHz no increase at ZTU−2..ZTU−1 (a start ends
    this; Nutzer 09.11.).
  * Clock reduction above ZTO, Bereich 1 = ZTO+1..+3, 2 = +4..+6, 3 = +7..+9, 4 = from +10: Bereich 1 −15/−10/−5 MHz
    by the ZeitImZieltemperatur (time in ZTU..ZTO before t first exceeded ZTO: < 60 s / 60–300 s / > 300 s), 2 and 3
    −20, 4 −50. At the start of an excursion at once (3 min after the last reduction if that was less than 3 min ago),
    same Bereich every 2 min, higher Bereich at once, lower Bereich 2 min after arriving there. A due reduction
    replaces a running command.
  * Start (Basisstufe gewählt, Automatik ein, Neustart des Miners, nach Wartung, nach Over Hot): start clock = learned
    value of the Basisstufe, else the one of the same level from the remembered Zieltemperatur with the nearest middle
    (equally far: the cooler one), else the initial value (level 1 = 170, 2 = 250, 3 = 330, 4 = 380, 5 = 400,
    6 = 420, from 7 = 440 MHz). Heiz- and Kühlmodus end without a lock, fan = Basisstufe.
  * Startpause after a start with cold chips (Neustart, Wartung, Over Hot): no clock increase until ZTU−5, at most
    10 min. „Nicht erreichbar“ around a restart is a restart entry, not an error.
  * Clock 100–440 MHz, the mode follows the clock (25 MHz margin downwards, Nutzer 08.11.; was 20).
  * Takt laut Miner weicht ab: more than 15 MHz off the set clock for 15 min without a break → set clock sent again.
  * Fehlerrate (DH): over 10 % for 30 min without a break → automatic restart, at most one per 6 h.
  * Learning: 15 min in ZTU..ZTO at an unchanged fan level → learned value of the active level, only at a mean error
    rate below 5 % (level 1: below 7 %, Nutzer 09.11.); learned values are remembered per Zieltemperatur. Its clock is
    the set clock at the end of the 15 min (Nutzer 08.11.; was the mean), beside it the mean clock reported by the
    miner and the work mode (Nutzer 09.11.). The user can delete a single learned value (Nutzer 08.11.), also with
    the Automatik off.
  * New Zieltemperatur: new limits at once, no start.
  * Every round and every decision goes into the protocol (automation_journal.py).
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

STATE_VERSION = 2             # never change: a new version starts without learned values (Nutzer 06.11.)
RUN_S = 5                     # one round per central reading
# --- Zieltemperatur ZTU–ZTO (°C) comes from fan_automation.target_low/target_high
ZB_START = (82, 85)           # Zielbereich bei Einführung (Startwert des Nutzers, 27.10.): die Lernwerte von davor gehören dazu
# --- fan modes (Nutzer 06.11.)
COOL_FROM, COOL_PLUS2_FROM = 2, 7    # Kühlmodus: t > ZTO+2 → Basisstufe +1, t > ZTO+7 → +2
COOL_END = 3                  # ... ends at t < ZTO−3
HEAT_FROM = 10                # Heizmodus: a step at t < ZTU−10 ...
HEAT_STEP_S = 60              # ... once the fan target was not changed for more than 1 min (−1, at least level 1)
HEAT_END = 5                  # ... ends at t > ZTU−5
MODE_END_LOCK_S = 3 * 60      # after the end of the Heiz- or Kühlmodus: 3 min no clock increase
# --- clock increase below ZTU: (from ZTU − t, + MHz, lock after it in s), the largest first (Nutzer 06.11.)
PLUS_TABLE = ((21, 40, 120), (11, 20, 120), (6, 15, 120), (4, 10, 180), (1, 5, 180))
LOCK_KEYS = ('erhoehung', 'heizmodus', 'kuehlmodus', 'senkung')
LOCK_EARLY_END = 3            # locks erhoehung/heizmodus/kuehlmodus end at t > ZTU+3; senkung never early
# --- clock reduction above ZTO (Nutzer 06.11.)
BAND_STEP = 3                 # Bereich 1 = ZTO+1..+3, 2 = +4..+6, 3 = +7..+9, 4 = from ZTO+10
CUT_MINUS = {2: 20, 3: 20, 4: 50}
ZT_LONG_S = 300               # Bereich 1 by the ZeitImZieltemperatur: 0–300 s −10, > 300 s −5 (Nutzer 08.11.)
CUT_EVERY_S = 120             # same Bereich: every 2 min; lower Bereich: 2 min after arriving there
CUT_GAP_S = 180               # excursion start less than 3 min after the last reduction: 3 min after that one
CUT_LOCK_S = 5 * 60           # after every reduction 5 min no clock increase (never ends early)
CUT5_MHZ = 5                  # Basisstufe 1: after a reduction by exactly −5 MHz (Bereich 1, over 5 min in ZT)
CUT5_BLOCK_S = 30 * 60        # ... 30 min no clock increase ...
CUT5_BLOCK_D = 2              # ... at ZTU−2 and ZTU−1 (from ZTU−3 as before); a start ends it (Nutzer 09.11.)
CUT5_BLOCK_BASE = 1
START_CLOCK = {1: 170, 2: 250, 3: 330, 4: 380, 5: 400, 6: 420, 7: 440, 8: 440, 9: 440, 10: 440}   # Nutzer 28.10.
CLOCK_MIN, CLOCK_MAX = 100, 440
MODE_TOP = {4: 360, 3: 380, 2: 400, 1: 420, 0: 440}   # oberer Takt je Modus (mode 04 ... mode 00)
MODE_DOWN_MARGIN = 25         # Spielraum nach unten: Wechsel in den tieferen Modus erst unter dessen Obergrenze − 25 MHz
                              # (Nutzer 08.11.; vorher 20), „nur kleiner“: 335 bleibt mode 03, 334 → mode 04
TENDENCY_S = 30               # Tendenz (only shown, no rule uses it): gegen den Wert von vor höchstens 30 s
START_HOLD_S = 10 * 60        # Startpause: nach einem Start mit kalten Chips keine Takterhöhung bis ZTU−5, höchstens 10 min
START_HOLD_BELOW = 5          # ... ZTU−5 (Nutzer 06.11.; vorher bis ZTU)
RESTART_EXPECT_S = 5 * 60     # Neustart über den Knopf: so lange gilt „nicht erreichbar“ als erwartet (Nutzer 25.10.)
RESTART_RELABEL_S = 3 * 60    # Neustart auf anderem Weg: „antwortet nicht/wieder“ der 3 min davor gehören zum Neustart
WEB_DOWN_LOG_S = 2 * 60       # Einstellungsseite antwortet nicht (Mining-Schnittstelle ok): Eintrag erst nach 2 min
# Fehlerrate DH (Nutzer 06.11.: only the automatic restart, no cap any more)
DH_RESTART_PCT = 10           # over 10 % ...
DH_RESTART_S = 30 * 60        # ... for 30 min without a break → automatic restart,
AUTO_RESTART_GAP_S = 6 * 3600     # ... at most one per 6 h (only the restarts of this rule count),
AUTO_RESTART_RETRY_S = 5 * 60     # ... after a failed send the next try after 5 min
DH_LEARN_PCT = 5              # learned value only at a mean error rate of the 15 min below 5 % (Nutzer 06.11.; was 4 %)
DH_LEARN_PCT_STUFE1 = 7       # ... at Lüfterstufe 1 below 7 % (Nutzer 09.11.)
LEARN_S = 15 * 60             # Lernperiode
LEARN_OUT_S = 60              # Ausflug aus ZTU..ZTO bis 60 s bricht die Lernperiode nicht ab (Nutzer 25.10.)
FAN_MIN, FAN_MAX = 1, 10
# --- bestehende Technik (unverändert übernommen)
RESEND_S = 20                 # Modus-/Taktbefehl ohne Übernahme: erneut senden
FW_DEV_MHZ, FW_DEV_S = 15, 15 * 60   # Takt laut Miner more than 15 MHz off for 15 min: send again (Nutzer 06.11.;
                                     # was 25 MHz above the set clock, 60 s after a change)
FAN_READ_S = 60
FAN_CONFIRM_S = 10            # the controller reports a new level only after > 5 s: read back 10 s after a write
FAN_WRITE_GAP_S = 20
TEMP_KEEP_S = 60
OWN_SWITCH_S = 120            # so lange nach einem eigenen Modusbefehl gilt ein Neustart als Folge davon
LOCAL_TZ = ZoneInfo('Europe/Berlin')  # Uhrzeiten in Protokolltexten
STOCK_PLAUSIBLE_MHZ = 15      # gemerkter Standardtakt nur, wenn er nahe am Standardtakt des Modus (Tabelle) liegt
THERMAL_EVERY_S, THERMAL_N = 30, 10   # thermischer Verlauf je Befehl: alle 30 s, 10 Werte
LOG_KEEP = 40
# state keys of the removed rules (v2.2.7, Nutzer 06.11.): dropped once, STATE_VERSION stays (keeps the learned values)
OBSOLETE_KEYS = ('caps', 'caps_cleared_seen', 'ertrag', 'ths', 'stoss', 'stoss_unten_at', 'stoss_unten_frei', 'stoss_at',
                 'stoss_frei', 'corr', 'corr_dwell_s', 'corr_wait', 'hot_wait', 'exc_active', 'calm', 'calm_for',
                 'window_until', 'window_active', 'below_since', 'last_plus_at', 'above', 'above_at', 'band_in',
                 'band_dwell_s', 'band_since', 'cold_since', 'down_since', 'cal', 'test', 'test_clear', 'dh_cap_since',
                 'dh_cap_clock', 'dh_restart_since', 'dh_block', 'start_at', 'resent_at')

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


def _hm(dt):
    """Local time HH:MM for protocol texts."""
    return dt.astimezone(LOCAL_TZ).strftime('%H:%M')


def _rel(n):
    """Fan level relative to the Basisstufe: '+1', '−2', '' (same level)."""
    return '' if not n else ('+%d' % n if n > 0 else '−%d' % -n)


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
    """Modus für einen Takt: aufwärts ab Obergrenze + 1, abwärts erst unter Obergrenze des tieferen Modus − 25 MHz
    (mode 00→01 unter 395, 01→02 unter 375, 02→03 unter 355, 03→04 unter 335; Nutzer 08.11.)."""
    if current is None:
        return table_level(mhz)
    level = current
    while level > 0 and mhz >= MODE_TOP[level] + 1:
        level -= 1
    while level < 4 and mhz < MODE_TOP[level + 1] - MODE_DOWN_MARGIN:
        level += 1
    return level


def points_for(stock, mhz):
    """The four clock points for an average clock, scaled from the stock points of the mode."""
    factor = mhz / (sum(stock) / len(stock))
    return [min(max(int(round(p * factor)), int(p * 0.15)), p) for p in stock]


def plus_rule(d):
    """Clock increase for t = ZTU − d (d ≥ 1) as (+ MHz, lock after it in s); None at or above ZTU (Nutzer 06.11.)."""
    for start, plus, lock_s in PLUS_TABLE:
        if d >= start:
            return plus, lock_s
    return None


def band_of(t, hi):
    """Bereich above ZTO = hi: 1 = ZTO+1..+3, 2 = +4..+6, 3 = +7..+9, 4 = from ZTO+10; None at or below ZTO."""
    d = t - hi
    return min((d - 1) // BAND_STEP + 1, 4) if d >= 1 else None


def minus_band_1(dwell):
    """Reduction in Bereich 1 by the ZeitImZieltemperatur in s: 0–300 s −10, > 300 s −5 MHz (Nutzer 08.11.; bis
    v2.2.7 unter 60 s −15)."""
    return 10 if dwell <= ZT_LONG_S else 5


def _dur(seconds):
    """ZeitImZieltemperatur for texts: '45 s', '3 min' (whole minutes)."""
    return '%d s' % seconds if seconds < 60 else '%d min' % (seconds // 60)


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

    State of the rules (Nutzer 06.11.): heat (Heizmodus, {'since'}), cool (Kühlmodus, {'since', 'plus'}),
    fan_changed_at (last change of the fan target), locks (until times of the clock-increase locks erhoehung,
    heizmodus, kuehlmodus, senkung), hot (excursion above ZTO: Bereich, next reduction, ZeitImZieltemperatur),
    cut_at/cut_band (last reduction and its Bereich), cut5_at (last reduction by −5 MHz, cleared by every start;
    Nutzer 09.11.), zt_since (entry into ZTU..ZTO), start_hold (Startpause),
    start_reason (a start is due), fw_dev (Takt laut Miner weicht ab seit), dh_since (Fehlerrate über 10 % seit). The shown phase is derived from
    heat/cool at the end of the round."""

    def __init__(self, row, now, ac, journal):
        self.row, self.now, self.ac, self.j = row, now, ac, journal
        self.st = dict(row['state'] or {})
        self.base = int(row['level'])
        self.lo, self.hi = int(row['target_low']), int(row['target_high'])   # Zieltemperatur ZTU–ZTO
        self.decisions = []
        self.cycle_log = None
        self.m = None
        self.t = None
        self.ref = None
        self.zt_dwell = None                             # ZeitImZieltemperatur (s) when t exceeds ZTO in this round

    # --- helpers
    def note(self, text):
        _note(self.st, self.now, text)

    def decide(self, text, log_id=None):
        self.decisions.append(text)
        if log_id and not self.cycle_log:
            self.cycle_log = log_id

    def phase(self):
        """Fan mode: 'kuehlmodus' | 'heizmodus' | 'basismodus' (Nutzer 06.11.)."""
        st = self.st
        if st.get('cool'):
            return 'kuehlmodus'
        if st.get('heat'):
            return 'heizmodus'
        return 'basismodus'

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
        Taktänderung weniger als 30 s zurück, gegenüber dem Wert bei dieser Änderung. Only shown (Nutzer 06.11.)."""
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

    # --- state of the removed rules (v2.2.7)
    def drop_old_rules(self):
        """Once after the update (Nutzer 06.11.): the state keys of the removed rules go, STATE_VERSION stays (so the
        learned values stay). Had an old rule moved the fan (Korrekturphase, Ausnahme, Lüfterstöße, Heizmodus of the
        start), the fan target goes back to the Basisstufe; the new rules decide from this round on. A fan below the
        Basisstufe is written even while the miner is silent (heat_fan_due, as heat_off did; Nutzer 26.10.)."""
        st = self.st
        moved = bool(st.get('corr') or st.get('exc_active') or st.get('stoss') or st.get('heat') is True)
        lowered = st.get('heat') is True or (st.get('stoss') or {}).get('dir') == 'down'
        for key in OBSOLETE_KEYS:
            st.pop(key, None)
        if st.get('heat') is not None and not isinstance(st['heat'], dict):
            st['heat'] = None                            # Heizmodus of the start until ZTU (bool, before v2.2.7)
        if moved:
            self.set_fan_target(self.base, 'Neue Regeln: Basisstufe %d' % self.base)
            self.note('Neue Regeln (Heiz-, Basis-, Kühlmodus): Lüfter Basisstufe %d.' % self.base)
        if lowered:
            st['heat_fan_due'] = True

    # --- start
    def start_clock(self):
        """Starttakt als (MHz, Quelle): Lernwert der Basisstufe in der Zieltemperatur; fehlt er, der derselben Stufe
        aus der gemerkten Zieltemperatur mit der nächsten Mitte (gleich weit: die kühlere); sonst der Initialwert.
        No cap any more (Nutzer 06.11.)."""
        st, key = self.st, str(self.base)
        learned, source = (st.get('learned') or {}).get(key), 'Lernwert'
        if not learned:
            mid, near = (self.lo + self.hi) / 2.0, []
            for zb, values in (st.get('learned_zb') or {}).items():
                try:
                    lo, hi = (int(x) for x in zb.split('-'))
                except ValueError:
                    continue
                if (values or {}).get(key):
                    near.append((abs((lo + hi) / 2.0 - mid), lo + hi, lo, hi, values[key]))
            if near:
                _, _, lo, hi, learned = min(near, key=lambda x: x[:3])
                source = 'Lernwert %d–%d' % (lo, hi)
        target = int(round(learned['mhz'])) if learned else START_CLOCK.get(self.base, START_CLOCK[1])
        return target, (source if learned else 'Initialwert')

    def start(self, reason):
        """Start: start clock (mode first if needed), Heiz- and Kühlmodus end without a lock, fan = Basisstufe; the
        excursion above ZTO starts anew. Locks, cut_at and the ZeitImZieltemperatur stay (Nutzer 06.11.); the block
        after a reduction by −5 MHz (cut5_at) ends (Nutzer 09.11.)."""
        st = self.st
        target, source = self.start_clock()
        target = min(max(target, CLOCK_MIN), CLOCK_MAX)
        old_fan, old_clock = st.get('fan_target'), st.get('clock_set')
        self.drop_pending('Start (%s)' % reason)
        self.end_heat('Start (%s)' % reason, lock=False)
        self.end_cool('Start (%s)' % reason, lock=False)
        st.update({'clock_set': target, 'queue': [], 'clock_changed_at': _iso(self.now), 'clock_changed_t': self.t,
                   'start_reason': None, 'hot': None, 'cut5_at': None})
        self.set_fan_target(self.base, 'Start: Basisstufe %d' % self.base, start=True)
        self.end_learning(None)
        level = table_level(target)
        queue = []
        if level_of(self.m['mode_key']) != level:
            queue.append({'kind': 'modus', 'value': level, 'why': 'Start: Modus für %d MHz' % target})
        queue.append({'kind': 'takt', 'value': target, 'from': old_clock, 'why': 'Start: %s Stufe %d' % (source, self.base)})
        st['queue'] = queue
        log_id = self.base_event(kind='start', trigger=reason, fan_old=old_fan, fan_new=st['fan_target'],
                                 clock_old=old_clock, clock_new=target, mode_new=calibration.mode_key(2, level),
                                 details={'quelle': source, 'starttakt': target, 'basisstufe': self.base})
        self.note('%s: Starttakt %d MHz (%s Stufe %d), Lüfter Stufe %d.' % (reason, target, source, self.base,
                                                                        st['fan_target']))
        self.decide('Start (%s): %d MHz' % (reason, target), log_id)
        if st.pop('start_cold', False):
            # die Chips waren aus (Neustart, Wartung, Over Hot – auch wenn in derselben Runde ein anderer Startgrund
            # dazukam): der erste Messwert ist kalt (25.10.: 26 °C → +40 MHz → 85 °C)
            until = self.now + timedelta(seconds=START_HOLD_S)
            st['start_hold'] = {'since': _iso(self.now), 'until': _iso(until)}
            text = 'Startpause: keine Takterhöhung bis %d °C, spätestens %s' % (self.lo - START_HOLD_BELOW, _hm(until))
            self.base_event(kind='startpause', trigger=text, details={'bis': _iso(until), 'grund': reason})
            self.note(text + '.')

    # --- commands
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
                    # außerhalb der Folge, die Taktbefehle laufen weiter – auch weitere Senkungen über ZTO (Nutzer 26.10.)
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

    def queue_clock(self, value, why, hot=False, step=None, details=None):
        """Takt setzen: Grenzen 100–440 MHz, Modus nach Regel; eine Senkung (hot) zuerst den Takt, dann den Modus, dann
        den Takt erneut, sonst erst den Modus. Ein laufender Befehl wird abgelöst."""
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
                  'details': dict({'schritt': step} if step is not None else {}, **(details or {}))}
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
        return True

    # --- fan (Lüftersteuerung)
    def set_fan_target(self, level, why, start=False):
        """New fan target. Every change of it (own steps, start, Kühlmodus, Heizmodus …) sets fan_changed_at – a start
        also at the same level; the Heizmodus steps only after more than 1 min without a change (Nutzer 06.11.)."""
        level = min(max(int(level), FAN_MIN), FAN_MAX)
        st = self.st
        if level != st.get('fan_target'):
            fp = st.get('fan_pending')
            if fp and fp.get('log_id'):
                self.j.update(fp['log_id'], takeover_state='nicht gemessen')
            st['fan_pending'] = None
            st['fan_target'] = level
            st['fan_why'] = why
            st['fan_changed_at'] = _iso(self.now)
        elif start:
            st['fan_changed_at'] = _iso(self.now)

    def read_after_write(self):
        st = self.st
        return _age(st.get('fan_read_at'), self.now) <= _age(st.get('fan_set_at'), self.now) - FAN_CONFIRM_S

    def confirm_fan(self):
        """Lüfterbefehl übernommen: die Lüftersteuerung meldet die Zielstufe, gelesen ab 10 s nach dem Befehl."""
        st, now = self.st, self.now
        fp = st.get('fan_pending')
        if fp and st.get('fan_read') == st.get('fan_target') and self.read_after_write():
            self.j.update(fp['log_id'], takeover_at=now, takeover_s=round(_age(fp['at'], now), 1),
                          takeover_state='gemessen')
            self.follow(fp['log_id'])
            st['fan_pending'] = None

    def write_fan(self):
        st, now, ac = self.st, self.now, self.ac
        fport = self.row['ac_port']
        target = st.get('fan_target')
        if target is None:
            return
        read_after_write = self.read_after_write()
        self.confirm_fan()
        fp = st.get('fan_pending')
        # eine neue Lüfterstufe sofort; erneut senden (Fehler oder andere Rückmeldung) frühestens FAN_WRITE_GAP_S nach
        # dem letzten Versuch
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

    # --- learning
    def end_learning(self, reason):
        st = self.st
        if st.get('learn_since') and reason:
            self.base_event(kind='lernen_ende', trigger=reason, details={'ergebnis': 'abgebrochen', 'grund': reason,
                                                                         'seit': st['learn_since']})
        st['learn_since'] = None
        st['learn_sum'], st['learn_n'] = 0, 0
        st['learn_mclk_sum'], st['learn_mclk_n'] = 0.0, 0
        st['learn_dh_sum'], st['learn_dh_n'] = 0.0, 0
        st['learn_ths_sum'], st['learn_ths_n'] = 0.0, 0
        st['learn_w_sum'], st['learn_w_n'] = 0.0, 0

    def learn(self):
        """Lernperiode 15 min auf derselben Lüfterstufe. Taktschritte setzen sie nicht zurück, kurze Ausflüge
        aus ZTU..ZTO bis 60 s auch nicht (Nutzer 25.10.: sonst entstand nie ein Lernwert). Neu beginnt sie bei Wechsel
        der Lüfterstufe, Moduswechsel, einem Wert über ZTO oder länger als 60 s außerhalb ZTU..ZTO.
        Gespeichert (Lernwert der aktiven Stufe): der gesetzte Takt am Ende der 15 min (Nutzer 08.11.; vorher sein
        Mittelwert), Mittelwert der Chiptemperatur und der Hashrate der 15 min, dazu die Effizienz (Ø Leistung /
        Ø Hashrate, J/TH) und der mittlere Takt laut Miner (mhz_miner, nur Runden ohne offenen Takt-/Modusbefehl;
        Nutzer 08.11.) und der Modus (mode, Nutzer 09.11.) – nur bei mittlerer Fehlerrate unter 5 % (Nutzer 06.11.;
        vorher 4 %), bei Lüfterstufe 1 unter 7 % (Nutzer 09.11.), sonst verworfen und neu begonnen. No cap limits it
        any more."""
        st, t, now = self.st, self.t, self.now
        lo, hi = self.lo, self.hi
        level = st.get('fan_target')
        mode_cmd = any(c.get('kind') == 'modus' for c in (st.get('queue') or [])) or \
            (st.get('pending') or {}).get('kind') == 'modus' or st.get('settle')
        reason = None
        if t > hi:
            reason = 'Chiptemperatur %d °C über %d °C' % (t, hi)
        elif level != st.get('fan_set'):
            reason = 'Lüfterstufe ändert sich'
        elif mode_cmd:
            reason = 'Moduswechsel'
        elif st.get('learn_since') and st.get('learn_level') != level:
            reason = 'Lüfterstufe geändert'
        if not (lo <= t <= hi) and not reason:
            st['learn_out_since'] = st.get('learn_out_since') or _iso(now)
            if _age(st['learn_out_since'], now) > LEARN_OUT_S:
                reason = 'Chiptemperatur länger als 60 s außerhalb %d–%d °C (%d °C)' % (lo, hi, t)
        elif lo <= t <= hi:
            st['learn_out_since'] = None
        if reason:
            self.end_learning(reason)
            st['learn_out_since'] = None
            return
        if not st.get('learn_since'):
            if not (lo <= t <= hi):
                return                                   # eine Lernperiode beginnt nur in der Zieltemperatur
            st.update({'learn_since': _iso(now), 'learn_level': level, 'learn_sum': 0, 'learn_n': 0,
                       'learn_mclk_sum': 0.0, 'learn_mclk_n': 0, 'learn_dh_sum': 0.0, 'learn_dh_n': 0,
                       'learn_ths_sum': 0.0, 'learn_ths_n': 0, 'learn_w_sum': 0.0, 'learn_w_n': 0,
                       'learn_out_since': None})
            self.base_event(kind='lernen_start', trigger='Chiptemperatur %d °C in %d–%d °C' % (t, lo, hi),
                            details={'takt': st.get('clock_set'), 'luefterstufe': level})
        st['learn_sum'] += t
        st['learn_n'] += 1
        if st.get('clock_now') is not None and not self.busy():
            # Takt laut Miner only while no clock/mode command waits for its takeover (Nutzer 08.11.)
            st['learn_mclk_sum'] = (st.get('learn_mclk_sum') or 0.0) + float(st['clock_now'])
            st['learn_mclk_n'] = (st.get('learn_mclk_n') or 0) + 1
        if self.m.get('dh') is not None:
            st['learn_dh_sum'] = (st.get('learn_dh_sum') or 0.0) + float(self.m['dh'])
            st['learn_dh_n'] = (st.get('learn_dh_n') or 0) + 1
        if self.m.get('ghsspd'):
            st['learn_ths_sum'] = (st.get('learn_ths_sum') or 0.0) + float(self.m['ghsspd']) / 1000.0
            st['learn_ths_n'] = (st.get('learn_ths_n') or 0) + 1
        if self.m.get('power'):
            st['learn_w_sum'] = (st.get('learn_w_sum') or 0.0) + float(self.m['power'])
            st['learn_w_n'] = (st.get('learn_w_n') or 0) + 1
        if _age(st['learn_since'], now) >= LEARN_S:
            n = max(st['learn_n'], 1)
            avg_t = round(st['learn_sum'] / n, 1)
            mhz = int(round(st.get('clock_set') or 0))   # the set clock at the end of the 15 min (Nutzer 08.11.)
            key = str(level)
            dh_n = st.get('learn_dh_n') or 0
            dh = st['learn_dh_sum'] / dh_n if dh_n else None
            limit = DH_LEARN_PCT_STUFE1 if level == 1 else DH_LEARN_PCT   # Lüfterstufe 1: 7 % (Nutzer 09.11.)
            if dh is None or dh >= limit:
                # Fehlerrate zu hoch (oder ohne Wert): kein Lernwert, die Periode beginnt neu
                why = ('Fehlerrate %.1f %% (Grenze %d %%)' % (dh, limit) if dh is not None
                       else 'Fehlerrate unbekannt (Grenze %d %%)' % limit)
                log_id = self.base_event(kind='lernen_ende', trigger='15 min erreicht, Lernwert verworfen: ' + why,
                                         details={'ergebnis': 'verworfen', 'grund': why, 'seit': st['learn_since']})
                self.note('Lernwert Stufe %s verworfen: %s.' % (key, why))
                self.decide('Lernwert verworfen', log_id)
            else:
                prev = (st.get('learned') or {}).get(key)
                entry = {'mhz': mhz, 'temp_avg': avg_t, 'at': _iso(now), 'count': (prev or {}).get('count', 0) + 1}
                mode = st.get('mode_key')
                if mode:                                      # Modus der Lernperiode, z. B. '2/L4' (Nutzer 09.11.)
                    entry['mode'] = mode
                if st.get('learn_mclk_n'):                    # Ø Takt laut Miner (Nutzer 08.11.)
                    entry['mhz_miner'] = round(st['learn_mclk_sum'] / st['learn_mclk_n'], 1)
                if st.get('learn_ths_n'):
                    entry['ths_avg'] = round(st['learn_ths_sum'] / st['learn_ths_n'], 1)   # Ø Hashrate (Nutzer 28.10.)
                    if st.get('learn_w_n'):                   # Effizienz J/TH (Nutzer 28.10.)
                        entry['j_th'] = round(st['learn_w_sum'] / st['learn_w_n'] * st['learn_ths_n']
                                              / st['learn_ths_sum'], 1)
                st.setdefault('learned', {})[key] = entry
                log_id = self.base_event(kind='lernwert', trigger='15 min auf Stufe %s, Ø %.1f °C' % (key, avg_t),
                                         details={'luefterstufe': level, 'takt': mhz,
                                                  'takt_miner': entry.get('mhz_miner'), 'modus': entry.get('mode'),
                                                  'temp_mittel': avg_t,
                                                  'anzahl': entry['count'], 'vorher': prev})
                self.base_event(kind='lernen_ende', trigger='15 min erreicht', details={'ergebnis': 'gespeichert'})
                self.note('Lernwert Stufe %s: %d MHz (gesetzter Takt), Ø %.1f °C (%d. Mal).' % (key, mhz, avg_t,
                                                                                    entry['count']))
                self.decide('Lernwert gespeichert', log_id)
            st.update({'learn_since': _iso(now), 'learn_sum': 0, 'learn_n': 0, 'learn_mclk_sum': 0.0,
                       'learn_mclk_n': 0, 'learn_dh_sum': 0.0, 'learn_dh_n': 0, 'learn_ths_sum': 0.0,
                       'learn_ths_n': 0, 'learn_w_sum': 0.0, 'learn_w_n': 0})

    # --- Lernwert löschen (Nutzer 08.11.)
    def learned_delete(self):
        """Requests of the user (automation_log kind 'lernwert_loeschen', newer than learned_del_seen; read with the
        row, also with the Automatik off): each deletes one learned value – of the current Zieltemperatur from learned,
        of another one from learned_zb (an archive entry left empty goes) – only if it still is the one the user saw
        (same 'at'). A running learning period goes on unchanged (Nutzer 08.11.). New dicts only: row['state'] stays
        untouched if the round fails later. learned_del_seen = time of the last request read."""
        st, requests = self.st, self.row.get('learned_del') or []
        if not requests:
            return
        zb = st.get('zb')
        current = '%d-%d' % tuple(zb) if zb else None
        for req in requests:
            details = req.get('details')
            if not isinstance(details, dict):
                details = {}                             # details not an object (jsonb): never fails the round
            zt, level, at = details.get('zieltemperatur'), details.get('stufe'), details.get('at')
            key = str(level)
            label = 'Lernwert Stufe %s (%s °C)' % (level, str(zt).replace('-', '–'))
            if not isinstance(zt, str):
                values = {}                              # malformed request: never fails the round
            elif zt == current:
                values = st.get('learned') or {}
            else:
                values = (st.get('learned_zb') or {}).get(zt) or {}
            entry = values.get(key)
            if at is None or not isinstance(entry, dict) or entry.get('at') != at:
                self.note(label + ' nicht gelöscht: nicht mehr vorhanden oder neu gelernt.')
                continue
            values = {k: v for k, v in values.items() if k != key}
            if zt == current:
                st['learned'] = values
            else:
                archive = dict(st.get('learned_zb') or {})
                if values:
                    archive[zt] = values
                else:
                    archive.pop(zt, None)
                st['learned_zb'] = archive
            self.note(label + ' gelöscht (Nutzer).')
        seen = requests[-1].get('recorded_at')        # not now: a request written meanwhile is read next round
        if seen is not None:
            st['learned_del_seen'] = _iso(seen) if isinstance(seen, datetime) else str(seen)

    # --- Zieltemperatur
    def target_range(self):
        """Neue Zieltemperatur: neue Grenzen sofort – kein Start, kein Taktsprung; Heiz-/Kühlmodus, Sperren und
        Startpause laufen mit den neuen Grenzen weiter. Lernwerte je Zieltemperatur gemerkt: zurück in der alten gelten
        wieder ihre Lernwerte, eine neue startet mit dem Lernwert der nächsten, sonst dem Initialwert (Nutzer 28.10.).
        Lernperiode endet, die ZeitImZieltemperatur wird neu gemessen. Ohne gemerkte Zieltemperatur (Stand vor dem
        27.10.): die Lernwerte gehören zum Startwert 82–85 – auch wenn bis zur ersten Runde schon eine andere gewählt
        wurde."""
        st = self.st
        new, old = [self.lo, self.hi], st.get('zb')
        if old is None:
            old = st['zb'] = list(ZB_START)
        if list(old) == new:
            return
        # neues dict: row['state'] bleibt bei einem Fehler später in der Runde unverändert (sonst ginge ein Archiv verloren)
        archive = dict(st.get('learned_zb') or {})
        archive['%d-%d' % tuple(old)] = st.get('learned') or {}
        st['learned'] = archive.pop('%d-%d' % tuple(new), {})
        st['learned_zb'] = archive
        self.end_learning('Zieltemperatur geändert')
        st['learn_out_since'] = None
        st['zt_since'] = None
        st['zb'] = new
        self.note('Zieltemperatur %d–%d °C übernommen.' % tuple(new))

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

    # --- locks of the clock increase (Nutzer 06.11.)
    def busy(self):
        return bool(self.st.get('pending') or self.st.get('queue') or self.st.get('settle'))

    def lock(self, key, seconds):
        """Clock-increase lock `key` until now + seconds; returns that time."""
        until = self.now + timedelta(seconds=seconds)
        self.st['locks'] = dict(self.st.get('locks') or {}, **{key: _iso(until)})
        return until

    def update_locks(self):
        """Expired locks become null; erhoehung, heizmodus and kuehlmodus end early as soon as t > ZTU+3 (only with a
        valid temperature); senkung never ends early."""
        locks = {}
        for key in LOCK_KEYS:
            until = (self.st.get('locks') or {}).get(key)
            early = key != 'senkung' and self.t is not None and self.t > self.lo + LOCK_EARLY_END
            locks[key] = until if until and _age(until, self.now) < 0 and not early else None
        self.st['locks'] = locks

    def locked(self):
        return any(until and _age(until, self.now) < 0 for until in (self.st.get('locks') or {}).values())

    # --- fan modes (Nutzer 06.11.)
    def fan_modes(self):
        """Exactly one fan mode: Kühlmodus (checked first), Heizmodus, otherwise Basismodus (fan = Basisstufe)."""
        st, t, lo, hi = self.st, self.t, self.lo, self.hi
        cool = st.get('cool')
        if cool:
            if t < hi - COOL_END:
                self.end_cool('Chiptemperatur %d °C unter %d °C' % (t, hi - COOL_END))
            else:
                if cool.get('plus', 1) < 2 and t > hi + COOL_PLUS2_FROM:
                    self.cool(2)                         # +1 becomes +2; the plus never goes down while it runs
                return
        elif t > hi + COOL_FROM:
            self.end_heat('Chiptemperatur %d °C über %d °C' % (t, lo - HEAT_END))   # only one mode at a time
            self.cool(2 if t > hi + COOL_PLUS2_FROM else 1)
            return
        if st.get('heat') and t > lo - HEAT_END:
            self.end_heat('Chiptemperatur %d °C über %d °C' % (t, lo - HEAT_END))
        if t < lo - HEAT_FROM and _age(st.get('fan_changed_at'), self.now) > HEAT_STEP_S:
            self.heat_step()
        if not st.get('heat'):
            self.set_fan_target(self.base, 'Basismodus: Basisstufe %d' % self.base)

    def cool(self, plus):
        """Kühlmodus starts (or +1 becomes +2): fan = Basisstufe + plus, at most level 10, at once."""
        st, t = self.st, self.t
        limit = self.hi + (COOL_PLUS2_FROM if plus == 2 else COOL_FROM)
        level = min(self.base + plus, FAN_MAX)
        old = st.get('fan_target')
        st['cool'] = {'since': (st.get('cool') or {}).get('since') or _iso(self.now), 'plus': plus}
        text = 'Kühlmodus: Chiptemperatur %d °C über %d °C, Lüfter Stufe %d (Basisstufe %d +%d)' % (
            t, limit, level, self.base, plus)
        self.set_fan_target(level, text)
        log_id = self.base_event(kind='kuehlmodus', trigger=text, fan_old=old, fan_new=level,
                                 details={'plus': plus, 'ab': limit + 1, 'basisstufe': self.base})
        self.note(text + '.')
        self.decide('Kühlmodus +%d' % plus, log_id)

    def end_cool(self, why, lock=True):
        """Kühlmodus ends: fan = Basisstufe; then 3 min no clock increase (not after a start)."""
        st = self.st
        if not st.get('cool'):
            return
        st['cool'] = None
        old = st.get('fan_target')
        self.set_fan_target(self.base, 'Kühlmodus beendet: Basisstufe %d' % self.base)
        text = 'Kühlmodus beendet: %s, Lüfter Stufe %d' % (why, self.base)
        if lock:
            text += '; keine Takterhöhung bis %s' % _hm(self.lock('kuehlmodus', MODE_END_LOCK_S))
        log_id = self.base_event(kind='kuehlmodus_ende', trigger=text, fan_old=old, fan_new=self.base)
        self.note(text + '.')
        self.decide('Kühlmodus beendet', log_id)

    def heat_step(self):
        """Heizmodus step: fan target −1 (at least level 1); the first step starts the Heizmodus – also when the fan
        is already at level 1."""
        st, t = self.st, self.t
        limit = self.lo - HEAT_FROM
        old = st.get('fan_target') or self.base
        level = max(old - 1, FAN_MIN)
        if not st.get('heat'):
            st['heat'] = {'since': _iso(self.now)}
            text = 'Heizmodus: Chiptemperatur %d °C unter %d °C, Lüfter Stufe %d (Basisstufe %d%s)' % (
                t, limit, level, self.base, _rel(level - self.base) and ' ' + _rel(level - self.base))
            log_id = self.base_event(kind='heizmodus', trigger=text, fan_old=old, fan_new=level,
                                     details={'unter': limit, 'basisstufe': self.base})
            self.note(text + '.')
            self.decide('Heizmodus', log_id)
        if level != old:
            self.set_fan_target(level, 'Heizmodus: Chiptemperatur %d °C unter %d °C, Lüfter Stufe %d → %d' % (
                t, limit, old, level))
            self.note('Heizmodus: Lüfter Stufe %d → %d (Chiptemperatur %d °C).' % (old, level, t))

    def end_heat(self, why, lock=True):
        """Heizmodus ends: fan = Basisstufe; then 3 min no clock increase (not after a start)."""
        st = self.st
        if not st.get('heat'):
            return
        st['heat'] = None
        old = st.get('fan_target')
        self.set_fan_target(self.base, 'Heizmodus beendet: Basisstufe %d' % self.base)
        text = 'Heizmodus beendet: %s, Lüfter Stufe %d' % (why, self.base)
        if lock:
            text += '; keine Takterhöhung bis %s' % _hm(self.lock('heizmodus', MODE_END_LOCK_S))
        log_id = self.base_event(kind='heizmodus_ende', trigger=text, fan_old=old, fan_new=self.base)
        self.note(text + '.')
        self.decide('Heizmodus beendet', log_id)

    def heat_off(self, why):
        """Ohne Regelung (Automatik aus, Miner antwortet nicht): ein laufender Heizmodus endet sofort, Lüfter auf die
        Basisstufe; ein fehlgeschlagener Lüfterbefehl wird wiederholt (Nutzer 26.10.)."""
        st = self.st
        if st.get('heat'):
            self.end_heat(why)
            st['heat_fan_due'] = True
        if not st.get('heat_fan_due'):
            return
        self.write_fan()
        if st.get('fan_set') == st.get('fan_target'):
            st.pop('heat_fan_due')
            fp = st.get('fan_pending')
            if fp and not self.row['enabled']:
                # Automatik aus: die Lüfterstufe wird nicht mehr gelesen – Übernahme nicht gemessen
                if fp.get('log_id'):
                    self.j.update(fp['log_id'], takeover_state='nicht gemessen')
                st['fan_pending'] = None

    # --- clock (Nutzer 06.11.)
    def start_hold_end(self):
        """Startpause ends at t ≥ ZTU−5, at the latest after 10 min."""
        st, t = self.st, self.t
        hold = st.get('start_hold')
        limit = self.lo - START_HOLD_BELOW
        if not hold or (t < limit and _age(hold['until'], self.now) < 0):
            return
        st['start_hold'] = None
        since = hold.get('since') or _iso(datetime.fromisoformat(hold['until']) - timedelta(seconds=START_HOLD_S))
        mins = int(_age(since, self.now) // 60)
        if t >= limit:
            text = 'Startpause beendet: %d °C erreicht (nach %d min)' % (limit, mins)
        else:
            text = 'Startpause beendet: 10 min vorbei (Chiptemperatur %d °C)' % t
        self.base_event(kind='startpause_ende', trigger=text, details={'seit': since})
        self.note(text + '.')

    def lower_clock(self):
        """Excursion above ZTO: reduce at its start at once – 3 min after the last reduction if that was less than
        3 min ago, at once if its Bereich is higher than at that one (Nutzer 08.11.); in the same Bereich every 2 min; rising into a higher Bereich at once; falling into a lower Bereich
        2 min after arriving there. The ZeitImZieltemperatur is fixed at the start of the excursion. t ≤ ZTO ends it."""
        st, t, now = self.st, self.t, self.now
        band = band_of(t, self.hi)
        hot = st.get('hot')
        if band is None:
            st['hot'] = None
            return
        if not hot:
            dwell = int(self.zt_dwell or 0)
            hot = {'band': band, 'since': _iso(now), 'band_at': _iso(now), 'next_cut_at': _iso(now),
                   'dwell_s': dwell, 'minus1': minus_band_1(dwell)}
            # a higher Bereich than at the last reduction reduces at once (Nutzer 08.11.)
            if _age(st.get('cut_at'), now) < CUT_GAP_S and band <= (st.get('cut_band') or 0):
                hot['next_cut_at'] = _iso(datetime.fromisoformat(st['cut_at']) + timedelta(seconds=CUT_GAP_S))
            due = _age(hot['next_cut_at'], now) >= 0
        elif band > hot['band']:
            hot, due = dict(hot, band=band, band_at=_iso(now)), True
        elif band < hot['band']:
            # 2 min from the arrival in the lower Bereich (and not before a 3-min wait at the start of the excursion)
            wait = max(datetime.fromisoformat(hot['next_cut_at']), now + timedelta(seconds=CUT_EVERY_S))
            hot, due = dict(hot, band=band, band_at=_iso(now), next_cut_at=_iso(wait)), False
        else:
            due = _age(hot['next_cut_at'], now) >= 0
        st['hot'] = hot
        if due and st.get('clock_set') is not None:
            self.cut(hot)

    def cut(self, hot):
        """One reduction (Bereich 1 by the ZeitImZieltemperatur, 2/3 −20, 4 −50 MHz); it replaces a running command.
        Afterwards 5 min no clock increase; the next one in this Bereich after 2 min."""
        st, t, now = self.st, self.t, self.now
        band = hot['band']
        # Bereich 1 from the fixed ZeitImZieltemperatur, so a running excursion follows a rule change (Nutzer 08.11., ae)
        minus = minus_band_1(hot['dwell_s']) if band == 1 else CUT_MINUS[band]
        if band == 1:
            hot = dict(hot, minus1=minus)
        old = int(round(st['clock_set']))
        if band == 1:
            why = 'Chiptemperatur %d °C (Bereich 1, ZeitImZieltemperatur %s): Takt −%d MHz' % (
                t, _dur(hot['dwell_s']), minus)
        else:
            why = 'Chiptemperatur %d °C (Bereich %d): Takt −%d MHz' % (t, band, minus)
        st['hot'] = dict(hot, next_cut_at=_iso(now + timedelta(seconds=CUT_EVERY_S)))
        details = {'bereich': band, 'zeit_im_ziel_s': hot['dwell_s']} if band == 1 else {'bereich': band}
        if self.queue_clock(old - minus, why, hot=True, step=-minus, details=details):
            st['cut_at'], st['cut_band'] = _iso(now), band
            if minus == CUT5_MHZ:
                st['cut5_at'] = _iso(now)                # Basisstufe 1: block of raise_clock (Nutzer 09.11.)
            until = self.lock('senkung', CUT_LOCK_S)
            self.note('Chiptemperatur %d °C (Bereich %d): Takt %d → %d MHz, keine Takterhöhung bis %s.' % (
                t, band, old, old - minus, _hm(until)))

    def raise_clock(self):
        """Clock increase below ZTU at the first such value, by the distance d = ZTU − t; then 2 or 3 min no increase.
        Not while a lock runs, during the Startpause, while a command is pending or queued, at 440 MHz or when more
        needs the waiting mode switch. At Basisstufe 1 not at ZTU−2..ZTU−1 for 30 min after a reduction by −5 MHz
        (cut5_at; silent like the locks, Nutzer 09.11.)."""
        st, t = self.st, self.t
        old = st.get('clock_set')
        if old is None or t >= self.lo or st.get('start_hold') or self.locked() or self.busy():
            return
        old = int(round(old))
        if old >= CLOCK_MAX:
            return
        current = level_of(self.m['mode_key'])
        if st.get('mode_wait') is not None and current is not None and old >= MODE_TOP[current]:
            return                                       # mehr geht erst mit dem wartenden Moduswechsel
        d = self.lo - t
        if (self.base == CUT5_BLOCK_BASE and d <= CUT5_BLOCK_D and st.get('cut5_at')
                and _age(st['cut5_at'], self.now) < CUT5_BLOCK_S):
            return                                       # Basisstufe 1: 30 min after −5 MHz not at ZTU−2..ZTU−1
        plus, lock_s = plus_rule(d)
        new = min(old + plus, CLOCK_MAX)
        until = self.now + timedelta(seconds=lock_s)
        why = 'Chiptemperatur %d °C (ZTU −%d): +%d MHz, keine Takterhöhung bis %s' % (t, d, new - old, _hm(until))
        if self.queue_clock(new, why, step=new - old, details={'sperre_bis': _iso(until)}):
            self.lock('erhoehung', lock_s)

    def rules(self):
        """With a valid temperature, in this order: Startpause end, fan mode (its end may set a lock), locks (expired,
        early end above ZTU+3), clock reduction above ZTO, clock increase below ZTU."""
        self.start_hold_end()
        self.fan_modes()
        self.update_locks()
        self.lower_clock()
        if self.t < self.lo:
            self.raise_clock()

    # --- Takt laut Miner weicht ab (Nutzer 06.11.)
    def firmware_clock(self):
        """More than 15 MHz between the clock reported by the miner and the set clock for 15 min without a break: send
        the set clock again (mode first if needed). The 15 min start anew at ≤ 15 MHz, when the set clock changes or a
        command is pending or queued, and after sending; without a value (run) and during Wartung as well."""
        st, now = self.st, self.now
        clock_set, clock_now = st.get('clock_set'), st.get('clock_now')
        if (self.busy() or self.row.get('maintenance_at') or not clock_set or clock_now is None
                or abs(clock_now - clock_set) <= FW_DEV_MHZ):
            st['fw_dev'] = None
            return
        fw = st.get('fw_dev')
        if not fw or fw.get('clock') != clock_set:
            st['fw_dev'] = {'since': _iso(now), 'clock': clock_set}
            return
        if _age(fw['since'], now) < FW_DEV_S:
            return
        st['fw_dev'] = None
        text = 'Takt laut Miner %.0f MHz, gesetzt %s MHz: seit 15 min mehr als %d MHz Abweichung' % (
            clock_now, clock_set, FW_DEV_MHZ)
        log_id = self.base_event(kind='firmware_takt', trigger=text, details={'seit': fw['since']})
        self.note('Takt laut Miner weicht seit 15 min ab (%.0f MHz laut Miner): %s MHz erneut.' % (clock_now, clock_set))
        self.decide('Takt erneut gesendet', log_id)
        current = level_of(self.m['mode_key'])
        level = required_level(clock_set, current)
        q = []
        if level != current:
            q.append({'kind': 'modus', 'value': level, 'why': 'Takt erneut: Modus'})
        q.append({'kind': 'takt', 'value': int(round(clock_set)), 'why': 'Takt laut Miner weicht ab: erneut senden'})
        st['queue'] = q

    # --- Fehlerrate (Nutzer 06.11.)
    def dh_reset(self):
        """Die 30 min der Fehlerrate beginnen neu (fehlender Wert, Miner antwortet nicht, Wartung, Over Hot …)."""
        self.st['dh_since'] = self.st['dh_noted'] = None

    def error_rate(self, over_hot):
        """Fehlerrate DH (jede Runde gelesen): über 10 % ununterbrochen 30 min und kein automatischer Neustart dieser
        Regel in den letzten 6 h → automatischer Neustart. Not counted during Wartung and Over Hot; a missing value
        or one ≤ 10 % starts the 30 min anew. Blocked by the 6 h: one note only. After a failed send the next try after
        5 min; after a sent restart the 30 min start anew. No dependency on the set clock or a start."""
        st, now = self.st, self.now
        dh = self.m.get('dh')
        if over_hot or self.row.get('maintenance_at') or dh is None or dh <= DH_RESTART_PCT:
            self.dh_reset()
            return
        st['dh_since'] = st.get('dh_since') or _iso(now)
        if _age(st['dh_since'], now) < DH_RESTART_S:
            return
        if _age(st.get('auto_restart_at'), now) < AUTO_RESTART_GAP_S:
            if not st.get('dh_noted'):
                st['dh_noted'] = True
                self.note('Fehlerrate %.1f %% seit 30 min über %d %%: kein automatischer Neustart (höchstens einer je '
                          '6 h, letzter um %s).' % (dh, DH_RESTART_PCT,
                                                    _hm(datetime.fromisoformat(st['auto_restart_at']))))
            return
        if _age(st.get('auto_restart_fail_at'), now) < AUTO_RESTART_RETRY_S:
            return
        self.auto_restart(dh)

    def auto_restart(self, dh):
        """Automatischer Neustart: Neustart-Befehl über die Mining-Schnittstelle (wie der Knopf „A15 neu starten“).
        Gesendet: „nicht erreichbar“ gilt als erwarteter Neustart, die 30 min beginnen neu; Fehler: nächster Versuch
        frühestens nach 5 min (zählt nicht als Neustart der 6 h)."""
        st, row = self.st, self.row
        clock = int(round(st.get('clock_set') or 0))
        endpoint = 'cgminer %s:%s' % (row['ip_address'], row['port'] or 4028)
        sent, t0 = _now(), time.monotonic()
        try:
            reply = calibration.reboot(row['ip_address'], row['port'])
        except (OSError, RuntimeError) as exc:
            err = str(exc)[:500]
            st['auto_restart_fail_at'] = _iso(self.now)
            log_id = self.base_event(kind='fehler', trigger='Automatischer Neustart: Befehl nicht gesendet', target='miner',
                                     endpoint=endpoint, request='ascset|0,reboot,0', status='Fehler', ok=False,
                                     error=err, api_sent_at=sent, api_reply_at=_now(),
                                     api_ms=int((time.monotonic() - t0) * 1000),
                                     details={'grund': 'neustart_auto', 'fehlerrate': dh, 'takt': clock})
            self.note('Automatischer Neustart fehlgeschlagen: %s – nächster Versuch frühestens in 5 min.' % err)
            self.decide('Fehler: automatischer Neustart', log_id)
            return
        text = 'Automatischer Neustart: Fehlerrate %.1f %% seit 30 min über %d %% (Takt %d MHz)' % (dh, DH_RESTART_PCT,
                                                                                                     clock)
        st['auto_restart_at'] = st['restart_req'] = _iso(self.now)   # „nicht erreichbar“ gehört zum Neustart
        st['auto_restart_fail_at'] = None
        self.dh_reset()                                  # after the restart the 30 min start anew
        log_id = self.base_event(kind='neustart_auto', trigger=text, target='miner', endpoint=endpoint,
                                 request='ascset|0,reboot,0', response=reply[:300], ok=True, api_sent_at=sent,
                                 api_reply_at=_now(), api_ms=int((time.monotonic() - t0) * 1000),
                                 details={'fehlerrate': dh, 'takt': clock})
        self.note(text + '.')
        self.decide('Automatischer Neustart', log_id)

    # --- main
    def run(self):
        """One round, in this order:
          0. Lernwert löschen (requests of the user), also with the Automatik off (Nutzer 08.11.).
          1. Automatik aus: Heizmodus ends (fan = Basisstufe), no clock; protocol, done.
          2. Automatik ein (or another STATE_VERSION): state anew (start 'Automatik ein'); state keys of the removed
             rules go once; Zieltemperatur taken over; Neustart button remembered.
          3. Read the miner (Wartung: fan off while the A15 is off; miner silent: 'nicht erreichbar' as restart or
             error, Heizmodus ends) – without a reading: protocol, done.
          4. Readings: restart of the miner, Basisstufe chosen, Over Hot (Heizmodus ends); t, tendency (display),
             ZeitImZieltemperatur; expired locks.
          5. Start, if due (needs reported clocks, not during Over Hot).
          6. Not during Over Hot: takeover of the command, waiting mode switch, Takt laut Miner weicht ab.
          7. Fehlerrate (automatic restart).
          8. With a valid temperature (rules): Startpause end → fan mode (Kühlmodus, Heizmodus, Basismodus; may set a
             lock) → locks (end early above ZTU+3) → clock reduction above ZTO → clock increase below ZTU.
          9. Fan command, then miner commands (not during Over Hot), learning (valid temperature), read the fan.
         10. Phase (heizmodus | basismodus | kuehlmodus) and the protocol of the round (automation_cycle)."""
        self.learned_delete()
        st, row, now = self.st, self.row, self.now
        if not row['enabled']:
            if not st.get('off'):
                self.note('Automatik aus: Takt bleibt %s.' % ('%s MHz' % st['clock_set'] if st.get('clock_set') else 'wie er ist'))
                self.drop_pending('Automatik aus')
            self.heat_off('Automatik aus')
            st.update({'off': True, 'phase': 'aus', 'queue': [], 'settle': None})
            self.j.cycle(recorded_at=now, phase='aus', base_level=self.base, clock_set=st.get('clock_set'),
                         fan_level=st.get('fan_set'), decision='Automatik aus')
            return st
        if st.pop('off', False) or st.get('v') != STATE_VERSION:
            # Automatik ein bzw. Umstellung auf die neue Regelung (startet ohne Lernwerte, Plan Kapitel 7); locks, cut_at
            # and the ZeitImZieltemperatur stay over a start, heat/cool end in it (Nutzer 06.11.)
            keep = ('log', 'stock', 'fan_set', 'fan_read', 'fan_read_at', 'fan_set_at', 'elapsed', 'mode_key',
                    'maint', 'maint_ended_at', 'offline', 'over_hot', 'restart_req_seen', 'zb', 'learned_zb',
                    'auto_restart_at', 'heat', 'cool', 'locks', 'cut_at', 'cut_band', 'zt_since', 'learned_del_seen')
            new = {k: st[k] for k in keep if k in st}
            new['learned'] = (st.get('learned') or {}) if st.get('v') == STATE_VERSION else {}
            if new.get('stock') is None:
                new['stock'] = {}
            new.update({'v': STATE_VERSION, 'level_seen': str(row.get('level_at')), 'start_reason': 'Automatik ein',
                        'fan_target': st.get('fan_set') or self.base})
            st = self.st = new
            self.note('Automatik ein (Basisstufe %d).' % self.base)
        self.drop_old_rules()
        self.target_range()
        self.update_locks()                              # expired locks (early end needs the temperature: rules)

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
                self.dh_reset()
                st['fw_dev'] = None
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
                self.dh_reset()                          # Fehlerrate: die 30 min beginnen neu
                st['fw_dev'] = None                      # Takt laut Miner: die 15 min beginnen neu
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
                if st['offline'] is True:
                    self.heat_off('Miner antwortet nicht')
                st['phase'] = self.phase()               # the last mode; Basismodus once heat_off ended the Heizmodus
                self.j.cycle(recorded_at=now, phase=st['phase'], base_level=self.base,
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

        # Neustart (nicht durch den eigenen Moduswechsel), Stufenwahl, Over Hot
        own_switch = (st.get('pending') or {}).get('kind') == 'modus' or _age(st.get('mode_sent_at'), now) < OWN_SWITCH_S
        if (st.get('elapsed') is not None and m['elapsed'] is not None and m['elapsed'] < st['elapsed']
                and not own_switch):
            self.base_event(kind='neustart', trigger='Laufzeit des Miners zurückgesetzt (%s s → %s s)' % (st['elapsed'], m['elapsed']))
            st['start_reason'] = 'Neustart des Miners'
            st['start_cold'] = True
            since = st.get('restart_req')                 # Knopf: nur Einträge ab dem Klick
            st['restart_req'] = None
            self.relabel_restart(since)
        st['elapsed'] = m['elapsed']
        level_at = row.get('level_at')
        if level_at is not None and str(level_at) != st.get('level_seen'):
            st['level_seen'] = str(level_at)
            st['start_reason'] = 'Basisstufe gewählt (Stufe %d)' % self.base

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
            self.end_heat('Over Hot')
        if not over_hot and st.get('over_hot'):
            st.pop('over_hot')
            st['start_reason'] = 'nach Over Hot'
            st['start_cold'] = True

        if valid:
            self.t = int(round(tmax))
            temps = [x for x in st.get('temps') or [] if _age(x[0], now) <= TEMP_KEEP_S]
            st['temps'] = temps
            st['t'] = self.t
            st['tendency'], self.ref = self.tendency()
            temps.append([_iso(now), self.t])
            # ZeitImZieltemperatur (Nutzer 06.11.): time in ZTU..ZTO without a break since the last entry (from below or
            # above); a value below ZTU or above ZTO ends the stay; measured with the first value above ZTO
            if self.lo <= self.t <= self.hi:
                st['zt_since'] = st.get('zt_since') or _iso(now)
            else:
                if self.t > self.hi:
                    self.zt_dwell = _age(st['zt_since'], now) if st.get('zt_since') else 0
                st['zt_since'] = None
            self.update_thermal()
        if st.get('fan_target') is None:
            st['fan_target'] = st.get('fan_set') or self.base

        # Start: braucht keine Temperatur, nur gemeldete Takte; nicht während Over Hot
        if st.get('start_reason') and not over_hot and m['points']:
            self.start(st['start_reason'])

        if over_hot:
            st['fw_dev'] = None                            # Takt laut Miner: no value during Over Hot
        else:
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
            self.firmware_clock()
        self.error_rate(over_hot)                          # automatischer Neustart (Nutzer 06.11.)
        if valid:
            self.rules()
        self.write_fan()                                   # Lüfter vor den (möglicherweise langsamen) Miner-Befehlen
        if not over_hot:
            self.resend_or_send()
            if valid:
                self.learn()
        self.read_fan()
        st['phase'] = self.phase()
        p = st.get('pending')
        self.j.cycle(recorded_at=now, phase=st['phase'], chip_temp=self.t, boards=st.get('boards'),
                     tendency=st.get('tendency') if valid else None, clock_set=st.get('clock_set'),
                     clock_reported=st.get('clock_now'), points=st.get('points'), mode_key=m['mode_key'] or '',
                     base_level=self.base, fan_level=st.get('fan_set'), fan_read=st.get('fan_read'),
                     power_w=m.get('power'), ghsspd_ths=(m['ghsspd'] / 1000.0) if m.get('ghsspd') else None,
                     dh_pct=m.get('dh'), status=(m['status'] or '')[:40], learn_since=st.get('learn_since'),
                     pending=('%s %s' % (p['kind'], p['value'])) if p else '',
                     decision=' · '.join(self.decisions)[:200] if self.decisions else ('Over Hot' if over_hot else
                                                                                         ('' if valid else 'Chiptemperatur ungültig')),
                     log=self.cycle_log)
        return st


def step(row, now, ac, journal=None):
    """One round for one automation row; returns the new state."""
    return Round(row, now, ac, journal or automation_journal.MemoryJournal()).run()


# requests „Lernwert löschen“ not yet processed (Nutzer 08.11.), inside the main SELECT (f = fan_automation, d = devices)
LEARNED_DEL_SQL = ("FROM automation_log l WHERE l.device_id = d.id AND l.kind = 'lernwert_loeschen' "
                   "AND l.source = 'nutzer' AND l.recorded_at > "
                   "COALESCE((f.state->>'learned_del_seen')::timestamptz, '-infinity'::timestamptz)")


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
                "SELECT f.id, f.enabled, f.ac_dev_id, f.ac_port, f.level, f.level_at, f.maintenance_at, f.state, "
                "f.target_low, f.target_high, "
                "d.id AS device_id, d.ip_address, d.port, "
                "(SELECT max(l.recorded_at) FROM automation_log l WHERE l.device_id = d.id AND l.kind = 'neustart_nutzer') "
                "AS restart_req_at, d.connection_config, "
                "(SELECT json_agg(json_build_object('recorded_at', l.recorded_at, 'details', l.details) "
                "ORDER BY l.recorded_at, l.id) " + LEARNED_DEL_SQL + ") AS learned_del "
                "FROM fan_automation f JOIN devices d ON d.id = f.device_id "
                "WHERE f.enabled OR NOT COALESCE((f.state->>'off')::boolean, false) "
                "OR COALESCE((f.state->>'heat_fan_due')::boolean, false) "     # Automatik aus: Lüfterbefehl wiederholen
                "OR EXISTS (SELECT 1 " + LEARNED_DEL_SQL + ")")   # Automatik aus: Lernwert trotzdem löschen (Nutzer 08.11.)
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
                if state.pop('maint_end', False):
                    # the A15 is back on: end the maintenance (only the one that was announced)
                    cur.execute("UPDATE fan_automation SET maintenance_at = NULL, state = %s WHERE id = %s "
                                "AND maintenance_at = %s", (json.dumps(state), row['id'], row['maintenance_at']))
            conn.commit()
    finally:
        conn.close()
