"""Temperatur-Automatik (A15 + AC Infinity fan), every 15 s from the data-service scheduler.

Normal operation: the fan runs at the level chosen on the "Lüfter" page (never below it); the A15 clock
keeps the hottest chip (TMax, median of the last 60 s) between T_LOW and T_HIGH:
  above T_HIGH: clock down 10 MHz per 1.5 degC (max 30 MHz), then wait WAIT_S,
  below T_LOW : clock up 10 MHz (at most to the stock clock of the mode), then wait WAIT_S.
A lower fan level is applied only after the clock brought TMax to T_LOW and WAIT_S passed; a higher one
at once. Protection: TMax >= T_PROTECT1 -> clock -30 MHz and fan +1 level, >= T_PROTECT2 -> fan +2 levels,
back to the chosen level at TMax <= T_HIGH.
During a Takt-Tabellen run no clock is set; the fan holds TMax at T_CAL (+-CAL_BAND, 1 level per WAIT_S).

Live only (MINERSENTINEL_ENV != Test). The AC Infinity client is the backend's (backend/api/acinfinity.py,
mounted read-only), so both use the same stored session. The data-service writes only fan_automation.state.
"""
import importlib.util
import json
import logging
import math
import os
import statistics
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

import calibration

logger = logging.getLogger(__name__)

T_LOW, T_HIGH = 80.0, 84.0
T_PROTECT1, T_PROTECT2 = 88.0, 90.0
T_CAL, CAL_BAND = 80.0, 1.0
WAIT_S = 180                 # measured: ~85 % of a temperature change after 3 min
PROTECT_WAIT_S = 60
STEP_MHZ = 10
C_PER_STEP = 1.5             # measured (run 7, 320 -> 280 MHz): ~1.5 degC per 10 MHz
MAX_STEP_MHZ = 30
FLOOR_SHARE = 0.15
TEMP_WINDOW_S = 60           # readings kept; the band uses the median of the last 30 s, protection the latest
FAN_READ_S = 60
FAN_WRITE_GAP_S = 20
FAN_MIN, FAN_MAX = 1, 10
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


def _stock(row, mode_key):
    prof = ((((row['connection_config'] or {}).get('throttle') or {}).get('profiles') or {}).get(mode_key) or {})
    return prof.get('points')


def _points_for(stock, mhz):
    factor = mhz / (sum(stock) / len(stock))
    return [min(max(int(round(p * factor)), int(p * FLOOR_SHARE)), p) for p in stock]


def _clock_step(st, now, t, t_fast, cur, stock_avg, upper, lowering):
    """New average clock or None (no change)."""
    since = _age(st.get('changed_at'), now)
    if t_fast >= T_PROTECT1 and since >= PROTECT_WAIT_S:
        new = cur - MAX_STEP_MHZ
    elif t > upper and since >= WAIT_S:
        new = cur - min(MAX_STEP_MHZ, STEP_MHZ * math.ceil((t - upper) / C_PER_STEP))
    elif t < T_LOW and not lowering and since >= WAIT_S and cur < stock_avg - 0.5:
        new = cur + STEP_MHZ
    else:
        return None
    new = min(max(new, stock_avg * FLOOR_SHARE), stock_avg)
    return new if abs(new - cur) >= 1 else None


def _boost(st, t):
    boost = int(st.get('boost') or 0)
    if t >= T_PROTECT2:
        return 2
    if t >= T_PROTECT1:
        return max(boost, 1)
    if t <= T_HIGH:
        return 0
    return boost


