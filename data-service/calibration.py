"""Takt-Tabellen (A15): measurement runs started from the web UI.

kind 'max'  : for every selected work mode, switch to it and take two clean
              measurements at its stock clocks.
kind 'step' : one work mode, from its stock clock downwards in 10 MHz steps to
              15 % of the stock clock, one clean measurement per step.

A measurement is "clean" when the miner refreshed GHSspd at least CLEAN_AFTER
after the last clock/mode change (GHSspd covers the last ~130 s). Every value
the miner reports is stored with each point. If the miner reports "Over Hot",
the run pauses and the current point is measured again after "resume".

State lives in calibration_runs (status, mode_index, step_index, state JSON);
points in calibration_points. Runs every 15 s from the data-service scheduler.
"""
import http.client
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

import miner_cache

logger = logging.getLogger(__name__)

CLEAN_AFTER = 145          # s after a change before a GHSspd refresh counts
STEP_MHZ = 10              # step of the measuring series (average clock)
FLOOR_SHARE = 0.15         # lowest clock: 15 % of the stock clock
MAX_MEASUREMENTS = 2       # clean measurements per mode in a max run
SWITCH_TIMEOUT = 300       # s to wait for the miner to report the new mode
STABLE_S = 180             # a measurement counts only if the power was stable this long ...
STABLE_BAND = 0.01         # ... within +-1 % (steady state, not warm-up)
CLOCK_STABLE_S = 120       # stock clocks of a mode are taken once they stayed unchanged this long
MAX_WAIT = 300             # max run: each measurement waits at least this long after the mode switch
                           # or after the previous measurement
RESEND_S = 20              # a mode switch not confirmed after this long is sent again (the A15 often drops
                           # the connection of the switch command without applying it)
CACHE_MAX_AGE = 15        # s: readings of the collector (every 10 s) are used up to this age, except
                           # while the clocks of a point settle or a point is measured (always read fresh)
A15_LEVEL_LABELS = {4: 'Power Mode 04', 3: 'Power Mode 03', 2: 'Power Mode 02', 1: 'Power Mode 01', 0: 'Power Mode 00'}
A15_MODE_LABELS = {1: 'High Performance'}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _cmd(ip, port, payload, timeout=8):
    with miner_cache.lock(ip), socket.create_connection((ip, port or 4028), timeout) as sock:
        sock.sendall(payload.encode())
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b''.join(chunks).decode('utf-8', 'ignore').replace('\x00', '')


def _retry(label, fn, *args, **kwargs):
    """Read request with one retry after 2 s (short connection drops); the error names the request."""
    for attempt in (1, 2):
        try:
            return fn(*args, **kwargs)
        except (OSError, http.client.HTTPException) as exc:
            if attempt == 2:
                raise RuntimeError('%s: %s' % (label, exc))
            time.sleep(2)


def a15_conf(ip):
    with miner_cache.lock(ip):
        raw = urllib.request.urlopen('http://%s/updatecgconf.cgi' % ip, timeout=5).read().decode('utf-8', 'replace')
    match = re.search(r'CGConfCallback\((\{.*?\})\);', raw, re.S)
    return json.loads(match.group(1)) if match else None


def a15_set_mode(ip, conf, mode, level):
    """Same POST as the Save button of the A15 configuration page."""
    fields = {k: conf.get(k, '') for k in ('pool1', 'worker1', 'passwd1', 'pool2', 'worker2', 'passwd2',
                                           'pool3', 'worker3', 'passwd3')}
    fields['mode'] = mode
    fields['power_level'] = '%02d' % (level if level is not None else int(conf.get('power_level') or 0))
    req = urllib.request.Request('http://%s/cgconf.cgi' % ip, data=urllib.parse.urlencode(fields).encode(),
                                 method='POST')
    try:
        with miner_cache.command(ip):
            urllib.request.urlopen(req, timeout=10).read()
    except (ConnectionResetError, http.client.HTTPException) as exc:
        # the A15 sometimes drops the connection of the switch command (then it is usually NOT applied);
        # wait_mode reads the mode back and sends the command again after RESEND_S
        logger.info('A15 %s: Umschaltbefehl – Verbindung vom A15 getrennt (%s), wird geprüft und wiederholt', ip, exc)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionResetError):
            logger.info('A15 %s: Umschaltbefehl – Verbindung vom A15 getrennt (%s), wird geprüft und wiederholt',
                        ip, exc)
        else:
            raise RuntimeError('A15-Umschaltbefehl: %s' % exc)
    except OSError as exc:
        raise RuntimeError('A15-Umschaltbefehl: %s' % exc)


