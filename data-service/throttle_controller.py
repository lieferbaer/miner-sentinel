"""Throttle controller for Avalon miners (cgminer TCP API).

Runs every 15 s from the data-service scheduler. For every active cgminer
device it
  * reads the work mode the miner reports (WORKMODE/WORKLEVEL; for the A15 the
    power level from its own web interface), the PLL clocks (SFx), the uptime
    and the miner's own measured speed (GHSspd, the value its display shows),
  * keeps exactly one profile per work mode: stock clocks and the measured
    full-power hashrate/power (mean of the last valid 12-min measurements),
  * detects when a throttle set from the web UI was lifted on the miner
    (reboot, work-mode change, or a clock change we did not make),
  * regulates an active throttle: hashrate is proportional to the clock, so the
    next clock is current clock x target / measured speed (max 20 % per step).

State lives in devices.connection_config -> 'throttle'. The backend bumps
'rev' whenever the user changes something; this controller only writes when
'rev' is unchanged (optimistic lock), so user actions always win.
"""
import json
import logging
import re
import socket
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

import calibration
import miner_cache

logger = logging.getLogger(__name__)

PENDING_SECONDS = 60              # clock ramp after every change
CLEAN_AFTER = 145                 # GHSspd covers the last ~130 s (+15 s poll interval): a refresh
                                  # seen this long after a clock change shows only the new clock
STABLE_SECONDS = 15 * 60          # a measurement needs 15 min unthrottled at unchanged clocks
MEASURE_WINDOW_MIN = 12           # ... and averages the last 12 of them
REMEASURE_SECONDS = 10 * 60
DEADBAND = 0.02                   # target reached within +-2 %
REARM = 0.04                      # re-adjust a reached target beyond +-4 %
MAX_STEP = 0.20                   # max clock change per step
EXTERNAL_TOL = 0.08               # clocks this far from what we set = changed on the miner
FLOOR_SHARE = 0.15                # never below 15 % of the stock clock
MEASURE_KEEP = 3                  # stored value = mean of the last 3 valid measurements
MEASURE_OUTLIER = 0.10            # a single measurement >10 % off only counts once confirmed
CAL_KEEP = 12                     # measured (clock, TH/s) points kept per mode
CAL_SPACING = 0.02                # one point per ~2 % of clock; a newer one replaces an older one
CACHE_MAX_AGE = 15                # s: readings of the collector (every 10 s) are used up to this age,
                                  # except while a throttle is regulated (always read fresh)
MANUAL_MEASURE_S = 15 * 60        # A15 manual clock: a measured point is stored after 15 min unchanged

