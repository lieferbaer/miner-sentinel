"""Lüfter (AC Infinity) test page: show controllers/ports, back up settings, test which write variant works;
settings and state of the Temperatur-Automatik (A15)."""
import json
import logging
import re
import time

from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import acinfinity as ac
from .models import AutomationLog, Device, DeviceSystemInfo, FanAutomation

logger = logging.getLogger(__name__)

READ_BACK_S = 5       # user choice (was 10); measured: the controller reports a new speed only after > 5 s
# fan_automation.state as written by the data-service (data-service/fan_automation.py) that the page reads
# (v2.2.7, Nutzer 06.11.: Heizmodus/Basismodus/Kühlmodus, locks of the clock increase, excursions above ZTO)
STATE_KEYS = ('phase', 't', 'tendency', 'boards', 'clock_set', 'clock_changed_at', 'clock_now', 'mode_key',
              'stock_mhz', 'fan_target', 'fan_set', 'fan_read', 'fan_read_at', 'fan_set_at', 'fan_changed_at',
              'learn_since', 'over_hot', 'offline', 'ac_error', 'last_error', 'seen_at', 'start_hold', 'restart_req',
              'heat', 'cool', 'locks', 'hot', 'cut_at', 'zb')
PENDING_KEYS = ('kind', 'value', 'sent_at', 'attempt')
STATE_FRESH_S = 15    # the data-service writes the state every 5 s (only while it reads the miner)
TMAX_FRESH_S = 30     # the collector writes the chip temperatures every 10 s
# Zieltemperatur (Nutzer 06.11.): ZTU from 77, ZTO up to 87 °C, 4 to 5 Grad (both edges counted: 81–84 = 4 Grad,
# 81–85 = 5 Grad)
TARGET_MIN, TARGET_MAX = 77, 87
TARGET_ERROR = 'Zieltemperatur 77–87 °C mit 4 bis 5 Grad wählen.'
# delete a single Lernwert (Nutzer 08.11.): the data-service deletes it in its next round
LEARNED_NOT_FOUND = 'Lernwert nicht gefunden.'
LEARNED_GONE = 'Lernwert nicht mehr vorhanden oder neu gelernt.'
LEARNED_NOT_DELETED = 'Lernwert nicht gelöscht.'


def _a15():
    """The A15 the Temperatur-Automatik works on (only one)."""
    from .views import DeviceViewSet
    for dev in Device.objects.filter(is_active=True, protocol=Device.PROTOCOL_CGMINER_TCP):
        if DeviceViewSet._throttle_family(dev) == 'a15':
            return dev
    return None


def _automation():
    dev = _a15()
    if not dev:
        return None, None
    auto, _ = FanAutomation.objects.get_or_create(device=dev)
    return dev, auto


def _latest_tmax(dev):
    """(hottest chip, MTmax per board, time) as the overview shows it (latest MTmax of the collector, every 10 s)."""
    row = DeviceSystemInfo.objects.filter(device=dev).order_by('-recorded_at').values('details', 'recorded_at').first()
    boards = ((row or {}).get('details') or {}).get('chip_temp_boards') or []
    valid = [b for b in boards if isinstance(b, (int, float)) and -50 < b < 200]
    return (max(valid) if valid else None), boards, (row or {}).get('recorded_at')


def _per_level(learned):
    """Learned values per fan level only (entries with 'mhz'; the per-mode values before 24.10. are dropped)."""
    return {k: v for k, v in (learned or {}).items() if isinstance(v, dict) and 'mhz' in v}


