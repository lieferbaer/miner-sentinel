"""Shared access to the Avalon A15 (cgminer API and web settings page), used by the Temperatur-Automatik
(fan_automation.py), the central reading (live_reader.py) and the throttle controller.

The Takt-Messung (Takt-Tabellen: measurement runs started from the web UI) is removed (v2.2.7, Nutzer 06.11.); its
tables calibration_runs/calibration_points stay in the database with their data, nothing here writes them any more.
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

import miner_cache

logger = logging.getLogger(__name__)

CACHE_MAX_AGE = 15        # s: readings of the central reader (live_reader.py, every 5 s; the automation runs right after it)
WEB_MAX_AGE = 35           # s: A15 web settings, read every 30 s centrally (own mode switches clear them at once)


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
    try:
        with miner_cache.lock(ip):
            raw = urllib.request.urlopen('http://%s/updatecgconf.cgi' % ip, timeout=5).read().decode('utf-8', 'replace')
    except http.client.HTTPException as exc:
        # e.g. IncompleteRead of the overloaded page: an OSError like a timeout, so every reader handles it (26.10.)
        raise OSError('%s: %s' % (type(exc).__name__, exc))
    match = re.search(r'CGConfCallback\((\{.*?\})\);', raw, re.S)
    return json.loads(match.group(1)) if match else None


_last_conf = {}           # ip -> last settings read from the A15 page (used while only the page does not answer)


def a15_set_mode(ip, conf, mode, level):
    """Same POST as the Save button of the A15 configuration page. Never with settings that were not read just
    now: the POST carries the pool fields (Nutzer 26.10.: a mode switch waits until the page answers again)."""
    if not conf or conf.get('_stale'):
        raise RuntimeError('A15-Einstellungsseite nicht erreichbar: Moduswechsel wartet (keine alten Pool-Daten)')
    fields = {k: conf.get(k, '') for k in ('pool1', 'worker1', 'passwd1', 'pool2', 'worker2', 'passwd2',
                                           'pool3', 'worker3', 'passwd3')}
    fields['mode'] = mode
    fields['power_level'] = '%02d' % (level if level is not None else int(conf.get('power_level') or 0))
    req = urllib.request.Request('http://%s/cgconf.cgi' % ip, data=urllib.parse.urlencode(fields).encode(),
                                 method='POST')
    try:
        with miner_cache.command(ip):
            urllib.request.urlopen(req, timeout=10).read()
        if ip in _last_conf:
            # switch sent: the last settings carry the new mode (pool fields unchanged), so a reading while only
            # the settings page does not answer reports the mode actually set (Review 26.10.)
            _last_conf[ip] = dict(_last_conf[ip], mode=mode, power_level=fields['power_level'])
    except (ConnectionResetError, http.client.HTTPException) as exc:
        # the A15 sometimes drops the connection of the switch command (then it is usually NOT applied); the
        # Temperatur-Automatik reads the mode back and sends the command again after its RESEND_S
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
    """Send the four clock points; returns the miner's reply (the protocol stores it)."""
    with miner_cache.command(ip):
        reply = _cmd(ip, port, json.dumps({'command': 'ascset',
                                           'parameter': '0,frequency,%s-0-0' % ':'.join(str(p) for p in points)}))
    if 'set OK' not in reply:
        raise RuntimeError('miner rejected frequency: ' + reply[:200])
    return reply[:500]


REBOOT_NO_REPLY = 'keine Antwort – Miner startet neu'


def reboot(ip, port, timeout=8):
    """Send the reboot command (ascset|0,reboot,0) like the button "A15 neu starten" (backend/api/views.py
    _reboot_command): not reachable (connect/send fails) raises OSError; once it is sent, a missing or broken reply
    means the miner is already restarting (Nutzer 27.10.). Automatischer Neustart der Temperatur-Automatik (Nutzer 28.10.)."""
    with miner_cache.command(ip), socket.create_connection((ip, port or 4028), timeout) as sock:
        sock.sendall(b'ascset|0,reboot,0')
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            if not chunks:
                return REBOOT_NO_REPLY
    return b''.join(chunks).decode('utf-8', 'ignore').replace('\x00', '').strip() or REBOOT_NO_REPLY


def mode_key(mode, level):
    return '2/L%d' % level if mode == 2 else '%d/-' % mode


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
    """fresh: read estats/summary from the miner, not from the collector's readings;
    fresh='estats': only estats (temperatures, clocks), summary from the collector (Temperatur-Automatik)."""
    age = None if fresh else CACHE_MAX_AGE
    estats = miner_cache.fetch(ip, 'estats', age, lambda: _retry(
        'cgminer-API estats', _cmd, ip, port, json.dumps({'command': 'estats'})))
    if fresh == 'estats':
        age = CACHE_MAX_AGE
    summ = miner_cache.fetch(ip, 'summary', age, lambda: _retry(
        'cgminer-API summary', _cmd, ip, port, json.dumps({'command': 'summary'}), 6))
    try:
        summary = json.loads(summ).get('SUMMARY', [{}])[0]
    except ValueError:
        summary = {}
    fields = _fields(estats)
    points = [int(v) for v in str(fields.get('SF0') or '').split()] if isinstance(fields.get('SF0'), str) else []
    # Nutzer 26.10.: answers only the settings page not (26.10. 04:31: 9.5 min, the mining interface answered), the
    # last settings read stand in – marked as stale, so no mode switch is ever sent with them
    web_error = None
    try:
        conf = miner_cache.fetch(ip, 'web', WEB_MAX_AGE, lambda: _retry('A15-Einstellungsseite lesen', a15_conf, ip))
    except (OSError, RuntimeError) as exc:
        # without settings read before (e.g. right after a restart of the data-service) the mode stays unknown here;
        # the Temperatur-Automatik then keeps its last known mode
        last = _last_conf.get(ip)
        conf, web_error = (dict(last, _stale=True) if last else None), str(exc)[:300]
    else:
        if conf:
            _last_conf[ip] = conf
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
        'conf_fresh': web_error is None, 'web_error': web_error,
        'mode_key': key, 'status': str(fields.get('SYSTEMSTATU') or ''),
        'ghsspd': _num(fields.get('GHSspd')), 'power': power if power and power > 0 else None,
        'dh': _num(fields.get('DH')), 'tmax': _num(fields.get('TMax')),
        'elapsed': summary.get('Elapsed'),
    }
