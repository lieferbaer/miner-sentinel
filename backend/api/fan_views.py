"""Lüfter (AC Infinity) test page: show controllers/ports, back up settings, test which write variant works."""
import time

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import acinfinity as ac

READ_BACK_S = 10      # the controller reports a new speed only after > 5 s


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
    except ac.NotActive:
        devs = []                     # test environment without an active test session: no AC Infinity access
    except ac.ACError as exc:
        return _error(exc)
    return Response({'configured': True, 'devices': devs, 'backups': ac.list_backups(),
                     'session': ac.session_info()})


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def fans_session(request):
    """Test environment only. GET: session state. POST {action: activate|end}."""
    if request.method == 'POST':
        action = request.data.get('action')
        try:
            if action == 'activate':
                ac.activate_test()
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