def automation_payload():
    dev, auto = _automation()
    if not auto:
        return None
    st = auto.state or {}
    tmax, boards, at = _latest_tmax(dev)
    pending = st.get('pending') if isinstance(st.get('pending'), dict) else None
    state = {k: st.get(k) for k in STATE_KEYS}
    state['base'] = auto.level
    state['pending'] = {k: pending.get(k) for k in PENDING_KEYS} if pending else None
    return {
        'now': {'tmax': tmax, 'boards': boards, 'at': at},
        'device': {'id': dev.id, 'name': dev.name}, 'enabled': auto.enabled, 'dev_id': auto.ac_dev_id,
        'maintenance_at': auto.maintenance_at, 'maintenance': st.get('maint') or None,
        'target_low': auto.target_low, 'target_high': auto.target_high,
        'port': auto.ac_port, 'level': auto.level,
        'state': state,
        # learned values per fan level ({"<stufe>": {mhz, temp_avg, at, count}}); the former per-mode values
        # of the old regulation are not shown
        'learned': _per_level(st.get('learned')),
        # those of the other Zieltemperaturen ({"81-84": {"<stufe>": …}}, only with values; Nutzer 08.11.)
        'learned_zb': {zb: _per_level(v) for zb, v in (st.get('learned_zb') or {}).items() if _per_level(v)},
        'log': (st.get('log') or [])[-15:][::-1],
    }


def automation_on(dev_id, port):
    """True if the Temperatur-Automatik controls this fan (manual fan buttons are then locked)."""
    return FanAutomation.objects.filter(enabled=True, ac_dev_id=str(dev_id), ac_port=port).exists()


def _lift_throttle(dev):
    """A manual clock of the A15 left from before 24.10. (the Drosselung of the A15 is abolished) is lifted
    when the automation is switched on."""
    from .views import DeviceViewSet
    st = json.loads(json.dumps((dev.connection_config or {}).get('throttle') or {}))
    active = st.get('active')
    if not active:
        return
    st['active'] = None
    st['last_event'] = {'type': 'automation', 'detail': 'für die Temperatur-Automatik aufgehoben',
                        'at': timezone.now().isoformat(), 'target_ths': active.get('target_ths'),
                        'clock_mhz': active.get('clock_mhz')}
    st['stable_since'] = None
    DeviceViewSet()._throttle_save(dev, st)


def _chip_temp_now(dev, st, now):
    """(chip temperature, tendency) at the time of the user's action: the data-service's values only while it
    reads the miner (automation on, miner answers) and they are valid (no Over Hot); otherwise the collector's
    latest value without tendency (null if older than TMAX_FRESH_S)."""
    try:
        seen = parse_datetime(st.get('seen_at') or '')
    except (TypeError, ValueError):
        seen = None
    if seen and (now - seen).total_seconds() <= STATE_FRESH_S:
        if st.get('over_hot'):
            return None, None                    # readings during Over Hot are not used
        tendency = st.get('tendency')
        return st.get('t'), tendency if isinstance(tendency, int) else None
    tmax, _, at = _latest_tmax(dev)
    return (tmax if at and (now - at).total_seconds() <= TMAX_FRESH_S else None), None


def _user_row(dev, auto, kind, trigger, **fields):
    """Row of a user event in automation_log (source 'nutzer'); phase as the data-service last wrote it.
    Raises DatabaseError."""
    st = auto.state or {}
    now = timezone.now()
    chip_temp, tendency = _chip_temp_now(dev, st, now)
    with transaction.atomic():
        AutomationLog.objects.create(
            device=dev, recorded_at=now, kind=kind, source='nutzer', trigger=trigger,
            phase=str(st.get('phase') or '')[:16], chip_temp=chip_temp, tendency=tendency, **fields)


def _user_event(dev, auto, kind, trigger, **fields):
    """User event in the protocol of the automation. A missing table never blocks the user's action."""
    try:
        _user_row(dev, auto, kind, trigger, **fields)
    except DatabaseError as exc:
        logger.warning('Protokoll: Nutzer-Ereignis %s nicht gespeichert: %s', kind, exc)