def set_frequency(ip, port, points):
    with miner_cache.command(ip):
        reply = _cmd(ip, port, json.dumps({'command': 'ascset',
                                           'parameter': '0,frequency,%s-0-0' % ':'.join(str(p) for p in points)}))
    if 'set OK' not in reply:
        raise RuntimeError('miner rejected frequency: ' + reply[:200])


def mode_key(mode, level):
    return '2/L%d' % level if mode == 2 else '%d/-' % mode


def mode_label(key):
    if key.startswith('2/L'):
        return A15_LEVEL_LABELS.get(int(key[3:]), 'Level ' + key[3:])
    return A15_MODE_LABELS.get(int(key.split('/')[0]), 'Mode ' + key.split('/')[0])


def parse_key(key):
    """'2/L4' -> (2, 4), '1/-' -> (1, None)"""
    mode = int(key.split('/')[0])
    return mode, (int(key[3:]) if key.startswith('2/L') else None)


def _fields(estats):
    """Every KEY[value] of the miner statistics; long number lists as min/avg/max."""
    out = {}
    for key, value in re.findall(r'(\w+)\[([^\]]*)\]', estats):
        if key in out:
            continue
        parts = value.split()
        if len(parts) > 4:
            try:
                nums = [float(p) for p in parts]
                out[key] = {'min': min(nums), 'avg': round(sum(nums) / len(nums), 2), 'max': max(nums),
                            'n': len(nums)}
                continue
            except ValueError:
                pass
        out[key] = value.strip()
    return out


def _num(text):
    match = re.match(r'\s*(-?[0-9.]+)', str(text or ''))
    return float(match.group(1)) if match else None


def read_miner(ip, port, fresh=False):
    """fresh: read estats/summary from the miner, not from the collector's readings."""
    age = None if fresh else CACHE_MAX_AGE
    estats = miner_cache.fetch(ip, 'estats', age, lambda: _retry(
        'cgminer-API estats', _cmd, ip, port, json.dumps({'command': 'estats'})))
    summ = miner_cache.fetch(ip, 'summary', age, lambda: _retry(
        'cgminer-API summary', _cmd, ip, port, json.dumps({'command': 'summary'}), 6))
    try:
        summary = json.loads(summ).get('SUMMARY', [{}])[0]
    except ValueError:
        summary = {}
    fields = _fields(estats)
    points = [int(v) for v in str(fields.get('SF0') or '').split()] if isinstance(fields.get('SF0'), str) else []
    conf = miner_cache.fetch(ip, 'web', CACHE_MAX_AGE, lambda: _retry('A15-Einstellungsseite lesen', a15_conf, ip))
    wm = _num(fields.get('WORKMODE'))
    key = None
    if wm is not None:
        wm = int(wm)
        if wm == 2:
            if conf and conf.get('mode') == 2:
                key = mode_key(2, int(conf['power_level']))
        else:
            key = mode_key(wm, None)
    ps = str(fields.get('PS') or '').split()
    power = _num(fields.get('WALLPOWER')) or (_num(ps[6]) if len(ps) >= 7 else None)
    return {
        'estats': estats, 'summary': summary, 'fields': fields, 'conf': conf, 'points': points,
        'mode_key': key, 'status': str(fields.get('SYSTEMSTATU') or ''),
        'ghsspd': _num(fields.get('GHSspd')), 'power': power if power and power > 0 else None,
        'dh': _num(fields.get('DH')), 'tmax': _num(fields.get('TMax')),
        'elapsed': summary.get('Elapsed'),
    }


