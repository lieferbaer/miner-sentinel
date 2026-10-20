"""AC Infinity cloud API (unofficial) for the fan test page ("Lüfter").

Sources: ober37/ac-infinity-mcp docs/API.md, keithah/homebridge-acinfinity (API_REFERENCE, CHANGELOG 1.3.3).
- login POST /user/appUserLogin (appEmail, appPasswordl - the "l" is required, password max 25 chars)
  -> token = data.appId, sent as header "token"
- only ONE session per account: every login can log out the phone app, so the token is cached
- writes POST /dev/addDevMode: always read the port settings first and send all fields back
  (without modeSetid, fieldSet, devSetting, ipcSetting); at least 1.5 s between writes (else 403)
- the controller reports a new speed only after > 5 s

Test environment (MINERSENTINEL_ENV=Test, e.g. a local copy on the iMac): it logs in only via
"Test aktivieren", uses the session for at most TEST_SESSION_S and never logs in again by itself.
Live (default) logs in again by itself, but only RELOGIN_PAUSE_S after another login took the session
(test environment or phone app), or at once via "Live jetzt übernehmen" (takeover).
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = 'https://www.acinfinityserver.com/api'
DATA_DIR = os.environ.get('ACINFINITY_DATA_DIR') or '/app/acinfinity_data'
CRED_FILE = DATA_DIR + '/credentials'
TOKEN_FILE = DATA_DIR + '/token'
SESSION_FILE = DATA_DIR + '/test_session'
PAUSE_FILE = DATA_DIR + '/relogin_after'
RELOGIN_PAUSE_S = 300         # Live: wait this long before taking a session back
HOST_DIR = os.environ.get('ACINFINITY_DATA_DIR') or '/opt/minersentinel/backend/acinfinity_data'
ENV_NAME = os.environ.get('MINERSENTINEL_ENV') or 'Live'
IS_TEST = ENV_NAME.lower() == 'test'
TEST_SESSION_S = 300          # test environment: the AC Infinity session is used for at most 5 min
WRITE_GAP = 1.5
EXCLUDE = ('modeSetid', 'fieldSet', 'devSetting', 'ipcSetting')
CODE_OK = 200
CODE_SESSION_EXPIRED = 10003
HEADERS_OBER37 = {}
HEADERS_KEITHAH = {'phoneType': '1', 'appVersion': '1.9.7', 'minversion': '3.5'}

_write_lock = threading.Lock()
_last_write = [0.0]
_login_lock = threading.Lock()


class ACError(Exception):
    pass


class NotConfigured(ACError):
    pass


class NotActive(ACError):
    """Test environment without an active test session."""


class SessionTaken(ACError):
    """Live: another login took the session; Live logs in again after RELOGIN_PAUSE_S."""


def _pause_until():
    try:
        with open(PAUSE_FILE, encoding='utf-8') as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return 0.0


def _pause_text(until):
    return datetime.fromtimestamp(until, timezone.utc).isoformat()


def _start_pause():
    until = time.time() + RELOGIN_PAUSE_S
    with open(PAUSE_FILE, 'w', encoding='utf-8') as fh:
        fh.write(str(until))
    os.chmod(PAUSE_FILE, 0o600)
    return until


def takeover():
    """Live: log in now (ends a test session elsewhere)."""
    if IS_TEST:
        raise ACError('Nur in der Live-Umgebung möglich.')
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass
    login()


def _credentials():
    vals = {}
    try:
        with open(CRED_FILE, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    vals[key.strip()] = value.strip()
    except OSError:
        pass
    if not vals.get('AC_EMAIL') or not vals.get('AC_PASSWORD'):
        raise NotConfigured('Zugangsdaten fehlen: in %s/credentials AC_EMAIL und AC_PASSWORD eintragen.' % HOST_DIR)
    return vals['AC_EMAIL'], vals['AC_PASSWORD']


def credential_info():
    """E-mail and whether a password is stored (the password itself is never returned)."""
    try:
        email, _ = _credentials()
        return {'email': email, 'password_set': True}
    except NotConfigured:
        vals = {}
        try:
            with open(CRED_FILE, encoding='utf-8') as fh:
                for line in fh:
                    if line.strip().startswith('AC_EMAIL='):
                        vals['email'] = line.strip().split('=', 1)[1].strip()
        except OSError:
            pass
        return {'email': vals.get('email', ''), 'password_set': False}


def save_credentials(email, password=None):
    """Write the credentials file; password None/'' keeps the stored one. A new login is done on next use."""
    email = (email or '').strip()
    if not email or any(c in email for c in '\r\n') or '@' not in email:
        raise ACError('Bitte eine gültige E-Mail-Adresse eingeben.')
    if password:
        if any(c in password for c in '\r\n'):
            raise ACError('Das Passwort darf keinen Zeilenumbruch enthalten.')
    else:
        try:
            _, password = _credentials()
        except NotConfigured:
            raise ACError('Bitte ein Passwort eingeben.')
    tmp = CRED_FILE + '.new'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write('# AC-Infinity-App-Zugangsdaten nur fuer den MinerSentinel-Luefter-Test\n')
        fh.write('AC_EMAIL=%s\nAC_PASSWORD=%s\n' % (email, password))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED_FILE)
    try:
        os.remove(TOKEN_FILE)        # old token belongs to the old account/password
    except OSError:
        pass


def configured():
    try:
        _credentials()
        return True
    except NotConfigured:
        return False


def _post(path, params=None, token=None, headers=None, query=None):
    url = BASE + path + ('?' + urllib.parse.urlencode(query) if query else '')
    head = {'User-Agent': 'okhttp/3.10.0', 'Content-Type': 'application/x-www-form-urlencoded'}
    if token:
        head['token'] = token
    head.update(headers or {})
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, headers=head, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            http, body = resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        http, body = exc.code, exc.read().decode('utf-8', 'replace')
    except OSError as exc:
        raise ACError('AC-Infinity-Server nicht erreichbar: %s' % exc)
    try:
        out = json.loads(body)
        if not isinstance(out, dict):
            out = {'code': None, 'msg': str(out)[:200]}
    except ValueError:
        out = {'code': None, 'msg': body[:200]}
    out['_http'] = http
    return out


def login():
    """New login (logs out the phone app if it is signed in with the same account)."""
    email, password = _credentials()
    with _login_lock:
        res = _post('/user/appUserLogin', {'appEmail': email, 'appPasswordl': password[:25]})
        token = (res.get('data') or {}).get('appId') if isinstance(res.get('data'), dict) else None
        if res.get('code') != CODE_OK or not token:
            raise ACError('Anmeldung fehlgeschlagen (Code %s: %s)' % (res.get('code'), res.get('msg')))
        token = str(token)
        with open(TOKEN_FILE, 'w', encoding='utf-8') as fh:
            fh.write(token)
        os.chmod(TOKEN_FILE, 0o600)
        return token


def _read_session():
    try:
        with open(SESSION_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_session(data):
    with open(SESSION_FILE, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    os.chmod(SESSION_FILE, 0o600)


def _drop_token():
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass


def end_test(reason='manual'):
    """Test environment: stop using the session (manual, expired, or 'live' when Live took it back)."""
    with _login_lock:
        _drop_token()
        sess = _read_session()
        if sess and not sess.get('ended'):
            sess['ended'] = reason
            sess['ended_at'] = datetime.now(timezone.utc).isoformat()
            _write_session(sess)


def activate_test():
    """Test environment: log in now and use the session for TEST_SESSION_S."""
    if not IS_TEST:
        raise ACError('Nur in der Test-Umgebung möglich.')
    login()
    now = time.time()
    _write_session({'started': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    'until': datetime.fromtimestamp(now + TEST_SESSION_S, timezone.utc).isoformat(),
                    'until_ts': now + TEST_SESSION_S})


def session_info():
    if not IS_TEST:
        until = _pause_until()
        return {'env': ENV_NAME, 'test': False,
                'paused_until': _pause_text(until) if until > time.time() else None}
    sess = _read_session()
    if sess and not sess.get('ended') and time.time() >= sess.get('until_ts', 0):
        end_test('expired')
        sess = _read_session()
    active = bool(sess) and not sess.get('ended') and os.path.exists(TOKEN_FILE)
    return {'env': ENV_NAME, 'test': True, 'active': active, 'until': sess.get('until') if active else None,
            'ended': sess.get('ended'), 'ended_at': sess.get('ended_at'), 'minutes': TEST_SESSION_S // 60}


def _token():
    if IS_TEST:
        if not session_info()['active']:
            raise NotActive('Test-Umgebung nicht aktiv: erst „Test aktivieren“.')
        with open(TOKEN_FILE, encoding='utf-8') as fh:
            return fh.read().strip()
    try:
        with open(TOKEN_FILE, encoding='utf-8') as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    until = _pause_until()
    if until > time.time():
        raise SessionTaken('Anmeldung wird gerade von einer anderen Umgebung genutzt – Live übernimmt um %s UTC.'
                           % datetime.fromtimestamp(until, timezone.utc).strftime('%H:%M:%S'))
    token = login()
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass
    return token


def _call(path, build_params, write=False, headers=None, user_query=False):
    """build_params(token) -> form fields. Code 10003 (session taken by another login): the test
    environment stops, Live logs in again only after RELOGIN_PAUSE_S (see _token)."""
    token = _token()
    params = build_params(token)
    query = {'userId': token} if user_query else None
    if write:
        with _write_lock:
            wait = WRITE_GAP - (time.monotonic() - _last_write[0])
            if wait > 0:
                time.sleep(wait)
            res = _post(path, params, token, headers, query)
            _last_write[0] = time.monotonic()
    else:
        res = _post(path, params, token, headers, query)
    if res.get('code') == CODE_SESSION_EXPIRED and IS_TEST:
        end_test('live')           # Live (or the phone app) took the session back: never log in again here
        raise NotActive('Live hat die Anmeldung übernommen – Test beendet.')
    if res.get('code') == CODE_SESSION_EXPIRED:
        with _login_lock:
            _drop_token()
            until = _pause_until() if _pause_until() > time.time() else _start_pause()
        raise SessionTaken('Anmeldung wird gerade von einer anderen Umgebung genutzt – Live übernimmt um %s UTC.'
                           % datetime.fromtimestamp(until, timezone.utc).strftime('%H:%M:%S'))
    return res


def devices():
    res = _call('/user/devInfoListAll', lambda t: {'userId': t}, user_query=True)
    if res.get('code') != CODE_OK:
        raise ACError('Geräteliste: Code %s: %s' % (res.get('code'), res.get('msg')))
    return res.get('data') or []


def port_settings(dev_id, port):
    return _call('/dev/getdevModeSettingList', lambda t: {'devId': dev_id, 'port': port, 'appId': t})


def write_settings(payload, headers=None):
    return _call('/dev/addDevMode', lambda t: payload, write=True, headers=headers)


def set_fan_level(dev_id, port, level):
    """Set the fan level (0-10) of one port: read its settings, send them back with the new onSpead."""
    read = port_settings(dev_id, port)
    settings = read.get('data')
    if read.get('code') != CODE_OK or not isinstance(settings, dict):
        raise ACError('Einstellungen von Port %s nicht lesbar (Code %s: %s)' % (port, read.get('code'), read.get('msg')))
    payload = build_payload(settings, {'onSpead': int(level)})
    payload.setdefault('devId', dev_id)
    res = write_settings(payload, HEADERS_OBER37)
    if res.get('code') != CODE_OK:
        raise ACError('Stufe nicht gesetzt (Code %s: %s)' % (res.get('code'), res.get('msg')))
    return res


def build_payload(settings, overlay):
    """All flat fields of the read settings (without the excluded ones), overlaid with the changes."""
    out = {}
    for key, value in (settings or {}).items():
        if key in EXCLUDE or isinstance(value, (dict, list)) or value is None:
            continue
        out[key] = int(value) if isinstance(value, bool) else value
    for key, value in overlay.items():
        out[key] = value
    return out


def _scaled(value, div=100.0):
    try:
        return round(float(value) / div, 2)
    except (TypeError, ValueError):
        return None


def summarize(dev):
    info = dev.get('deviceInfo') or {}
    ports = []
    for p in info.get('ports') or []:
        ports.append({'port': p.get('port'), 'name': p.get('portName'), 'speak': p.get('speak'),
                      'online': p.get('online'), 'cur_mode': p.get('curMode'),
                      'raw': {k: v for k, v in p.items() if not isinstance(v, (dict, list))}})
    return {
        'dev_id': str(dev.get('devId')), 'name': dev.get('devName'), 'dev_type': dev.get('devType'),
        'online': dev.get('online'),
        'temperature_c': _scaled(info.get('temperature')), 'humidity_pct': _scaled(info.get('humidity')),
        'vpd_kpa': _scaled(info.get('vpdnums', info.get('vpdNums'))),
        'ports': ports,
    }


def find_device(dev_id):
    for dev in devices():
        if str(dev.get('devId')) == str(dev_id):
            return dev
    raise ACError('Controller %s nicht gefunden.' % dev_id)


def speed_of(dev_id, port):
    dev = find_device(dev_id)
    for p in (dev.get('deviceInfo') or {}).get('ports') or []:
        if str(p.get('port')) == str(port):
            return p.get('speak')
    return None


# ---- backups of the port settings (ports 0-4) ----

def _backup_files():
    try:
        names = sorted((n for n in os.listdir(DATA_DIR) if n.startswith('backup-') and n.endswith('.json')),
                       reverse=True)
    except OSError:
        names = []
    return names


def list_backups():
    out = []
    for name in _backup_files():
        try:
            with open(os.path.join(DATA_DIR, name), encoding='utf-8') as fh:
                data = json.load(fh)
            out.append({'file': name, 'dev_id': data.get('dev_id'), 'created_at': data.get('created_at'),
                        'ports': sorted(int(k) for k, v in (data.get('ports') or {}).items() if v.get('ok'))})
        except (OSError, ValueError):
            continue
    return out


def has_backup(dev_id):
    return any(b['dev_id'] == str(dev_id) for b in list_backups())


def make_backup(dev_id):
    ports = {}
    for port in range(0, 5):
        res = port_settings(dev_id, port)
        data = res.get('data')
        ok = res.get('code') == CODE_OK and isinstance(data, dict) and bool(data)
        ports[str(port)] = {'ok': ok, 'code': res.get('code'), 'msg': res.get('msg'), 'settings': data if ok else None}
    now = datetime.now(timezone.utc)
    name = 'backup-%s-%s.json' % (now.strftime('%Y%m%d-%H%M%S'), dev_id)
    with open(os.path.join(DATA_DIR, name), 'w', encoding='utf-8') as fh:
        json.dump({'dev_id': str(dev_id), 'created_at': now.isoformat(), 'ports': ports}, fh, indent=1)
    os.chmod(os.path.join(DATA_DIR, name), 0o600)
    return name, ports


def restore_backup(name):
    if name not in _backup_files():
        raise ACError('Sicherung %s nicht gefunden.' % name)
    with open(os.path.join(DATA_DIR, name), encoding='utf-8') as fh:
        data = json.load(fh)
    results = []
    for port, entry in sorted((data.get('ports') or {}).items(), key=lambda kv: int(kv[0])):
        if not entry.get('ok'):
            continue
        res = write_settings(build_payload(entry['settings'], {}))
        results.append({'port': int(port), 'code': res.get('code'), 'msg': res.get('msg'), 'http': res.get('_http')})
    return data.get('dev_id'), results
