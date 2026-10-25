"""Protokoll der Temperatur-Automatik (Tabellen automation_log und automation_cycle, Modelle im Backend).

automation_log  : ein Eintrag je Entscheidung, Befehl und Versuch (Takt, Modus, Lüfter, Lernen, Fehler ...),
                  mit API-Reaktionszeit, Übernahmezeit und thermischem Verlauf (nachgetragen).
automation_cycle: ein Eintrag je Regelzyklus (alle 5 s), auch ohne Entscheidung.
Beide werden 360 Tage behalten (prune, einmal täglich).
Zugangsdaten (Pool-Passwörter, Tokens) werden nie geschrieben; die Aufrufer übergeben nur bereinigte Texte.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.errors

logger = logging.getLogger(__name__)

KEEP_DAYS = 360
PRUNE_BATCH = 10000
TEXT_MAX = 4000

LOG_FIELDS = ('recorded_at', 'kind', 'source', 'trigger', 'phase', 'chip_temp', 'tendency', 'base_old', 'base_new',
              'fan_old', 'fan_new', 'clock_old', 'clock_new', 'clock_reported', 'mode_old', 'mode_new', 'target',
              'endpoint', 'request', 'response', 'status', 'ok', 'error', 'attempt', 'api_sent_at', 'api_reply_at',
              'api_ms', 'takeover_at', 'takeover_s', 'takeover_state', 'temp_before', 'thermal', 'details')
CYCLE_FIELDS = ('recorded_at', 'phase', 'chip_temp', 'boards', 'tendency', 'clock_set', 'clock_reported', 'points',
                'mode_key', 'base_level', 'fan_level', 'fan_read', 'power_w', 'ghsspd_ths', 'dh_pct', 'status',
                'window_active', 'learn_since', 'pending', 'decision', 'log')
JSON_FIELDS = {'thermal', 'details', 'boards', 'points'}
TEXT_DEFAULTS = {'trigger': '', 'phase': '', 'mode_old': '', 'mode_new': '', 'target': '', 'endpoint': '',
                 'request': '', 'response': '', 'status': '', 'error': '', 'takeover_state': '', 'source': 'regelung',
                 'mode_key': '', 'pending': '', 'decision': ''}


def _value(name, value):
    if name in JSON_FIELDS:
        return json.dumps(value if value is not None else ([] if name in ('thermal', 'boards', 'points') else {}))
    if value is None and name in TEXT_DEFAULTS:
        return TEXT_DEFAULTS[name]
    if name == 'window_active':
        return bool(value)
    if isinstance(value, str) and len(value) > TEXT_MAX:
        return value[:TEXT_MAX]
    return value


_shared = {'conn': None, 'url': None, 'muted_until': 0.0}
MUTE_S = 600                  # Tabellen fehlen (noch nicht migriert): 10 min nichts schreiben, nur einmal warnen


def connect(database_url):
    """Eine dauerhafte Verbindung (autocommit), bei Bedarf neu aufgebaut: ein Protokollfehler berührt nie die
    Transaktion der Regelung. None, wenn die Datenbank nicht erreichbar ist (die Regelung läuft weiter)."""
    conn = _shared['conn']
    if conn is not None and not conn.closed and _shared['url'] == database_url:
        return conn
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = True
    except psycopg2.Error as exc:
        logger.warning('Protokoll: keine Datenbank: %s', exc)
        return None
    _shared.update(conn=conn, url=database_url)
    return conn


def _drop_connection():
    conn = _shared['conn']
    _shared['conn'] = None
    if conn is not None:
        try:
            conn.close()
        except psycopg2.Error:
            pass


class Journal:
    """Writes the protocol. Errors (e.g. tables not migrated yet) never stop the regulation."""

    def __init__(self, conn, device_id):
        self.conn = conn
        self.device_id = device_id

    def _execute(self, sql, params, what, fetch=False):
        if self.conn is None or time.monotonic() < _shared['muted_until']:
            return None
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()[0] if fetch else None
        except psycopg2.errors.UndefinedTable as exc:
            _shared['muted_until'] = time.monotonic() + MUTE_S
            logger.warning('Protokoll: Tabelle fehlt (Migration 0024 noch nicht eingespielt?), 10 min Pause: %s', exc)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            _drop_connection()
            self.conn = None
            logger.warning('Protokoll: Verbindung verloren, wird neu aufgebaut: %s', exc)
        except psycopg2.Error as exc:
            logger.warning('Protokoll: %s nicht gespeichert: %s', what, exc)
        return None

    def event(self, **fields):
        fields.setdefault('recorded_at', datetime.now(timezone.utc))
        fields.setdefault('attempt', 1)
        cols = [f for f in LOG_FIELDS if f in fields or f in JSON_FIELDS or f in TEXT_DEFAULTS]
        return self._execute(
            'INSERT INTO automation_log (device_id, %s) VALUES (%%s, %s) RETURNING id'
            % (', '.join(cols), ', '.join(['%s'] * len(cols))),
            [self.device_id] + [_value(c, fields.get(c)) for c in cols], 'Eintrag', fetch=True)

    def update(self, log_id, **fields):
        cols = [f for f in fields if f in LOG_FIELDS]
        if not log_id or not cols:
            return
        self._execute('UPDATE automation_log SET %s WHERE id = %%s' % ', '.join('%s = %%s' % c for c in cols),
                      [_value(c, fields[c]) for c in cols] + [log_id], 'Nachtrag')

    def cycle(self, **fields):
        fields.setdefault('recorded_at', datetime.now(timezone.utc))
        cols = list(CYCLE_FIELDS)
        self._execute('INSERT INTO automation_cycle (device_id, %s) VALUES (%%s, %s)'
                      % (', '.join(cols), ', '.join(['%s'] * len(cols))),
                      [self.device_id] + [_value(c, fields.get(c)) for c in cols], 'Zyklus')


class MemoryJournal:
    """For tests: keeps everything in lists."""

    def __init__(self):
        self.events, self.cycles = [], []

    def event(self, **fields):
        fields['id'] = len(self.events) + 1
        self.events.append(fields)
        return fields['id']

    def update(self, log_id, **fields):
        for ev in self.events:
            if ev['id'] == log_id:
                ev.update(fields)

    def cycle(self, **fields):
        self.cycles.append(fields)

    def kinds(self):
        return [e['kind'] for e in self.events]


def prune(database_url, days=KEEP_DAYS):
    """Delete entries older than `days` from both tables, in batches (the regulation keeps running)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = {'automation_log': 0, 'automation_cycle': 0}
    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.Error as exc:
        logger.warning('Protokoll aufräumen: keine Datenbank: %s', exc)
        return deleted
    try:
        for table in deleted:
            while True:
                with conn.cursor() as cur:
                    cur.execute('DELETE FROM %s WHERE id IN (SELECT id FROM %s WHERE recorded_at < %%s LIMIT %d)'
                                % (table, table, PRUNE_BATCH), (cutoff,))
                    n = cur.rowcount
                conn.commit()
                deleted[table] += n
                if n < PRUNE_BATCH:
                    break
        if any(deleted.values()):
            logger.info('Protokoll aufgeräumt (älter als %d Tage): %s', days, deleted)
    except psycopg2.Error as exc:
        conn.rollback()
        logger.warning('Protokoll aufräumen fehlgeschlagen: %s', exc)
    finally:
        conn.close()
    return deleted