def _learned_delete(request, dev, auto):
    """Delete one Lernwert (Nutzer 08.11.): only a request row in automation_log (kind 'lernwert_loeschen'); the
    data-service deletes the value in its next round if it still has this 'at' (also with the automation off).
    auto.state is only read here."""
    zb, level, at = request.data.get('zb'), _whole_number(request.data.get('level')), request.data.get('at')
    match = re.fullmatch(r'(\d{1,3})-(\d{1,3})', zb) if isinstance(zb, str) else None
    if not match or level is None or not 1 <= level <= 10:
        return Response({'error': LEARNED_NOT_FOUND}, status=status.HTTP_400_BAD_REQUEST)
    lo, hi = int(match.group(1)), int(match.group(2))
    zb = '%d-%d' % (lo, hi)
    st = auto.state or {}
    try:
        current = '%d-%d' % tuple(st['zb'])
    except (KeyError, TypeError, ValueError):
        current = None                        # no Zieltemperatur taken over yet: only the other ones
    if zb == current:
        values = st.get('learned')
    else:
        others = st.get('learned_zb')
        values = others.get(zb) if isinstance(others, dict) else None
    entry = values.get(str(level)) if isinstance(values, dict) else None
    if not isinstance(entry, dict) or not isinstance(at, str) or entry.get('at') != at:
        return Response({'error': LEARNED_GONE}, status=status.HTTP_409_CONFLICT)
    try:
        _user_row(dev, auto, 'lernwert_loeschen', 'Nutzer: Lernwert Stufe %d (%d–%d °C) gelöscht' % (level, lo, hi),
                  details={'zieltemperatur': zb, 'stufe': level, 'at': at, 'takt': entry.get('mhz'),
                           'temp_mittel': entry.get('temp_avg'), 'anzahl': entry.get('count')})
    except DatabaseError as exc:
        logger.warning('Protokoll: Lernwert-Löschung nicht gespeichert: %s', exc)
        return Response({'error': LEARNED_NOT_DELETED}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(automation_payload())


def _whole_number(value):
    """Whole number from the request (JSON number or text such as '82'); None for anything else (82.5, True, 'x')."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _error(exc):
    if isinstance(exc, ac.NotActive):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, ac.NotConfigured):
        code = status.HTTP_400_BAD_REQUEST
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return Response({'error': str(exc), 'session': ac.session_info()}, status=code)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def fans_status(request):
    if not ac.configured():
        return Response({'configured': False, 'hint': 'In %s/credentials AC_EMAIL und AC_PASSWORD eintragen.'
                                                      % ac.HOST_DIR, 'devices': [], 'backups': [],
                         'session': ac.session_info()})
    try:
        devs = [ac.summarize(d) for d in ac.devices()]
    except (ac.NotActive, ac.SessionTaken):
        devs = []                     # test session not active / Live waits before taking the session back
    except ac.ACError as exc:
        return _error(exc)
    return Response({'configured': True, 'devices': devs, 'backups': ac.list_backups(),
                     'session': ac.session_info(), 'automation': automation_payload()})


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def fans_automation(request):
    """Temperatur-Automatik (A15). GET: settings + state. POST {action: enable, dev_id, port, level} |
    {action: level, level} | {action: disable} | {action: maintenance_start|maintenance_end} |
    {action: target, low, high} (Zieltemperatur, also with the automation off or during Wartung) |
    {action: learned_delete, zb: 'lo-hi', level, at} (one Lernwert, also with the automation off; Nutzer 08.11.).
    The data-service applies the changes. The actions of the removed Temperatur-Test (temp_test_start|
    temp_test_abort) and Obergrenzen (caps_clear) are refused like any unknown action (v2.2.7, Nutzer 06.11.)."""
    dev, auto = _automation()
    if not auto:
        return Response({'error': 'Kein Avalon A15 gefunden.'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'POST':
        action = request.data.get('action')
        if action == 'learned_delete':
            return _learned_delete(request, dev, auto)
        try:
            level = int(request.data.get('level')) if request.data.get('level') is not None else auto.level
        except (TypeError, ValueError):
            level = -1
        if action in ('enable', 'level') and not 1 <= level <= 10:
            return Response({'error': 'Stufe 1–10 wählen.'}, status=status.HTTP_400_BAD_REQUEST)
        if action == 'enable':
            dev_id = str(request.data.get('dev_id') or '')
            try:
                port = int(request.data.get('port'))
            except (TypeError, ValueError):
                port = 0
            if not dev_id or port not in (1, 2, 3, 4):
                return Response({'error': 'Controller und Port wählen.'}, status=status.HTTP_400_BAD_REQUEST)
            if not ac.has_backup(dev_id):
                return Response({'error': 'Bitte zuerst eine Sicherung erstellen.'}, status=status.HTTP_409_CONFLICT)
            _lift_throttle(dev)
            auto.enabled, auto.ac_dev_id, auto.ac_port, auto.level = True, dev_id, port, level
            auto.level_at = timezone.now()
            events = [('automatik_ein', 'Nutzer: Automatik eingeschaltet (Basisstufe %d)' % level, {'base_new': level})]
        elif action == 'level' and auto.enabled:
            events = [('basisstufe', 'Nutzer: Basisstufe %d gewählt' % level,
                       {'base_old': auto.level, 'base_new': level})]
            auto.level, auto.level_at = level, timezone.now()     # the data-service starts with this level
        elif action == 'disable':
            # Automatik aus: no clock is sent, the clock stays as it is; already off: no entry
            events = [('automatik_aus', 'Nutzer: Automatik beendet (Takt bleibt, wie er ist)', {})] if auto.enabled else []
            auto.enabled = False
        elif action == 'maintenance_start' and not auto.maintenance_at:
            # Wartung: the A15 is switched off by hand
            auto.maintenance_at = timezone.now()
            events = [('wartung_start', 'Nutzer: Wartung gestartet', {})]
        elif action == 'maintenance_end':
            events = [('wartung_ende', 'Nutzer: Wartung beendet', {})] if auto.maintenance_at else []
            auto.maintenance_at = None
        elif action == 'target':
            # Zieltemperatur (Nutzer 06.11.): the data-service takes it over in its next round
            low, high = _whole_number(request.data.get('low')), _whole_number(request.data.get('high'))
            if low is None or high is None or not (TARGET_MIN <= low and high <= TARGET_MAX and 3 <= high - low <= 4):
                return Response({'error': TARGET_ERROR}, status=status.HTTP_400_BAD_REQUEST)
            old_low, old_high = auto.target_low, auto.target_high
            if (low, high) == (old_low, old_high):
                return Response(automation_payload())                 # same Zieltemperatur: no entry
            events = [('zielbereich', 'Nutzer: Zieltemperatur %d–%d °C gewählt (vorher %d–%d °C)'
                       % (low, high, old_low, old_high),
                       {'details': {'alt': '%d–%d °C' % (old_low, old_high), 'neu': '%d–%d °C' % (low, high)}})]
            auto.target_low, auto.target_high = low, high
        else:
            return Response({'error': 'Diese Aktion ist gerade nicht möglich.'}, status=status.HTTP_400_BAD_REQUEST)
        auto.updated_at = timezone.now()
        auto.save(update_fields=['enabled', 'ac_dev_id', 'ac_port', 'level', 'level_at', 'maintenance_at',
                                 'target_low', 'target_high', 'updated_at'])
        for kind, trigger, fields in events:
            _user_event(dev, auto, kind, trigger, **fields)
    return Response(automation_payload())


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def fans_session(request):
    """Test environment only. GET: session state. POST {action: activate|end}."""
    if request.method == 'POST':
        action = request.data.get('action')
        try:
            if action == 'activate':
                ac.activate_test()
            elif action == 'takeover':
                ac.takeover()
            elif action == 'end':
                ac.end_test('manual')
            else:
                return Response({'error': 'Ungültige Aktion.'}, status=status.HTTP_400_BAD_REQUEST)
        except ac.ACError as exc:
            return _error(exc)
    return Response(ac.session_info())


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def fans_credentials(request):
    """GET: stored e-mail and whether a password is set. POST {email, password}: save (empty password = keep)."""
    if request.method == 'POST':
        try:
            ac.save_credentials(request.data.get('email'), request.data.get('password') or None)
        except ac.ACError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(ac.credential_info())


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fans_backup(request):
    dev_id = str(request.data.get('dev_id') or '')
    if not dev_id:
        return Response({'error': 'dev_id fehlt.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        name, ports = ac.make_backup(dev_id)
    except ac.ACError as exc:
        return _error(exc)
    return Response({'file': name, 'ports': {p: {'ok': v['ok'], 'code': v['code'], 'msg': v['msg']}
                                             for p, v in ports.items()}, 'backups': ac.list_backups()})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fans_backup_delete(request):
    """POST {file}: delete one backup; the last backup of a controller stays (Nutzer 08.11.)."""
    name = str(request.data.get('file') or '')
    try:
        ac.delete_backup(name)
    except ac.LastBackup as exc:
        return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
    except ac.ACError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
    return Response({'deleted': name, 'backups': ac.list_backups()})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fans_restore(request):
    try:
        dev_id, results = ac.restore_backup(str(request.data.get('file') or ''))
    except ac.ACError as exc:
        return _error(exc)
    return Response({'dev_id': dev_id, 'results': results})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def fans_test(request):
    """POST {dev_id, port (1-4, the port the fan is on), action: set|on|off, speed (0-10)}.
    Writes, waits READ_BACK_S, reads the speed again and reports whether it was taken over."""
    data = request.data
    dev_id = str(data.get('dev_id') or '')
    action = data.get('action')
    try:
        port = int(data.get('port'))
        speed = int(data.get('speed') if data.get('speed') is not None else 0)
    except (TypeError, ValueError):
        return Response({'error': 'Port und Stufe müssen Zahlen sein.'}, status=status.HTTP_400_BAD_REQUEST)
    if not dev_id or port not in (1, 2, 3, 4) or not 0 <= speed <= 10 or action not in ('set', 'on', 'off'):
        return Response({'error': 'Ungültige Angaben.'}, status=status.HTTP_400_BAD_REQUEST)
    if not ac.has_backup(dev_id):
        return Response({'error': 'Bitte zuerst eine Sicherung erstellen.'}, status=status.HTTP_409_CONFLICT)
    if automation_on(dev_id, port):
        return Response({'error': 'Die Temperatur-Automatik steuert diesen Lüfter.'}, status=status.HTTP_409_CONFLICT)

    if action == 'set':        # tested on the 69 Pro: per port, only onSpead changes (modeType stays as it is)
        write_port, overlay, headers = port, {'onSpead': speed}, ac.HEADERS_OBER37
        expect = speed
    else:                      # atType: OFF 1, ON 2 (ober37)
        write_port, overlay, headers = port, {'atType': 2 if action == 'on' else 1}, ac.HEADERS_OBER37
        expect = None
    try:
        before = ac.speed_of(dev_id, port)
        read = ac.port_settings(dev_id, write_port)
        settings = read.get('data')
        if read.get('code') != ac.CODE_OK or not isinstance(settings, dict):
            return Response({'error': 'Einstellungen von Port %d nicht lesbar (Code %s: %s)'
                                      % (write_port, read.get('code'), read.get('msg'))},
                            status=status.HTTP_502_BAD_GATEWAY)
        payload = ac.build_payload(settings, overlay)
        payload.setdefault('devId', dev_id)
        res = ac.write_settings(payload, headers)
        time.sleep(READ_BACK_S)
        after = ac.speed_of(dev_id, port)
    except ac.ACError as exc:
        return _error(exc)
    if expect is not None:
        accepted = after == expect
    elif action == 'on':
        accepted = bool(after)
    else:
        accepted = after == 0
    return Response({
        'action': action, 'write_port': write_port, 'fan_port': port, 'speed': speed,
        'changes': {k: {'before': settings.get(k), 'sent': v} for k, v in overlay.items()},
        'headers': sorted(headers), 'code': res.get('code'), 'msg': res.get('msg'), 'http': res.get('_http'),
        'before': before, 'after': after, 'accepted': accepted, 'waited_s': READ_BACK_S,
    })
