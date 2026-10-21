"""Temperatur-Automatik (A15 + AC Infinity fan), right after every central reading (live_reader.py, 5 s).

Normal operation: the fan runs at the level chosen on the "Lüfter" page (never below it); the A15 clock
keeps the hottest chip (TMax, median of the last 60 s) between T_LOW and T_HIGH:
  above T_HIGH: clock down 10 MHz per 1.5 degC (max 30 MHz), then wait WAIT_S,
  below T_LOW : clock up 10 MHz (at most to the stock clock of the mode), then wait WAIT_S.
A lower fan level is applied only after the clock brought TMax to T_LOW and WAIT_S passed; a higher one
at once. Protection: TMax > T_HIGH -> fan +1 level, >= T_PROTECT1 -> fan +2 and clock -30 MHz, >= T_PROTECT2 ->
fan +3, back to the chosen level at TMax <= T_LOW.
During a Takt-Tabellen run and while a manual clock (Drosselung) is set no clock is set by the automation;
the fan holds TMax around 80 degC (levels 1-10): +1 level above T_CAL_UP, -1 level at T_CAL_DOWN or below,
at least CAL_FAN_GAP_S between two changes. Afterwards back to the chosen level (lower: clock first).

Live only (MINERSENTINEL_ENV != Test). The AC Infinity client is the backend's (backend/api/acinfinity.py,
mounted read-only), so both use the same stored session. The data-service writes only fan_automation.state.
"""
import importlib.util
import json
import logging
import math
import os
import statistics
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

import calibration

logger = logging.getLogger(__name__)

RUN_S = 5                    # one round per central reading (every 5 s; user decision 21.10.)
T_LOW, T_HIGH = 80.0, 84.0
T_PROTECT1, T_PROTECT2 = 88.0, 90.0
T_CAL_UP, T_CAL_DOWN = 81.0, 78.0   # Takt-Messung: fan +1 above 81 degC, -1 at <= 78 degC (user decision 21.10.)
CAL_FAN_GAP_S = 60           # ... at least 60 s between two fan changes (~half of the effect is visible after 60-90 s)
WAIT_S = 180                 # measured: ~85 % of a temperature change after 3 min
PROTECT_WAIT_S = 60
STEP_MHZ = 10
C_PER_STEP = 1.5             # measured (run 7, 320 -> 280 MHz): ~1.5 degC per 10 MHz
MAX_STEP_MHZ = 30
FLOOR_SHARE = 0.15
TEMP_WINDOW_S = 60           # readings kept; the band uses the median of the last 30 s, protection the latest
FAN_READ_S = 60
FAN_CONFIRM_S = 10           # the controller reports a new level only after > 5 s: read back 10 s after a write
FAN_WRITE_GAP_S = 20
FAN_MIN, FAN_MAX = 1, 10
LEARN_S = 600                # learned value: TMax 10 min quietly within 80-84 degC at one fan level and mode
PROTECT_HOLD_S = 900         # after a protection (fan above the chosen level) the clock is not raised for 15 min
RAISE_DELAY_S = 60           # higher fan level: the learned clock follows 60 s after the fan
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
    """New average clock or None (no change). The clock is raised only WAIT_S after the last clock AND fan change
    (a lower fan level cools less only after 1-3 min)."""
    since = _age(st.get('changed_at'), now)
    since_fan = _age(st.get('fan_set_at'), now)
    if t_fast >= T_PROTECT1 and since >= PROTECT_WAIT_S:
        new = cur - MAX_STEP_MHZ
    elif t > upper and since >= WAIT_S:
        new = cur - min(MAX_STEP_MHZ, STEP_MHZ * math.ceil((t - upper) / C_PER_STEP))
    elif (t < T_LOW and not lowering and since >= WAIT_S and since_fan >= WAIT_S and cur < stock_avg - 0.5
          and _age(st.get('protect_at'), now) >= PROTECT_HOLD_S):
        new = cur + STEP_MHZ
    else:
        return None
    new = min(max(new, stock_avg * FLOOR_SHARE), stock_avg)
    return new if abs(new - cur) >= 1 else None


def _learned(learned, mode, level):
    return ((learned.get(mode) or {}).get(str(level)) or {}).get('mhz')