def avg(points):
    return sum(points) / len(points) if points else None


def _save_point(conn, run, m, kind, key, seq, set_points):
    speed = m['ghsspd'] / 1000.0
    conf = {k: v for k, v in (m['conf'] or {}).items() if not k.startswith('passwd')}
    data = {'fields': m['fields'], 'summary': m['summary'], 'web': conf, 'estats_raw': m['estats']}
    with conn.cursor() as cur:
        if kind == 'step':
            cur.execute("DELETE FROM calibration_points WHERE run_id = %s AND seq = %s", (run['id'], seq))
        cur.execute(
            "INSERT INTO calibration_points (device_id, run_id, kind, mode_key, mode_label, seq, set_mhz, set_points, "
            "miner_mhz, miner_points, hashrate_ths, power_w, j_th, dh_pct, chip_temp_c, data, recorded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run['device_id'], run['id'], kind, key, mode_label(key), seq,
             round(avg(set_points), 1) if set_points else None, json.dumps(set_points or []),
             round(avg(m['points']), 1) if m['points'] else None, json.dumps(m['points'] or []),
             round(speed, 3), m['power'], round(m['power'] / speed, 2) if m['power'] and speed else None,
             m['dh'], m['tmax'], json.dumps(data), _now()))


def save_manual_point(conn, device_id, m, key, set_points):
    """Measured point of a manual clock (Drosselung A15), shown in the Takt-Tabellen; no commit."""
    _save_point(conn, {'id': None, 'device_id': device_id}, m, 'manual', key, 0, set_points)


def _power_stable(st, power, now):
    """Track the power readings since the last change; True once they stayed within
    STABLE_BAND for at least STABLE_S."""
    if power is None:
        return False
    hist = [h for h in st.get('pw') or [] if (now - _parse(h[0])).total_seconds() <= STABLE_S + 60]
    hist.append([_iso(now), power])
    st['pw'] = hist
    window = [h[1] for h in hist if (now - _parse(h[0])).total_seconds() <= STABLE_S]
    covered = (now - _parse(hist[0][0])).total_seconds() >= STABLE_S
    mean = sum(window) / len(window)
    # every reading within +-STABLE_BAND of the mean (before: max-min <= 1 %, i.e. only +-0.5 %)
    return covered and max(window) <= mean * (1 + STABLE_BAND) and min(window) >= mean * (1 - STABLE_BAND)


def bounce_key(key):
    """Another work mode to switch to briefly, so that switching back makes the firmware apply
    its own clocks for `key` (MinerSentinel never sets the clocks of a mode itself)."""
    return '2/L3' if key == '2/L4' else '2/L4'


def _send_mode(st, ip, conf, key, now, elapsed, after):
    """Switch via the A15 web interface and wait for the miner to report `key`, then go to `after`."""
    mode, level = parse_key(key)
    a15_set_mode(ip, conf, mode, level)
    st.update({'phase': 'wait_mode', 'expect': key, 'after': after, 'switch_sent_at': _iso(now),
               'elapsed_before': elapsed, 'post_at': _iso(now), 'posts': 1})


