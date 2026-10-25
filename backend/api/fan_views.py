"""Lüfter (AC Infinity) test page: show controllers/ports, back up settings, test which write variant works."""
import json
import logging
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
STATE_KEYS = ('phase', 't', 'tendency', 'boards', 'clock_set', 'clock_changed_at', 'clock_now', 'mode_key',
              'stock_mhz', 'fan_target', 'fan_set', 'fan_read', 'fan_read_at', 'fan_set_at', 'window_until',
              'window_active', 'learn_since', 'cal', 'over_hot', 'offline', 'ac_error', 'last_error', 'seen_at',
              'exc_active')
PENDING_KEYS = ('kind', 'value', 'sent_at', 'attempt')
STATE_FRESH_S = 15    # the data-service writes the state every 5 s (only while it reads the miner)
TMAX_FRESH_S = 30     # the collector writes the chip temperatures every 10 s
# actions that set or clear FanAutomation.temp_test_at (Temperatur-Test, 25.10.)
TEST_ACTIONS = ('temp_test_start', 'temp_test_abort', 'disable', 'maintenance_start')


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
    # Temperatur-Test: stage, band, ends as the data-service wrote them; only for the test that is set now (an aborted
    # or older test keeps its state until the next full round of the data-service)
    test = st.get('test')
    started = parse_datetime(test.get('started') or '') if isinstance(test, dict) else None
    state['test'] = test if auto.temp_test_at and started and started == auto.temp_test_at else None
    return {
        'now': {'tmax': tmax, 'boards': boards, 'at': at},
        'device': {'id': dev.id, 'name': dev.name}, 'enabled': auto.enabled, 'dev_id': auto.ac_dev_id,
        'maintenance_at': auto.maintenance_at, 'maintenance': st.get('maint') or None,
        'temp_test_at': auto.temp_test_at,
        'port': auto.ac_port, 'level': auto.level,
        'state': state,
        # learned values per fan level ({"<stufe>": {mhz, temp_avg, at, count}}); the former per-mode values
        # of the old regulation are not shown
        'learned': {k: v for k, v in (st.get('learned') or {}).items() if isinstance(v, dict) and 'mhz' in v},
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


def _user_event(dev, auto, kind, trigger, **fields):
    """User event in the protocol of the automation (automation_log, source 'nutzer'); phase as the data-service
    last wrote it. A missing table never blocks the user's action."""
    st = auto.state or {}
    now = timezone.now()
    chip_temp, tendency = _chip_temp_now(dev, st, now)
    try:
        with transaction.atomic():
            AutomationLog.objects.create(
                device=dev, recorded_at=now, kind=kind, source='nutzer', trigger=trigger,
                phase=str(st.get('phase') or '')[:16], chip_temp=chip_temp, tendency=tendency, **fields)
    except DatabaseError as exc:
        logger.warning('Protokoll: Nutzer-Ereignis %s nicht gespeichert: %s', kind, exc)


def _temp_test_refusal(dev, auto):
    """Why a Temperatur-Test cannot start now (None: it can)."""
    from .models import CalibrationRun
    from .views import DeviceViewSet
    if not auto.enabled:
        return 'Temperatur-Test nur bei eingeschalteter Automatik.'
    if auto.maintenance_at:
        return 'Während der Wartung ist kein Temperatur-Test möglich.'
    if CalibrationRun.objects.filter(device=dev, status__in=DeviceViewSet.CAL_RUNNING).exists():
        return 'Während einer Takt-Messung ist kein Temperatur-Test möglich.'
    if auto.temp_test_at:
        return 'Ein Temperatur-Test läuft bereits.'
    return None


def _test_end(reason):
    """User event: a running Temperatur-Test is aborted (by the user, Automatik aus or Wartung)."""
    trigger = 'Nutzer: Temperatur-Test abgebrochen' + ('' if reason == 'Nutzer' else ' (%s)' % reason)
    return ('test_ende', trigger, {'details': {'ergebnis': 'abgebrochen', 'grund': reason}})


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
    {action: temp_test_start|temp_test_abort}. The data-service applies the changes."""
    dev, auto = _automation()
    if not auto:
        return Response({'error': 'Kein Avalon A15 gefunden.'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'POST':
        action = request.data.get('action')
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
            # Automatik aus: no clock is sent, the clock stays as it is; already off: no entry; ends a running test
            events = [('automatik_aus', 'Nutzer: Automatik beendet (Takt bleibt, wie er ist)', {})] if auto.enabled else []
            if auto.temp_test_at:
                events.append(_test_end('Automatik aus'))
            auto.enabled, auto.temp_test_at = False, None
        elif action == 'maintenance_start' and not auto.maintenance_at:
            # Wartung: the A15 is switched off by hand; a running Takt-Messung is paused, a running test ends
            from .models import CalibrationRun
            auto.maintenance_at = timezone.now()
            CalibrationRun.objects.filter(device=dev, status='running').update(
                status='paused', pause_reason='wartung', message='Pausiert (Wartung).', updated_at=timezone.now())
            events = [('wartung_start', 'Nutzer: Wartung gestartet', {})]
            if auto.temp_test_at:
                events.append(_test_end('Wartung'))
            auto.temp_test_at = None
        elif action == 'maintenance_end':
            events = [('wartung_ende', 'Nutzer: Wartung beendet', {})] if auto.maintenance_at else []
            auto.maintenance_at = None
        elif action == 'temp_test_start':
            refusal = _temp_test_refusal(dev, auto)
            if refusal:
                return Response({'error': refusal}, status=status.HTTP_409_CONFLICT)
            auto.temp_test_at = timezone.now()                    # the data-service runs the stages from here
            events = [('test_start', 'Nutzer: Temperatur-Test gestartet', {})]
        elif action == 'temp_test_abort':
            # no test (already over): no entry
            events = [_test_end('Nutzer')] if auto.temp_test_at else []
            auto.temp_test_at = None
        else:
            return Response({'error': 'Diese Aktion ist gerade nicht möglich.'}, status=status.HTTP_400_BAD_REQUEST)
        auto.updated_at = timezone.now()
        fields = ['enabled', 'ac_dev_id', 'ac_port', 'level', 'level_at', 'maintenance_at', 'updated_at']
        if action in TEST_ACTIONS:
            fields.append('temp_test_at')     # other actions never write back a test the data-service just ended
        auto.save(update_fields=fields)
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