def step(row, now, ac):
    """One round for one automation row; returns the new state."""
    st = dict(row['state'] or {})
    ip, port = row['ip_address'], row['port']
    if not row['enabled']:
        if st.get('clock_set') and st.get('stock_points'):
            calibration.set_frequency(ip, port, st['stock_points'])     # clocks back to the firmware's
            _note(st, now, 'Automatik aus: Standardtakt wieder gesetzt.')
        else:
            _note(st, now, 'Automatik aus.')
        st.update({'clock_set': None, 'off': True, 'phase': 'aus'})
        return st
    if st.pop('off', False) or not st.get('seen_at'):
        st = {'log': st.get('log') or []}
        _note(st, now, 'Automatik ein (Stufe %d).' % row['level'])

    m = calibration.read_miner(ip, port, fresh=True)     # always the miner's current values
    st['seen_at'] = _iso(now)
    rebooted = st.get('elapsed') is not None and m['elapsed'] is not None and m['elapsed'] < st['elapsed']
    if st.get('mode_key') not in (None, m['mode_key']) or rebooted:
        if st.get('clock_set'):
            _note(st, now, 'Neustart/Moduswechsel: Firmware-Takt, Regelung beginnt neu.')
        st['clock_set'] = None
        st['changed_at'] = _iso(now)
    st['mode_key'], st['elapsed'] = m['mode_key'], m['elapsed']
    st['clock_now'] = round(sum(m['points']) / len(m['points']), 1) if m['points'] else None   # for the page

    tmax = calibration._num(m['fields'].get('TMax'))
    temps = [x for x in st.get('temps') or [] if _age(x[0], now) <= TEMP_WINDOW_S]
    if tmax is not None and -50 < tmax < 200 and 'Over Hot' not in m['status']:
        temps.append([_iso(now), tmax])
    st['temps'] = temps
    recent = [x[1] for x in temps if _age(x[0], now) <= 30]
    # band 80-84: median of the last 30 s; protection (88/90): the latest reading
    t = statistics.median(recent) if len(recent) >= 2 else None
    t_fast = temps[-1][1] if temps and _age(temps[-1][0], now) < 1 else None
    if t is not None and t_fast is None:
        t_fast = t
    st['t'], st['t_now'] = t, t_fast

    # --- fan: read every FAN_READ_S ---
    dev_id, fport = row['ac_dev_id'], row['ac_port']
    st['ac_error'] = None
    if _age(st.get('fan_read_at'), now) >= FAN_READ_S:
        try:
            st['fan_read'] = ac.speed_of(dev_id, fport)
            st['fan_read_at'] = _iso(now)
        except ac.ACError as exc:
            st['ac_error'] = str(exc)
    if st.get('base') is None:
        if st.get('fan_read') is None:
            return st                                   # the fan level must be known first
        st['base'] = max(int(st['fan_read']), FAN_MIN)
        st.setdefault('fan_set', int(st['fan_read']))   # the fan already runs at this level
        if st['fan_set'] is None:
            st['fan_set'] = int(st['fan_read'])

    cal = bool(row['cal_active'])
    st['cal'] = cal
    stock = _stock(row, m['mode_key'])
    if stock:
        st['stock_points'] = stock
        st['stock_mhz'] = round(sum(stock) / len(stock), 1)

    if cal:
        # Takt-Tabellen: no clocks; the fan holds T_CAL
        st['phase'] = 'messung'
        fan = int(st.get('cal_fan') or st.get('fan_set') or st['base'])
        since_fan = _age(st.get('cal_fan_at'), now)
        new = fan
        if t is not None:
            if t_fast >= T_PROTECT2 and since_fan >= PROTECT_WAIT_S:
                new = fan + 2
            elif t_fast >= T_PROTECT1 and since_fan >= PROTECT_WAIT_S:
                new = fan + 1
            elif t > T_CAL + CAL_BAND and since_fan >= WAIT_S:
                new = fan + 1
            elif t < T_CAL - CAL_BAND and since_fan >= WAIT_S:
                new = fan - 1
        new = min(max(new, FAN_MIN), FAN_MAX)
        if new != fan or st.get('cal_fan') is None:
            st['cal_fan_at'] = _iso(now)
            if new != fan:
                _note(st, now, 'Takt-Messung: TMax %.0f °C → Lüfter Stufe %d.' % (t, new))
        st['cal_fan'] = new
        target_fan = new
    else:
        if st.pop('cal_fan', None) is not None:
            st['base'] = max(int(st.get('fan_set') or st['base']), st['base'])
            st.pop('cal_fan_at', None)
            _note(st, now, 'Takt-Messung beendet: zurück zur gewählten Stufe %d.' % row['level'])
        chosen = int(row['level'])
        if chosen > st['base']:
            st['base'] = chosen                         # higher level: at once
        lowering = chosen < st['base']
        if lowering and not st.get('lower_since'):
            st['lower_since'] = _iso(now)
        st['phase'] = 'senken' if lowering else 'normal'
        if lowering and not stock and _age(st.get('lower_since'), now) >= WAIT_S:
            st['base'] = chosen                         # no stock clock known: no clock step possible
            lowering = False
        if not lowering:
            st.pop('lower_since', None)
        # a manual clock (allowed while a run is paused) has priority: no clock changes then
        throttled = bool((((row['connection_config'] or {}).get('throttle') or {}).get('active')))
        st['throttled'] = throttled
        if lowering and throttled and _age(st.get('lower_since'), now) >= WAIT_S:
            st['base'] = chosen
            lowering = False
            st.pop('lower_since', None)
        if t is not None:
            st['boost'] = _boost(st, t_fast)
            if stock and not throttled:
                stock_avg = st['stock_mhz']
                # the clock the miner runs now (also a manual clock from before the automation)
                cur = sum(m['points']) / len(m['points']) if m['points'] else stock_avg
                new = _clock_step(st, now, t, t_fast, cur, stock_avg, T_LOW if lowering else T_HIGH, lowering)
                if new is not None:
                    points = _points_for(stock, new)
                    calibration.set_frequency(ip, port, points)
                    st['clock_set'] = round(new, 1) if new < stock_avg - 0.5 else None
                    st['changed_at'] = _iso(now)
                    _note(st, now, 'TMax %.1f °C → Takt %.0f MHz.' % (t, new))
                if lowering:
                    floor = st.get('clock_set') and st['clock_set'] <= stock_avg * FLOOR_SHARE + 0.5
                    settled = _age(st.get('changed_at'), now) >= WAIT_S
                    if settled and (t <= T_LOW or floor):
                        _note(st, now, 'Temperatur %.1f °C stabil → Lüfter auf Stufe %d.' % (t, chosen))
                        st['base'] = chosen
                        st['phase'] = 'normal'
                        st.pop('lower_since', None)
        target_fan = min(st['base'] + int(st.get('boost') or 0), FAN_MAX)

    # --- fan: write the target level (at most every FAN_WRITE_GAP_S) ---
    differs = target_fan != st.get('fan_set') or (st.get('fan_read') is not None and st['fan_read'] != target_fan
                                                   and _age(st.get('fan_set_at'), now) >= FAN_READ_S)
    if differs and _age(st.get('fan_set_at'), now) >= FAN_WRITE_GAP_S:
        try:
            ac.set_fan_level(dev_id, fport, target_fan)
            if target_fan != st.get('fan_set'):
                _note(st, now, 'Lüfter Stufe %d gesetzt.' % target_fan)
            st['fan_set'] = target_fan
            st['fan_set_at'] = _iso(now)
            st['fan_read_at'] = None                    # read back in the next round
        except ac.ACError as exc:
            st['ac_error'] = str(exc)
    return st


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
                "SELECT f.id, f.enabled, f.ac_dev_id, f.ac_port, f.level, f.state, d.ip_address, d.port, "
                "d.connection_config, EXISTS (SELECT 1 FROM calibration_runs r WHERE r.device_id = d.id "
                "AND r.status IN ('running', 'cancelling', 'finishing')) AS cal_active "
                "FROM fan_automation f JOIN devices d ON d.id = f.device_id "
                "WHERE f.enabled OR NOT COALESCE((f.state->>'off')::boolean, false)")
            rows = cur.fetchall()
        for row in rows:
            try:
                state = step(row, _now(), ac or load_ac())
                state.pop('last_error', None)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                state = dict(row['state'] or {})
                state['last_error'] = '%s: %s' % (_now().strftime('%H:%M:%S UTC'), str(exc)[:200])
                logger.info('Temperatur-Automatik: %s', exc)
            with conn.cursor() as cur:
                cur.execute("UPDATE fan_automation SET state = %s WHERE id = %s", (json.dumps(state), row['id']))
            conn.commit()
    finally:
        conn.close()