def step_run(conn, run):
    st = run['state'] or {}
    now = _now()
    ip, port = run['ip_address'], run['port']
    # clocks settling / measuring: values as fresh as before; otherwise the collector's readings suffice
    m = read_miner(ip, port, fresh=st.get('phase') in ('set_clock', 'measure'))
    if not m['points']:
        return run
    st['seen_at'] = _iso(now)
    st['miner_mode'] = m['mode_key']
    st.pop('last_error', None)
    finishing = run['status'] in ('cancelling', 'finishing')

    if m['mode_key'] is None:          # mode/level not readable this round: do nothing
        run['state'] = st
        return run
    if 'orig_mode' not in st:
        st['orig_mode'] = m['mode_key']
        st['phase'] = 'switch'

    if 'Over Hot' in m['status'] and not finishing:
        run['status'], run['pause_reason'] = 'paused', 'overhot'
        st['resume_pending'] = True
        run['message'] = 'Over Hot gemeldet: Messung pausiert.'
        run['state'] = st
        return run

    if st.pop('resume', False):
        st['resume_pending'] = True
    if st.pop('resume_pending', False):
        # measure the last stored point again, then continue
        if run['kind'] == 'step':
            run['step_index'] = max(int(st.get('saved_steps', 0)) - 1, 0)
        else:
            st['count'] = 0
            with conn.cursor() as cur:
                cur.execute("DELETE FROM calibration_points WHERE run_id = %s AND mode_key = %s",
                            (run['id'], run['modes'][run['mode_index']]))
        st['phase'] = 'switch'

    if finishing and not st.get('restoring'):
        # enter the restore phase once; afterwards wait_mode confirms the switch instead of
        # sending the switch command again every 15 s
        st['restoring'] = True
        st['phase'] = 'restore'

    modes = run['modes']
    phase = st.get('phase', 'switch')

    if phase == 'switch':
        if run['mode_index'] >= len(modes):
            st['phase'] = 'restore'
            run['state'] = st
            return run
        target = modes[run['mode_index']]
        applied = st.setdefault('applied', [])
        if m['mode_key'] == target and target in applied:
            st['phase'] = 'set_clock' if run['kind'] == 'step' else 'measure'
            st['changed_at'] = _iso(now)
            st['last_speed'] = None
            st['pw'] = []
        elif m['mode_key'] == target:
            # already in this mode, but possibly with clocks not set by the firmware: switch away
            # briefly and back, so the firmware applies its own clocks for this mode
            _send_mode(st, ip, m['conf'], bounce_key(target), now, m['elapsed'], 'switch')
            run['message'] = 'Schalte kurz auf %s und zurück auf %s, damit die Firmware ihre Takte setzt …' % (
                mode_label(bounce_key(target)), mode_label(target))
        else:
            _send_mode(st, ip, m['conf'], target, now, m['elapsed'], 'target')
            run['message'] = 'Schalte auf %s …' % mode_label(target)

    elif phase == 'wait_mode':
        target = modes[run['mode_index']] if run['mode_index'] < len(modes) else None
        expect = st.get('expect') or target
        sent = _parse(st.get('switch_sent_at')) or now
        notes = st.setdefault('notes', {})
        if m['mode_key'] == expect and st.get('after') == 'switch':
            st['phase'] = 'switch'                     # bounce done: now switch (back) to the target
        elif m['mode_key'] == expect and st.get('after') in ('restore_back', 'restore_done'):
            st['phase'] = st['after']
        elif m['mode_key'] == expect:
            restarted = (st.get('elapsed_before') is not None and m['elapsed'] is not None
                         and m['elapsed'] < st['elapsed_before'])
            notes[target] = {'switch_s': int((now - sent).total_seconds()), 'restart': bool(restarted)}
            st.setdefault('applied', []).append(target)
            st['phase'] = 'set_clock' if run['kind'] == 'step' else 'measure'
            st['changed_at'] = _iso(now)
            st['last_speed'] = None
            st['pw'] = []
        elif st.get('after') in ('restore_back', 'restore_done') and (now - sent).total_seconds() > SWITCH_TIMEOUT:
            st['phase'] = 'restore_done'
            run['message'] = 'Zurückschalten nicht bestätigt (%s).' % mode_label(expect)
        elif (now - sent).total_seconds() > SWITCH_TIMEOUT:
            notes[target] = {'error': 'ließ sich nicht umschalten (nach %d s weiter %s)'
                                      % (SWITCH_TIMEOUT, mode_label(m['mode_key']) if m['mode_key'] else '?')}
            if run['kind'] == 'step':
                run['message'] = '%s ließ sich nicht einschalten.' % mode_label(target)
                st['phase'] = 'restore'
            else:
                run['mode_index'] += 1
                st['count'] = 0
                st['phase'] = 'switch'
        elif (now - (_parse(st.get('post_at')) or sent)).total_seconds() >= RESEND_S:
            mode, level = parse_key(expect)
            a15_set_mode(ip, m['conf'], mode, level)
            st['post_at'] = _iso(now)
            st['posts'] = int(st.get('posts', 1)) + 1
            logger.info('Calibration run %s: %s noch nicht aktiv, Umschaltbefehl erneut gesendet (%d.)',
                        run['id'], mode_label(expect), st['posts'])

    elif phase == 'set_clock':
        target = modes[run['mode_index']]
        if not st.get('stock_points'):
            if True:
                # stock clocks = what the firmware set after switching to this mode, once unchanged
                if st.get('clk') != m['points']:
                    st['clk'], st['clk_since'] = m['points'], _iso(now)
                if (now - (_parse(st.get('clk_since')) or now)).total_seconds() < CLOCK_STABLE_S:
                    run['message'] = 'Warte, bis der Standardtakt von %s stabil ist.' % mode_label(target)
                    run['state'] = st
                    return run
                st['stock_points'] = m['points']
        stock = st['stock_points']
        stock_avg = avg(stock)
        want = stock_avg - STEP_MHZ * run['step_index']
        if want < stock_avg * FLOOR_SHARE:
            run['message'] = 'Messreihe abgeschlossen.'
            st['phase'] = 'restore'
        else:
            points = stock if run['step_index'] == 0 else [
                max(int(round(p * want / stock_avg)), int(p * FLOOR_SHARE)) for p in stock]
            set_frequency(ip, port, points)
            st.update({'set_points': points, 'phase': 'measure', 'changed_at': _iso(now), 'last_speed': None,
                       'pw': [], 'stable': False,
                       'steps_total': int((stock_avg * (1 - FLOOR_SHARE)) // STEP_MHZ) + 1})
            run['message'] = 'Punkt %d: Takt %.0f MHz gesetzt, warte auf saubere Messung.' % (
                run['step_index'] + 1, avg(points))

    elif phase == 'measure':
        target = modes[run['mode_index']]
        if m['mode_key'] != target:
            # the miner changed mode by itself (reboot etc.): start this point again
            st['phase'] = 'switch'
        elif m['ghsspd']:
            changed = _parse(st.get('changed_at')) or now
            fresh = st.get('last_speed') is not None and abs(m['ghsspd'] - st['last_speed']) > 1e-6
            st['last_speed'] = m['ghsspd']
            st['stable'] = _power_stable(st, m['power'], now)
            wait = MAX_WAIT if run['kind'] == 'max' else CLEAN_AFTER
            if fresh and st['stable'] and (now - changed).total_seconds() >= wait:
                if run['kind'] == 'step':
                    _save_point(conn, run, m, 'step', target, run['step_index'], st.get('set_points'))
                    run['step_index'] += 1
                    st['saved_steps'] = run['step_index']
                    st['phase'] = 'set_clock'
                else:
                    count = int(st.get('count', 0))
                    _save_point(conn, run, m, 'max', target, count, m['points'])
                    count += 1
                    st['count'] = count
                    run['message'] = '%s: Messung %d von %d gespeichert.' % (mode_label(target), count,
                                                                         MAX_MEASUREMENTS)
                    if count >= MAX_MEASUREMENTS:
                        run['mode_index'] += 1
                        st['count'] = 0
                        st['phase'] = 'switch'
                    else:
                        # the next measurement waits the full time again
                        st['changed_at'] = _iso(now)
                        st['last_speed'] = None
                        st['pw'] = []
                        st['stable'] = False

    elif phase == 'restore':
        # Abbrechen: the current mode stays as it is. Finished normally: back to the mode of the start.
        # Clocks set by MinerSentinel (Messreihe) go back to the firmware by switching briefly away and back.
        back = m['mode_key'] if run['status'] == 'cancelling' else (st.get('orig_mode') or m['mode_key'])
        clocks_changed = run['kind'] == 'step' and st.get('set_points') is not None
        st['back_mode'] = back
        if back and m['mode_key'] != back:
            _send_mode(st, ip, m['conf'], back, now, m['elapsed'], 'restore_done')
            run['message'] = 'Schalte zurück auf %s …' % mode_label(back)
        elif back and clocks_changed:
            _send_mode(st, ip, m['conf'], bounce_key(back), now, m['elapsed'], 'restore_back')
            run['message'] = 'Gebe die Takte an die Firmware zurück: kurz %s, dann wieder %s …' % (
                mode_label(bounce_key(back)), mode_label(back))
        else:
            st['phase'] = 'restore_done'

    elif phase == 'restore_back':
        back = st.get('back_mode') or st.get('orig_mode') or modes[0]
        _send_mode(st, ip, m['conf'], back, now, m['elapsed'], 'restore_done')
        run['message'] = 'Schalte zurück auf %s …' % mode_label(back)

    if st.get('phase') == 'restore_done':
        run['status'] = 'cancelled' if run['status'] == 'cancelling' else 'done'
        run['finished_at'] = now
        if run['status'] == 'cancelled':
            run['message'] = 'Abgebrochen.'
        elif run['kind'] == 'max':
            run['message'] = 'Max-Messung abgeschlossen.'
        st['phase'] = 'done'

    run['state'] = st
    return run


def run_calibration(database_url):
    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.Error as exc:
        logger.warning('Calibration: no database: %s', exc)
        return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT r.id, r.device_id, r.kind, r.status, r.pause_reason, r.modes, r.mode_index, r.step_index, "
                "r.state, r.message, r.finished_at, d.ip_address, d.port, d.connection_config "
                "FROM calibration_runs r JOIN devices d ON d.id = r.device_id "
                "WHERE r.status IN ('running', 'cancelling', 'finishing')")
            runs = cur.fetchall()
        for run in runs:
            try:
                run = step_run(conn, dict(run))
                with conn.cursor() as cur:
                    # only write if the user did not change the run meanwhile (pause/cancel from the UI)
                    cur.execute(
                        "UPDATE calibration_runs SET status=%s, pause_reason=%s, mode_index=%s, step_index=%s, "
                        "state=%s, message=%s, finished_at=%s, updated_at=%s "
                        "WHERE id=%s AND status IN ('running', 'cancelling', 'finishing')",
                        (run['status'], run['pause_reason'] or '', run['mode_index'], run['step_index'],
                         json.dumps(run['state']), run['message'] or '', run['finished_at'], _now(), run['id']))
                conn.commit()
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, AttributeError,
                    ZeroDivisionError, psycopg2.Error) as exc:
                conn.rollback()
                logger.info('Calibration run %s: %s', run['id'], exc)
                try:
                    with conn.cursor() as cur:
                        # the error goes next to the status message, it does not replace it
                        cur.execute("UPDATE calibration_runs SET state = jsonb_set(COALESCE(state, '{}'::jsonb), "
                                    "'{last_error}', to_jsonb(%s::text)), updated_at=%s WHERE id=%s",
                                    ('Verbindungsfehler %s: %s (wird alle 15 s erneut versucht)'
                                     % (datetime.now(timezone.utc).strftime('%H:%M:%S UTC'), str(exc)[:200]),
                                     _now(), run['id']))
                    conn.commit()
                except psycopg2.Error:
                    conn.rollback()
    finally:
        conn.close()