A15_LEVEL_LABELS = {4: 'Power Mode 04', 3: 'Power Mode 03', 2: 'Power Mode 02', 1: 'Power Mode 01', 0: 'Power Mode 00'}
A15_MODES = {1: 'High Performance'}   # WORKMODE 2 = Power Mode with levels 00-04
FAMILY_MODES = {
    'nano': {0: 'Low', 1: 'Medium', 2: 'High'},
    'q': {0: 'Eco', 1: 'Standard', 2: 'Super'},
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _cmd(ip, port, payload, timeout=6):
    with miner_cache.lock(ip), socket.create_connection((ip, port or 4028), timeout) as sock:
        sock.sendall(json.dumps(payload).encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b''.join(chunks).decode('utf-8', 'ignore').replace('\x00', '')


def read_miner(ip, port, fresh=False):
    """fresh: read estats/summary from the miner, not from the collector's readings."""
    age = None if fresh else CACHE_MAX_AGE
    est = miner_cache.fetch(ip, 'estats', age, lambda: _cmd(ip, port, {'command': 'estats'}, 8))
    summ = miner_cache.fetch(ip, 'summary', age, lambda: _cmd(ip, port, {'command': 'summary'}, 6))
    boards = {}
    for name, values in re.findall(r'\b(SF\d+)\[([0-9 ]+)\]', est):
        if name not in boards and values.split():
            boards[name] = [int(v) for v in values.split()]

    def num(key):
        match = re.search(r'\b' + key + r'\[([0-9.]+)', est)
        return float(match.group(1)) if match else None

    try:
        summary = json.loads(summ).get('SUMMARY', [{}])[0]
    except ValueError:
        summary = {}
    ghsspd = num('GHSspd')
    return {
        'ghsspd_ths': ghsspd / 1000.0 if ghsspd else None,
        'points': boards[sorted(boards)[0]] if boards else None,
        'workmode': num('WORKMODE'),
        'worklevel': num('WORKLEVEL'),
        'elapsed': summary.get('Elapsed'),
        'mhs5': summary.get('MHS 5m'),
    }


def _a15_conf(ip):
    """Pool/mode/power-level settings from the A15 web interface (readable without login)."""
    import urllib.request
    with miner_cache.lock(ip):
        raw = urllib.request.urlopen('http://%s/updatecgconf.cgi' % ip, timeout=5).read().decode('utf-8', 'replace')
    match = re.search(r'CGConfCallback\((\{.*?\})\);', raw, re.S)
    return json.loads(match.group(1)) if match else None


def family_of(dev):
    text = ' '.join(str(dev.get(k) or '') for k in ('device_name', 'model', 'device_id')).lower()
    if 'nano' in text:
        return 'nano'
    if 'a15' in text or '15-' in text:
        return 'a15'
    if re.search(r'(^|\W)q(\W|$)', text) or 'avalonq' in text:
        return 'q'
    return None


def mode_key_and_label(m, family):
    """Work mode as the miner reports it (WORKMODE): key for the profile, name for the UI.
    A15: WORKMODE 2 is Power Mode, its level (00-04) comes from the web interface."""
    if m['workmode'] is None:
        return None, 'unbekannt'
    wm = int(m['workmode'])
    if family == 'a15':
        if wm == 2:
            level = m['a15_level']
            return '2/L%d' % level, A15_LEVEL_LABELS.get(level, 'Level %02d' % level)
        return '%d/-' % wm, A15_MODES.get(wm, 'Mode %d' % wm)
    wl = '-' if m['worklevel'] is None else str(int(m['worklevel']))
    return '%d/%s' % (wm, wl), (FAMILY_MODES.get(family) or {}).get(wm, 'Mode %d' % wm)


def mode_option(m, family):
    """Value of the active mode button: A15 power level (Power Mode only), else WORKMODE."""
    if m['workmode'] is None:
        return None
    if family == 'a15':
        return m['a15_level'] if int(m['workmode']) == 2 else None
    return int(m['workmode'])


def avg(points):
    return sum(points) / len(points)


def close(a, b, tol):
    return abs(avg(a) - avg(b)) <= avg(b) * tol


def set_frequency(ip, port, points):
    with miner_cache.command(ip):
        reply = _cmd(ip, port, {'command': 'ascset',
                                'parameter': '0,frequency,%s-0-0' % ':'.join(str(p) for p in points)}, 8)
    if 'set OK' not in reply:
        raise RuntimeError('miner rejected frequency: ' + reply[:200])
    return points


def _measured_full_power(conn, device_id):
    """(TH/s, W) averaged over the last MEASURE_WINDOW_MIN minutes, or (None, None)."""
    window = str(MEASURE_WINDOW_MIN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT AVG(hashrate_ghs), COUNT(*) FROM device_mining_stats "
            "WHERE device_id=%s AND recorded_at > NOW() - (%s || ' minutes')::interval AND hashrate_ghs > 0",
            (device_id, window))
        value, count = cur.fetchone()
        cur.execute(
            "SELECT AVG(power_watts), COUNT(*) FROM device_hardware_stats "
            "WHERE device_id=%s AND recorded_at > NOW() - (%s || ' minutes')::interval AND power_watts > 0",
            (device_id, window))
        watts, wcount = cur.fetchone()
    ths = (float(value) / 1000.0) if value and count >= 30 else None
    return ths, (float(watts) if watts and wcount >= 30 else None)


def _record_measurement(prof, value, watts, now):
    """Keep the stored full-power value stable (mean of the last valid measurements);
    a single outlier only replaces it after a second measurement confirms it."""
    new = {'ths': round(value, 2), 'w': round(watts) if watts else None, 'at': _iso(now)}
    prof['measure_try_at'] = _iso(now)
    hist = prof.get('measurements') or []
    ref = prof.get('measured_max_ths')
    if not hist and ref:
        hist = [{'ths': ref, 'w': prof.get('measured_power_w'), 'at': prof.get('measured_at')}]
    if ref and abs(value - ref) / ref > MEASURE_OUTLIER:
        pend = prof.get('pending_measure')
        if pend and abs(value - pend['ths']) / pend['ths'] <= MEASURE_OUTLIER:
            hist = [pend, new]
            prof.pop('pending_measure', None)
        else:
            prof['pending_measure'] = new
            prof['measurements'] = hist
            return
    else:
        hist = (hist + [new])[-MEASURE_KEEP:]
        prof.pop('pending_measure', None)
    prof['measurements'] = hist
    ths = sum(h['ths'] for h in hist) / len(hist)
    ws = [h['w'] for h in hist if h.get('w')]
    prof['measured_max_ths'] = round(ths, 2)
    prof['measured_at'] = _iso(now)
    if ws:
        w = sum(ws) / len(ws)
        prof['measured_power_w'] = round(w)
        prof['efficiency_j_th'] = round(w / ths, 1)


def _record_point(prof, mhz, ths):
    """Remember the measured hashrate at this average clock (reached throttle) for this mode.
    The backend starts the next throttle from the measured point closest to its target."""
    cal = [p for p in prof.get('cal') or [] if abs(p[0] - mhz) > mhz * CAL_SPACING]
    prof['cal'] = (cal + [[round(mhz, 1), round(ths, 2)]])[-CAL_KEEP:]


def next_clock(points, target, x_now):
    """Average clock for `target` TH/s from measured (clock, TH/s) points of this mode.
    Between the nearest measured points below and above the target: straight line between them.
    Otherwise along the line through the two measured points closest to the target; with a single
    point (or no rising line) proportional from the point closest to the target."""
    pts = sorted({round(x, 1): y for x, y in points if x > 0 and y > 0}.items())
    below = [p for p in pts if p[1] < target]
    above = [p for p in pts if p[1] > target]
    if below and above:
        (x1, y1), (x2, y2) = max(below, key=lambda p: p[1]), min(above, key=lambda p: p[1])
    else:
        near = sorted(pts, key=lambda p: abs(p[1] - target))[:2]
        if len(near) < 2:
            return near[0][0] * target / near[0][1] if near else x_now
        (x1, y1), (x2, y2) = near
    if (x2 - x1) * (y2 - y1) <= 0 or y1 == y2:      # no rising line: proportional from the closest
        x0, y0 = min(pts, key=lambda p: abs(p[1] - target))
        return x0 * target / y0
    return x1 + (target - y1) * (x2 - x1) / (y2 - y1)


def _manual_point(conn, dev, active, m, mkey, now):
    speed = m['ghsspd_ths']
    fresh = speed is not None and active.get('last_speed') is not None and abs(speed - active['last_speed']) > 1e-6
    active['last_speed'] = speed
    if active.get('manual_measured_at') or active.get('pending_until') or not fresh:
        return
    since = (now - (_parse(active.get('last_change_at')) or now)).total_seconds()
    if since < MANUAL_MEASURE_S:
        return
    full = calibration.read_miner(dev['ip_address'], dev['port'])
    if not full['ghsspd'] or 'Over Hot' in full['status'] or full['mode_key'] != mkey:
        return
    calibration.save_manual_point(conn, dev['id'], full, mkey, active['applied_points'])
    active['manual_measured_at'] = _iso(now)
    logger.info('Throttle device %s: manual clock %.1f MHz measured', dev['id'], active['clock_mhz'])


def control_device(conn, dev):
    cfg = dev['connection_config'] or {}
    st = json.loads(json.dumps(cfg.get('throttle') or {}))
    rev = int(st.get('rev', 0) or 0)
    family = family_of(dev)
    # regulating a throttle: values as fresh as before; otherwise (also A15 manual clock) the
    # collector's readings suffice
    regulating = st.get('active') and st['active'].get('clock_mhz') is None and not dev.get('calibrating')
    age = None if regulating else CACHE_MAX_AGE
    m = read_miner(dev['ip_address'], dev['port'], fresh=age is None)
    if not m['points']:
        return
    if family == 'a15' and m['workmode'] is not None and int(m['workmode']) == 2:
        # raises OSError if the miner does not answer
        conf = miner_cache.fetch(dev['ip_address'], 'web', age, lambda: _a15_conf(dev['ip_address']))
        if not conf or conf.get('mode') != 2:
            return                                  # level unknown this round: do nothing
        m['a15_level'] = conf['power_level']
    if 'freq_ok' not in st:
        st['freq_ok'] = 'frequency' in _cmd(dev['ip_address'], dev['port'],
                                            {'command': 'ascset', 'parameter': '0,help'}, 6)
    mkey, label = mode_key_and_label(m, family)
    if mkey is None:
        return
    now = _now()
    points = m['points']
    profiles = st.setdefault('profiles', {})
    last = st.get('last_seen') or {}
    active = st.get('active')

    rebooted = (last.get('elapsed') is not None and m['elapsed'] is not None
                and m['elapsed'] < last['elapsed'])
    mode_changed = last.get('mode_key') not in (None, mkey)
    observe = bool(dev.get('calibrating'))   # Takt-Messung läuft: nur lesen, nichts regeln

    # 1) was an active throttle lifted on the miner?
    if active and not observe:
        applied = active['applied_points']
        pending_until = _parse(active.get('pending_until'))
        pending = pending_until is not None and now < pending_until
        event = None
        if rebooted:
            event = ('reboot', 'Neustart des Miners')
        elif mode_changed:
            event = ('mode_change', 'Moduswechsel %s → %s' % (last.get('mode_label', '?'), label))
        elif pending:
            pass
        elif pending_until is not None:
            active['applied_points'] = points       # ramp finished: adopt the clocks the firmware settled on
            active.pop('pending_until', None)
        elif not close(points, applied, EXTERNAL_TOL):
            event = ('external_change', 'Takt am Miner geändert')
        if event:
            st['active'] = None
            st['last_event'] = {'type': event[0], 'detail': event[1], 'at': _iso(now),
                                'target_ths': active.get('target_ths')}
            active = None
            logger.info('Throttle on device %s lifted: %s', dev['id'], event[1])

    # 2) unthrottled: the clocks the miner runs are the stock clocks of this mode; measure full power
    if observe:
        st['profile'] = mkey
    elif not active:
        prof = profiles.setdefault(mkey, {'mode_key': mkey})
        prof['mode_label'] = label
        steady = last.get('points') == points and not mode_changed and not rebooted
        if steady:
            prof['points'] = points
            prof['avg_mhz'] = round(avg(points), 1)
        st['profile'] = mkey
        if not steady or last.get('throttled') or not st.get('stable_since'):
            st['stable_since'] = _iso(now)
        stable_for = (now - (_parse(st.get('stable_since')) or now)).total_seconds()
        tried = _parse(prof.get('measure_try_at')) or _parse(prof.get('measured_at'))
        if (family != 'a15'               # A15: measured values only from the Takt-Tabellen
                and stable_for >= STABLE_SECONDS
                and (tried is None or (now - tried).total_seconds() >= REMEASURE_SECONDS)):
            value, watts = _measured_full_power(conn, dev['id'])
            if value:
                _record_measurement(prof, value, watts, now)

    # 3a) A15 manual clock: nothing is regulated; after MANUAL_MEASURE_S unchanged the next fresh
    #     GHSspd is stored as a measured point (Takt-Tabellen, kind 'manual'), once per setting
    elif active.get('clock_mhz') is not None:
        _manual_point(conn, dev, active, m, mkey, now)

    # 3) active: proportional regulation on the miner's own measured speed (GHSspd).
    #    GHSspd refreshes about every 130 s and averages that period, so only a refresh seen at
    #    least CLEAN_AFTER after the last clock change is used (and recorded as a measured point).
    #    Hashrate does not follow the clock 1:1, so the next clock comes from the straight line
    #    between measured points (next_clock), not from a rule of three.
    elif not active.get('pending_until') and m['ghsspd_ths']:
        prof = profiles.get(active.get('profile')) or {}
        last_change = _parse(active.get('last_change_at')) or now
        since_change = (now - last_change).total_seconds()
        speed = m['ghsspd_ths']
        fresh = active.get('last_speed') is not None and abs(speed - active['last_speed']) > 1e-6
        active['last_speed'] = speed
        if fresh:
            active['estimate_ths'] = round(speed, 2)
        clean = fresh and since_change >= CLEAN_AFTER
        if clean:
            active['speed_updates'] = int(active.get('speed_updates', 0)) + 1
        if prof.get('points') and clean:
            active['measured_ths'] = round(m['mhs5'] / 1e6, 2) if m['mhs5'] else None
            active['measured_at'] = _iso(now)
            x_now = avg(active['applied_points'])
            meas = [p for p in active.get('meas') or [] if abs(p[0] - x_now) > 0.5]
            active['meas'] = (meas + [[round(x_now, 1), round(speed, 2)]])[-8:]
            err = active['target_ths'] / speed - 1
            band = DEADBAND if active.get('status') != 'reached' else REARM
            if abs(err) <= band:
                if active.get('status') != 'reached':
                    active['reached_at'] = _iso(now)
                active['status'] = 'reached'
                if family != 'a15':           # A15: measured values only from the Takt-Tabellen
                    _record_point(prof, x_now, speed)
            else:
                x_new = next_clock(active['meas'] + (prof.get('cal') or []), active['target_ths'], x_now)
                f = min(max(x_new / x_now, 1 - MAX_STEP), 1 + MAX_STEP)
                new = [min(max(int(round(p * f)), int(s0 * FLOOR_SHARE)), s0)
                       for p, s0 in zip(active['applied_points'], prof['points'])]
                if close(new, active['applied_points'], 0.002):
                    active['status'] = 'limit'
                else:
                    set_frequency(dev['ip_address'], dev['port'], new)
                    active['applied_points'] = new
                    active['last_change_at'] = _iso(_now())
                    active['pending_until'] = _iso(_now() + timedelta(seconds=PENDING_SECONDS))
                    active['speed_updates'] = 0
                    active['last_speed'] = None
                    active['status'] = 'adjusting'
                    active['steps'] = int(active.get('steps', 0)) + 1
                    logger.info('Throttle device %s: target %.2f, speed %.2f -> clock %s',
                                dev['id'], active['target_ths'], speed, new)

    st['last_seen'] = {'elapsed': m['elapsed'], 'mode_key': mkey, 'mode_label': label,
                       'option': mode_option(m, family), 'ghsspd_ths': m['ghsspd_ths'],
                       'throttled': bool(st.get('active')), 'points': points, 'at': _iso(now)}
    st['rev'] = rev
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devices SET connection_config = jsonb_set(COALESCE(connection_config, '{}'::jsonb), "
            "'{throttle}', %s::jsonb) WHERE id = %s "
            "AND COALESCE((connection_config->'throttle'->>'rev')::int, 0) = %s",
            (json.dumps(st), dev['id'], rev))
        if cur.rowcount == 0:
            conn.rollback()        # the user changed something meanwhile: drop this round (incl. a manual point)
            return
    conn.commit()


def run_throttle_controller(database_url):
    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.Error as exc:
        logger.warning('Throttle controller: no database: %s', exc)
        return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, ip_address, port, connection_config, name AS device_name, model, device_id "
                        ", EXISTS (SELECT 1 FROM "
                        "calibration_runs r WHERE r.device_id = devices.id AND r.status IN "
                        "('running', 'paused', 'cancelling', 'finishing')) AS calibrating "
                        "FROM devices WHERE is_active AND protocol = 'cgminer_tcp'")
            devices = cur.fetchall()
        for dev in devices:
            try:
                control_device(conn, dev)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
                conn.rollback()
                logger.debug('Throttle controller skipped device %s: %s', dev['id'], exc)
    finally:
        conn.close()