def _start_clock(learned, mode, level, cur):
    """(clock, text) to start from at a fan level, or (None, None) to keep the current clock.
    Learned value of the level; a lower fan level never starts above any learned value of a higher level,
    a higher level never below any learned value of a lower level (also when its own value says otherwise)."""
    vals = {int(k): v.get('mhz') for k, v in (learned.get(mode) or {}).items() if v.get('mhz')}
    higher = [mhz for lv, mhz in vals.items() if lv > level]
    lower = [mhz for lv, mhz in vals.items() if lv < level]
    hi = min(higher) if higher else None
    lo = max(lower) if lower else None
    own = vals.get(level)
    if own is not None:
        target = min(own, hi) if hi is not None else own
        target = max(target, lo) if lo is not None else target
        return target, 'Lernwert Stufe %d' % level
    if hi is not None and cur > hi + 0.5:
        return hi, 'Stufe %d: höchstens Lernwert der höheren Stufe' % level
    if lo is not None and cur < lo - 0.5:
        return lo, 'Stufe %d: mindestens Lernwert der niedrigeren Stufe' % level
    return None, None


def _learn(st, now, mode, t, cur, quiet, stock_avg=None):
    """Remember the clock at which TMax stayed LEARN_S within T_LOW..T_HIGH at this fan level and mode.
    Stored is the clock the automation SET (it is sent again when the value is used; the firmware drifts the
    reported clock upwards). Only if the clocks were not set by the automation (e.g. right after a run):
    the clock the miner reports."""
    key = [mode, st.get('base')]
    if not quiet or t is None or cur is None or not T_LOW <= t <= T_HIGH:
        st.pop('band_since', None)
        return
    if st.get('band_key') != key or not st.get('band_since'):
        st['band_key'], st['band_since'] = key, _iso(now)
        return
    if _age(st['band_since'], now) < LEARN_S:
        return
    if st.get('clock_own'):
        mhz, source = (st.get('clock_set') or stock_avg or cur), 'gesetzt'
    else:
        mhz, source = cur, 'laut Miner'
    entry = {'mhz': round(mhz, 1), 'at': _iso(now), 'source': source}
    learned = st.setdefault('learned', {}).setdefault(mode, {})
    prev = learned.get(str(st['base']))
    learned[str(st['base'])] = entry
    if st.get('band_noted') != st['band_since']:
        st['band_noted'] = st['band_since']
        _note(st, now, 'Lernwert gespeichert: Stufe %s → %.0f MHz (%s)%s.' % (
            st['base'], mhz, source, '' if not prev else ', vorher %.0f MHz' % prev['mhz']))


def _boost(st, t):
    """Fan protection above the chosen level (user decision 21.10.): > 84 degC +1, >= 88 +2, >= 90 +3 (latest
    reading); kept until TMax is back at T_LOW (80 degC) or below, so the fan does not swing at the 84 degC line."""
    boost = int(st.get('boost') or 0)
    if t >= T_PROTECT2:
        return max(boost, 3)
    if t >= T_PROTECT1:
        return max(boost, 2)
    if t > T_HIGH:
        return max(boost, 1)
    if t <= T_LOW:
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
        st = {'log': st.get('log') or [], 'learned': st.get('learned') or {}}   # learned values stay
        _note(st, now, 'Automatik ein (Stufe %d).' % row['level'])

    m = calibration.read_miner(ip, port)     # the central reading taken just before (fallback: own request)
    st['seen_at'] = _iso(now)
    rebooted = st.get('elapsed') is not None and m['elapsed'] is not None and m['elapsed'] < st['elapsed']
    if st.get('mode_key') not in (None, m['mode_key']) or rebooted:
        if st.get('clock_set'):
            _note(st, now, 'Neustart/Moduswechsel: Firmware-Takt, Regelung beginnt neu.')
        st['clock_set'] = None
        st['clock_own'] = False                     # firmware clocks now, not set by the automation
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
    # a manual clock (Drosselung) overrides the automation's clock control until it is lifted
    throttled = bool((((row['connection_config'] or {}).get('throttle') or {}).get('active')))
    st['throttled'] = throttled
    stock = _stock(row, m['mode_key'])
    if stock:
        st['stock_points'] = stock
        st['stock_mhz'] = round(sum(stock) / len(stock), 1)

    if cal or throttled:
        # Takt-Tabellen or manual clock: no clocks; the fan holds ~80 degC (T_CAL_DOWN..T_CAL_UP), levels 1-10
        why = 'Takt-Messung' if cal else 'Manueller Takt'
        st['phase'] = 'messung' if cal else 'manuell'
        fan = int(st.get('cal_fan') or st.get('fan_set') or st['base'])
        since_fan = _age(st.get('cal_fan_at'), now)
        new = fan
        if t is not None:
            # protection: a higher protection level acts at once (88 degC: +1, 90 degC: +2), still hot
            # after PROTECT_WAIT_S: again
            prot = 2 if t_fast >= T_PROTECT2 else 1 if t_fast >= T_PROTECT1 else 0
            last_prot = int(st.get('cal_protect') or 0)
            st['cal_protect'] = prot
            if prot > last_prot:
                new = fan + prot - last_prot
            elif prot and since_fan >= PROTECT_WAIT_S:
                new = fan + prot
            elif t > T_CAL_UP and since_fan >= CAL_FAN_GAP_S:
                new = fan + 1
            elif t <= T_CAL_DOWN and since_fan >= CAL_FAN_GAP_S:
                new = fan - 1
        new = min(max(new, FAN_MIN), FAN_MAX)
        if new != fan:
            st['cal_fan_at'] = _iso(now)
            _note(st, now, '%s: TMax %.0f °C → Lüfter Stufe %d.' % (why, t, new))
        st['cal_fan'] = new
        target_fan = new
    else:
        if st.pop('cal_fan', None) is not None:
            st['base'] = max(int(st.get('fan_set') or st['base']), st['base'])
            st.pop('cal_fan_at', None)
            st.pop('cal_protect', None)
            st['exit_learned'] = True               # take the learned clock of the chosen level at once
            st['clock_own'] = False                 # the clocks of the run / manual throttle are in effect
            _note(st, now, 'Lüfter-Regelung beendet: zurück zur gewählten Stufe %d.' % row['level'])
        chosen = int(row['level'])
        mode = m['mode_key']
        learned = st.setdefault('learned', {})
        cur = sum(m['points']) / len(m['points']) if m['points'] else None
        stock_avg = st['stock_mhz'] if stock else None
        can_clock = bool(stock) and cur is not None

        def set_clock(new, text):
            new = min(max(new, stock_avg * FLOOR_SHARE), stock_avg)
            calibration.set_frequency(ip, port, _points_for(stock, new))
            st['clock_set'] = round(new, 1) if new < stock_avg - 0.5 else None
            st['clock_own'] = True
            st['changed_at'] = _iso(now)
            _note(st, now, text % new)

        # a click on a level (also the same level again): its learned clock is set at once, in both
        # directions (user decision 21.10.); a lower fan level still follows after WAIT_S
        clicked = False
        level_at = row.get('level_at')
        if level_at is not None and str(level_at) != st.get('level_seen'):
            st['level_seen'] = str(level_at)
            if can_clock:
                ref = (st.get('clock_set') or stock_avg) if st.get('clock_own') else cur
                target, why = _start_clock(learned, mode, chosen, ref)
                if target is not None:
                    clicked = True
                    for key in ('raise_to', 'raise_why', 'exit_learned'):
                        st.pop(key, None)
                    if abs(target - ref) >= 0.5:
                        set_clock(target, why + ': Takt %.0f MHz (Stufe gewählt).')

        if chosen > st['base']:
            st['base'] = chosen                         # higher level: the fan at once ...
            for key in ('lower_since', 'lower_learned'):
                st.pop(key, None)
            target, why = _start_clock(learned, mode, chosen, cur) if can_clock and not clicked else (None, None)
            if target and target > cur + 0.5:
                st['raise_to'], st['raise_from'] = target, _iso(now)   # ... the start clock RAISE_DELAY_S later
                st['raise_why'] = why
            elif target and target < cur - 0.5:
                set_clock(target, why + ': Takt %.0f MHz.')
        lowering = chosen < st['base']
        if lowering and not st.get('lower_since'):
            st['lower_since'] = _iso(now)
            st.pop('raise_to', None)
            if clicked:
                st['lower_learned'] = True              # clock already at the start clock: the fan after WAIT_S
            target, why = _start_clock(learned, mode, chosen, cur) if can_clock and not clicked else (None, None)
            if target and target < cur - 0.5:
                # start clock of the lower level: clock straight to it, the fan follows after WAIT_S
                set_clock(target, why + ': Takt %.0f MHz, Lüfter folgt nach 3 min.')
                st['lower_learned'] = True
        st['phase'] = 'senken' if lowering else 'normal'
        # after a run / manual clock: straight to the learned clock of the chosen level (lower: at once,
        # higher: RAISE_DELAY_S after the fan); while lowering the fan this waits until the fan is down
        if st.get('exit_learned') and not lowering:
            st.pop('exit_learned', None)
            target, why = _start_clock(learned, mode, chosen, cur) if can_clock else (None, None)
            if target and target < cur - 0.5:
                set_clock(target, why + ': Takt %.0f MHz.')
            elif target and target > cur + 0.5:
                st['raise_to'], st['raise_from'], st['raise_why'] = target, _iso(now), why
        if lowering and not stock and _age(st.get('lower_since'), now) >= WAIT_S:
            st['base'] = chosen                         # no clock step possible: the fan after WAIT_S
            lowering = False
        if not lowering:
            for key in ('lower_since', 'lower_learned'):
                st.pop(key, None)
        if t is not None:
            prev_boost = int(st.get('boost') or 0)
            st['boost'] = _boost(st, t_fast)
            # the firmware drifts a set clock up by ~10 MHz within minutes: regulate from the clock SET last
            # (steps then add up), not from the reported one (21.10.: -10 MHz steps were eaten by the drift)
            ref = (st.get('clock_set') or stock_avg) if st.get('clock_own') and stock_avg else cur
            if can_clock and prev_boost == 0 and st['boost'] > 0:
                # protection needed: the clock is too high for this fan level -> lower it for good
                st['protect_at'] = _iso(now)
                step = min(MAX_STEP_MHZ, STEP_MHZ * math.ceil((t - T_HIGH) / C_PER_STEP)) if t > T_HIGH else 0
                new = max(ref - step - STEP_MHZ, stock_avg * FLOOR_SHARE)     # the usual step plus 10 MHz
                set_clock(new, 'Schutz bei TMax %.1f °C: Takt dauerhaft auf %%.0f MHz für Stufe %d.' % (t_fast, st['base']))
                lv = learned.setdefault(mode, {}).get(str(st['base']))
                if not lv or lv.get('mhz', 1e9) > new:
                    learned[mode][str(st['base'])] = {'mhz': round(new, 1), 'at': _iso(now), 'source': 'Schutz'}
                ref = new
            if can_clock:
                raise_to = st.get('raise_to')
                if raise_to and t > T_HIGH:
                    st.pop('raise_to', None)            # too warm already: normal regulation
                    raise_to = None
                if raise_to:
                    if _age(st.get('raise_from'), now) >= RAISE_DELAY_S:
                        st.pop('raise_to', None)
                        set_clock(raise_to, (st.pop('raise_why', None) or 'Lernwert Stufe %d' % st['base'])
                                  + ': Takt %.0f MHz.')
                else:
                    upper = T_LOW if lowering and not st.get('lower_learned') else T_HIGH
                    new = _clock_step(st, now, t, t_fast, ref, stock_avg, upper, lowering)
                    if new is not None:
                        set_clock(new, 'TMax %.1f °C → Takt %%.0f MHz.' % t)
                if lowering:
                    floor = st.get('clock_set') and st['clock_set'] <= stock_avg * FLOOR_SHARE + 0.5
                    settled = _age(st.get('changed_at'), now) >= WAIT_S
                    if settled and (t <= T_LOW or floor or st.get('lower_learned')):
                        _note(st, now, 'Temperatur %.1f °C, 3 min abgewartet → Lüfter auf Stufe %d.' % (t, chosen))
                        st['base'] = chosen
                        st['phase'] = 'normal'
                        lowering = False
                        for key in ('lower_since', 'lower_learned'):
                            st.pop(key, None)
                        st['exit_learned'] = True       # next round: start clock of the new level if still needed
        quiet = not lowering and not st.get('raise_to') and not int(st.get('boost') or 0)
        _learn(st, now, mode, t, cur, quiet, stock_avg)
        target_fan = min(st['base'] + int(st.get('boost') or 0), FAN_MAX)

    # --- fan: write the target level (at most every FAN_WRITE_GAP_S) ---
    # a different read level counts only if it was read at least FAN_CONFIRM_S after the last write
    read_after_write = _age(st.get('fan_read_at'), now) <= _age(st.get('fan_set_at'), now) - FAN_CONFIRM_S
    differs = target_fan != st.get('fan_set') or (st.get('fan_read') is not None and st['fan_read'] != target_fan
                                                   and read_after_write)
    if differs and _age(st.get('fan_set_at'), now) >= FAN_WRITE_GAP_S:
        try:
            ac.set_fan_level(dev_id, fport, target_fan)
            if target_fan != st.get('fan_set'):
                _note(st, now, 'Lüfter Stufe %d gesetzt.' % target_fan)
            st['fan_set'] = target_fan
            st['fan_set_at'] = _iso(now)
            # read back FAN_CONFIRM_S after the write (earlier the controller still reports the old level)
            st['fan_read_at'] = _iso(now - timedelta(seconds=FAN_READ_S - FAN_CONFIRM_S))
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
                "SELECT f.id, f.enabled, f.ac_dev_id, f.ac_port, f.level, f.level_at, f.state, d.ip_address, d.port, "
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
