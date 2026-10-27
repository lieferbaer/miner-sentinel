import logging
from datetime import timedelta

import pandas as pd
from django.contrib.auth import authenticate, login, logout
from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
import json
import re
import socket

from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import (
    AlertEvent,
    CalibrationPoint,
    CalibrationRun,
    CollectorSettings,
    Device,
    DeviceHardwareStats,
    DeviceMiningStats,
    DeviceSystemInfo,
    FanAutomation,
    PoolStats,
)
from .serializers import (
    AlertEventSerializer,
    CollectorSettingsSerializer,
    DeviceSerializer,
    DeviceWriteSerializer,
)
from .unified_serializers import (
    UnifiedHardwareSerializer,
    UnifiedMiningSerializer,
    UnifiedPoolSerializer,
    UnifiedSystemSerializer,
)
from .analytics_unified import detailed_analytics, overview_analytics
from .time_window import MAX_DAYS as _MAX_DAYS
from .time_window import MAX_HOURS as _MAX_HOURS
from .time_window import parse_int_param as _parse_int_param
from .time_window import parse_time_window

logger = logging.getLogger(__name__)



A15_LEVEL_LABELS = {4: 'Power Mode 04', 3: 'Power Mode 03', 2: 'Power Mode 02', 1: 'Power Mode 01', 0: 'Power Mode 00'}


def _a15_conf(ip):
    """Pool/mode/power-level settings from the A15 web interface (readable without login)."""
    import urllib.request
    raw = urllib.request.urlopen('http://%s/updatecgconf.cgi' % ip, timeout=5).read().decode('utf-8', 'replace')
    match = re.search(r'CGConfCallback\((\{.*?\})\);', raw, re.S)
    return json.loads(match.group(1)) if match else None


def _a15_set_level(ip, level):
    """Power Mode + power level via the web interface form (same POST as its Save button)."""
    import time
    import urllib.parse
    import urllib.request
    conf = _a15_conf(ip)
    if conf is None:
        return False
    if conf.get('mode') == 2 and conf.get('power_level') == level:
        return True
    fields = {k: conf.get(k, '') for k in ('pool1', 'worker1', 'passwd1', 'pool2', 'worker2', 'passwd2',
                                           'pool3', 'worker3', 'passwd3')}
    fields['mode'] = 2
    fields['power_level'] = '%02d' % level
    req = urllib.request.Request('http://%s/cgconf.cgi' % ip, data=urllib.parse.urlencode(fields).encode(),
                                 method='POST')
    urllib.request.urlopen(req, timeout=10).read()
    for _ in range(10):
        time.sleep(2)
        conf = _a15_conf(ip)
        if conf and conf.get('mode') == 2 and conf.get('power_level') == level:
            return True
    return False


def _cgminer_command(ip_address, port, command, timeout=10):
    """Send a raw cgminer API command to a miner and return its reply text."""
    with socket.create_connection((ip_address, port or 4028), timeout) as sock:
        sock.sendall(command.encode())
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b''.join(chunks).decode('utf-8', 'ignore').replace('\x00', '').strip()


REBOOT_NO_REPLY = 'keine Antwort – Miner startet neu'


def _reboot_command(ip_address, port, timeout=8):
    """Send the reboot command. Not reachable (connect/send fails) raises OSError; once it is sent, a missing or broken
    reply means the miner is already restarting (Nutzer 27.10.: the A15 answered nothing → "timed out" was shown)."""
    with socket.create_connection((ip_address, port or 4028), timeout) as sock:
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

class DeviceViewSet(viewsets.ModelViewSet):
    """Unified device registry CRUD (sole device store after Release C)."""
    queryset = Device.objects.all().order_by('make', 'name')
    serializer_class = DeviceSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        make = self.request.query_params.get('make')
        if make:
            qs = qs.filter(make=make)
        active_only = self.request.query_params.get('active_only', 'false').lower() == 'true'
        if active_only:
            qs = qs.filter(is_active=True)
        return qs

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return DeviceWriteSerializer
        return DeviceSerializer


    @action(detail=True, methods=['post'], url_path='set-pool')
    def set_pool(self, request, pk=None):
        """Write the mining pool configuration to an Avalon device (cgminer setpool)."""
        device = self.get_object()
        if device.protocol != Device.PROTOCOL_CGMINER_TCP:
            return Response(
                {'error': 'Only cgminer TCP devices (Avalon) support remote pool configuration.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        pool_url = (request.data.get('pool_url') or '').strip()
        worker = (request.data.get('worker') or '').strip()
        if not pool_url or not worker:
            return Response(
                {'error': 'pool_url and worker are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        worker_password = (request.data.get('worker_password') or 'x').strip()
        miner_user = (request.data.get('miner_user') or 'root').strip()
        miner_password = (request.data.get('miner_password') or 'root').strip()
        reboot = request.data.get('reboot', True)

        for value in (pool_url, worker, worker_password, miner_user, miner_password):
            if ',' in value or '|' in value:
                return Response(
                    {'error': 'Values must not contain "," or "|" - the miner API uses them as separators.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        command = 'ascset|0,setpool,{},{},{},{},{}'.format(
            miner_user, miner_password, pool_url, worker, worker_password
        )
        try:
            reply = _cgminer_command(device.ip_address, device.port, command)
        except OSError as exc:
            return Response(
                {'error': 'Device unreachable: {}'.format(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        rebooted = False
        reboot_error = None
        if reboot:
            try:
                _cgminer_command(device.ip_address, device.port, 'ascset|0,reboot,0')
                rebooted = True
            except OSError as exc:
                reboot_error = str(exc)

        return Response({
            'status': 'ok',
            'device_reply': reply,
            'rebooted': rebooted,
            'reboot_error': reboot_error,
            'note': 'Avalon applies pool settings after a reboot.',
        })

    @action(detail=True, methods=['get'], url_path='pools')
    def pools(self, request, pk=None):
        """Read the pools currently configured on the device."""
        device = self.get_object()
        if device.protocol != Device.PROTOCOL_CGMINER_TCP:
            return Response({'pools': []})
        try:
            raw = _cgminer_command(device.ip_address, device.port, json.dumps({'command': 'pools'}))
            payload = json.loads(raw)
        except (OSError, ValueError) as exc:
            return Response(
                {'error': 'Could not read pools: {}'.format(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        pools = [
            {
                'index': entry.get('POOL'),
                'url': entry.get('URL'),
                'user': entry.get('User'),
                'status': entry.get('Status'),
                'priority': entry.get('Priority'),
                'accepted': entry.get('Accepted'),
                'rejected': entry.get('Rejected'),
                'stratum_active': entry.get('Stratum Active'),
            }
            for entry in payload.get('POOLS', [])
        ]
        return Response({'pools': pools})

    @action(detail=False, methods=['get'], url_path='system-latest')
    def system_latest(self, request):
        """Latest live system values of all active devices (small payload for the overview)."""
        keys = ('work_mode', 'work_level', 'power_target_watts', 'temp_target', 'fan_speed_percent',
                'chip_temp_boards', 'system_status', 'error_rate_pct')
        out = {}
        for device in Device.objects.filter(is_active=True):
            row = (DeviceSystemInfo.objects.filter(device=device).order_by('-recorded_at')
                   .values('details').first())
            values = (row or {}).get('details') or {}
            out[device.device_id] = dict({k: values.get(k) for k in keys}, id=device.id, name=device.name)
        return Response(out)

    # ---- Takt-Tabellen (A15): measurement runs, executed by data-service/calibration.py ----
    CAL_MODES = [('2/L4', 'Power Mode 04'), ('2/L3', 'Power Mode 03'), ('2/L2', 'Power Mode 02'),
                 ('2/L1', 'Power Mode 01'), ('2/L0', 'Power Mode 00'),
                 ('1/-', 'High Performance')]
    CAL_ACTIVE = ('running', 'paused', 'cancelling', 'finishing')
    CAL_RUNNING = ('running', 'cancelling', 'finishing')      # paused: throttling is allowed

    @staticmethod
    def _cal_point(p):
        data = p.data or {}
        fields = dict(data.get('fields') or {})
        for key, value in (data.get('summary') or {}).items():
            fields.setdefault('summary.' + key, value)
        return {'id': p.id, 'run': p.run_id, 'kind': p.kind, 'mode_key': p.mode_key, 'mode_label': p.mode_label,
                'seq': p.seq, 'set_mhz': p.set_mhz, 'set_points': p.set_points, 'miner_mhz': p.miner_mhz,
                'miner_points': p.miner_points, 'hashrate_ths': p.hashrate_ths, 'power_w': p.power_w,
                'j_th': p.j_th, 'dh_pct': p.dh_pct, 'chip_temp_c': p.chip_temp_c, 'recorded_at': p.recorded_at,
                'fan_level': data.get('fan_level'), 'power_spread_pct': data.get('power_spread_pct'),
                'fields': fields}

    def _cal_run(self, run):
        labels = dict(self.CAL_MODES)
        st = run.state or {}
        set_points = st.get('set_points') or []
        return {
            'id': run.id, 'kind': run.kind, 'status': run.status, 'pause_reason': run.pause_reason,
            'active': run.status in self.CAL_ACTIVE,
            'modes': [{'key': k, 'label': labels.get(k, k)} for k in run.modes or []],
            'mode_index': run.mode_index, 'step_index': run.step_index, 'message': run.message,
            'phase': st.get('phase'), 'stable': bool(st.get('stable')), 'count': st.get('count', 0),
            'steps_total': st.get('steps_total'),
            'set_mhz': round(sum(set_points) / len(set_points), 1) if set_points else None,
            'notes': st.get('notes') or {}, 'seen_at': st.get('seen_at'), 'last_error': st.get('last_error'),
            'changed_at': st.get('changed_at'), 'server_now': timezone.now(),
            'created_at': run.created_at, 'finished_at': run.finished_at,
        }

    @action(detail=True, methods=['get', 'post'], url_path='calibration')
    def calibration(self, request, pk=None):
        """GET ?mode=<key>: runs and measured points. POST {action: start_max|start_step|pause|resume|cancel}."""
        device = self.get_object()
        if self._throttle_family(device) != 'a15' or device.protocol != Device.PROTOCOL_CGMINER_TCP:
            return Response({'error': 'Takt-Tabellen gibt es nur für den A15.'}, status=status.HTTP_400_BAD_REQUEST)
        if request.method == 'POST':
            return self._calibration_command(device, request.data)
        keys = [k for k, _ in self.CAL_MODES]
        mode = request.query_params.get('mode')
        mode = mode if mode in keys else keys[0]
        run = CalibrationRun.objects.filter(device=device).order_by('-created_at').first()

        # max values: per mode the points of the latest run that measured it
        max_points, max_notes, seen = [], {}, {}
        for p in CalibrationPoint.objects.filter(device=device, kind='max').order_by('-recorded_at'):
            seen.setdefault(p.mode_key, p.run_id)
            if seen[p.mode_key] == p.run_id:
                max_points.append(p)
        max_points.sort(key=lambda p: (keys.index(p.mode_key) if p.mode_key in keys else 99, p.seq))
        for r in CalibrationRun.objects.filter(device=device, kind='max').order_by('created_at'):
            max_notes.update((r.state or {}).get('notes') or {})

        last = CalibrationPoint.objects.filter(device=device, kind='step', mode_key=mode).order_by('-recorded_at').first()
        step_points = list(CalibrationPoint.objects.filter(run_id=last.run_id, kind='step', mode_key=mode)
                           .order_by('seq')) if last else []
        step_run = CalibrationRun.objects.filter(pk=last.run_id).first() if last else None
        manual = CalibrationPoint.objects.filter(device=device, kind='manual').order_by('-recorded_at')[:50]
        return Response({
            'device': {'id': device.id, 'name': device.name},
            'modes': [{'key': k, 'label': lbl, 'stock_mhz': (((device.connection_config or {}).get('throttle') or {})
                       .get('profiles') or {}).get(k, {}).get('avg_mhz')} for k, lbl in self.CAL_MODES],
            'run': self._cal_run(run) if run else None,
            'max': [self._cal_point(p) for p in max_points],
            'max_notes': max_notes,
            'step_mode': mode,
            'step': [self._cal_point(p) for p in step_points],
            'step_run': {'id': step_run.id, 'created_at': step_run.created_at, 'status': step_run.status}
            if step_run else None,
            'manual': [self._cal_point(p) for p in manual],
        })

    def _calibration_command(self, device, data):
        action_name = data.get('action')
        keys = [k for k, _ in self.CAL_MODES]
        run = CalibrationRun.objects.filter(device=device, status__in=self.CAL_ACTIVE).order_by('-created_at').first()
        now = timezone.now()
        if action_name in ('start_max', 'start_step'):
            if run:
                return Response({'error': 'Es läuft bereits eine Messung.'}, status=status.HTTP_409_CONFLICT)
            if action_name == 'start_max':
                modes = [k for k in keys if k in (data.get('modes') or [])]
                if not modes:
                    return Response({'error': 'Bitte mindestens einen Modus wählen.'},
                                    status=status.HTTP_400_BAD_REQUEST)
            else:
                if data.get('mode') not in keys:
                    return Response({'error': 'Unbekannter Modus.'}, status=status.HTTP_400_BAD_REQUEST)
                modes = [data.get('mode')]
            failed = self._cal_fan_automation_on(device) or self._cal_lift_throttle(device)
            if failed:
                return failed
            run = CalibrationRun.objects.create(device=device, kind=action_name[6:], modes=modes, status='running',
                                                state={}, message='Gestartet.', created_at=now, updated_at=now)
        elif action_name == 'pause' and run and run.status == 'running':
            run.status, run.pause_reason, run.message = 'paused', 'user', 'Pausiert.'
        elif action_name == 'resume' and run and run.status == 'paused':
            # first the throttle set during the pause is lifted, then the last point is measured again
            failed = self._cal_fan_automation_on(device) or self._cal_lift_throttle(device)
            if failed:
                return failed
            st = dict(run.state or {})
            st['resume'] = True
            run.status, run.pause_reason, run.state = 'running', '', st
            run.message = 'Wird fortgesetzt: letzter gespeicherter Punkt wird neu gemessen.'
        elif action_name == 'cancel' and run and run.status != 'cancelling':
            run.status, run.message = 'cancelling', 'Wird abgebrochen …'
        else:
            return Response({'error': 'Diese Aktion ist gerade nicht möglich.'}, status=status.HTTP_400_BAD_REQUEST)
        run.updated_at = now
        run.save()
        return Response(self._cal_run(run))

    @staticmethod
    def _cal_fan_automation_on(device):
        """A run needs the Temperatur-Automatik (the fan holds 80 degC): switched on here if it is off."""
        auto = FanAutomation.objects.filter(device=device).first()
        if not auto or not auto.ac_dev_id:
            return Response({'error': 'Erst im Menü „Lüfter“ den Port wählen und die Temperatur-Automatik '
                                      'einmal einschalten.'}, status=status.HTTP_409_CONFLICT)
        if not auto.enabled:
            auto.enabled, auto.updated_at = True, timezone.now()
            auto.save(update_fields=['enabled', 'updated_at'])
        return None

    def _cal_lift_throttle(self, device):
        """A running throttle is lifted before a measurement starts (the run lets the firmware set the clocks)."""
        st = json.loads(json.dumps((device.connection_config or {}).get('throttle') or {}))
        active = st.get('active')
        if not active:
            return None
        # no clocks are set here: the measurement switches the mode via the web interface, so the
        # firmware applies its own clocks
        st['active'] = None
        st['last_event'] = {'type': 'calibration', 'detail': 'für die Takt-Messung aufgehoben',
                            'at': timezone.now().isoformat(), 'target_ths': active.get('target_ths'),
                            'clock_mhz': active.get('clock_mhz')}
        st['stable_since'] = None
        self._throttle_save(device, st)
        return None

    @action(detail=True, methods=['post'], url_path='restart')
    def restart(self, request, pk=None):
        """POST: reboot an Avalon miner (offered when it reports "Over Hot")."""
        device = self.get_object()
        if device.protocol != Device.PROTOCOL_CGMINER_TCP:
            return Response({'error': 'Neustart ist nur für Avalon-Miner möglich.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            reply = _reboot_command(device.ip_address, device.port)
        except OSError as exc:
            return Response({'error': 'Miner nicht erreichbar: {}'.format(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        # the A15 of the Temperatur-Automatik: user event in its protocol (Nutzer 25.10.)
        from .fan_views import _automation, _user_event
        dev, auto = _automation()
        if dev and dev.id == device.id:
            _user_event(dev, auto, 'neustart_nutzer', 'Nutzer: A15-Neustart gesendet', target='miner',
                        endpoint='cgminer %s:%s' % (device.ip_address, device.port or 4028),
                        request='ascset|0,reboot,0', response=reply[:300], ok=True)
        return Response({'status': 'ok', 'device_reply': reply[:300]})

    # ---- Throttle: scale the Avalon PLL frequencies to a target hashrate ----
    # The data-service (throttle_controller.py) measures full power per work mode,
    # detects when a throttle was lifted on the miner and regulates towards the target.
    # One profile per work mode (key = mode_key, e.g. "1/0" or "2/L4" for the A15).
    THROTTLE_MIN_SHARE = 0.2   # lowest allowed target: 20 % of the measured full power
    THROTTLE_FLOOR = 0.15      # never set a PLL below 15 % of its stock clock
    THROTTLE_FAMILY_MODES = {
        'nano': {0: 'Low', 1: 'Medium', 2: 'High'},
        'q': {0: 'Eco', 1: 'Standard', 2: 'Super'},
    }
    THROTTLE_A15_MODES = {1: 'High Performance'}   # WORKMODE 2 = Power Mode with levels 00-04

    def _throttle_live(self, device):
        live = self._throttle_live_raw(device)
        family = self._throttle_family(device)
        wm = live['workmode']
        if family == 'a15' and wm == 2:
            try:
                conf = _a15_conf(device.ip_address)
            except (OSError, ValueError):
                conf = None
            if not conf or conf.get('mode') != 2:
                raise OSError('A15-Webinterface antwortet nicht (Stufe nicht lesbar)')
            level = conf['power_level']
            live['mode_key'] = '2/L%d' % level
            live['workmode'] = level
            live['mode_label'] = A15_LEVEL_LABELS.get(level, 'Level %02d' % level)
        elif family == 'a15' and wm is not None:
            live['mode_key'] = '%d/-' % wm
            live['workmode'] = None
            live['mode_label'] = self.THROTTLE_A15_MODES.get(wm, 'Mode %d' % wm)
        elif wm is not None:
            names = self.THROTTLE_FAMILY_MODES.get(family) or {}
            live['mode_label'] = names.get(wm, 'Mode %d' % wm)
        return live

    def _throttle_live_raw(self, device):
        help_reply = _cgminer_command(device.ip_address, device.port,
                                      json.dumps({'command': 'ascset', 'parameter': '0,help'}), timeout=6)
        est = _cgminer_command(device.ip_address, device.port, json.dumps({'command': 'estats'}), timeout=8)
        summ = _cgminer_command(device.ip_address, device.port, json.dumps({'command': 'summary'}), timeout=6)
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
        wm, wl = num('WORKMODE'), num('WORKLEVEL')
        key = ('-' if wm is None else str(int(wm))) + '/' + ('-' if wl is None else str(int(wl)))
        points = boards[sorted(boards)[0]] if boards else None
        return {
            'workmode': int(wm) if wm is not None else None,
            'supported': 'frequency' in help_reply and bool(points),
            'points': points, 'mode_key': key, 'mode_label': 'unbekannt',
            'ghsspd': num('GHSspd'), 'mhs5': summary.get('MHS 5m'),
        }

    @staticmethod
    def _throttle_seen(device):
        """What the throttle controller last read from the miner (it polls every 15 s)."""
        st = (device.connection_config or {}).get('throttle') or {}
        seen = st.get('last_seen') or {}
        if not seen.get('points'):
            return None
        return {
            'points': seen['points'], 'mode_key': seen.get('mode_key'),
            'mode_label': seen.get('mode_label') or 'unbekannt', 'workmode': seen.get('option'),
            'ghsspd': seen['ghsspd_ths'] * 1000.0 if seen.get('ghsspd_ths') else None,
            'supported': bool(st.get('freq_ok')), 'seen_at': seen.get('at'),
        }

    @staticmethod
    def _throttle_clock_for(prof, target):
        """Average clock for `target` TH/s from the measured points of this mode (full power and
        throttles reached earlier, prof['cal']); same rule as the controller's next_clock:
        straight line between the nearest points below and above the target, otherwise through
        the two closest points, with a single point proportional."""
        pts = [(float(prof['avg_mhz']), prof['measured_max_ths'])] + [tuple(p) for p in prof.get('cal') or []]
        pts = sorted({round(x, 1): y for x, y in pts if x > 0 and y > 0}.items())
        below = [p for p in pts if p[1] < target]
        above = [p for p in pts if p[1] > target]
        if below and above:
            (x1, y1), (x2, y2) = max(below, key=lambda p: p[1]), min(above, key=lambda p: p[1])
        else:
            near = sorted(pts, key=lambda p: abs(p[1] - target))[:2]
            if len(near) < 2:
                return near[0][0] * target / near[0][1]
            (x1, y1), (x2, y2) = near
        if (x2 - x1) * (y2 - y1) <= 0 or y1 == y2:
            x0, y0 = min(pts, key=lambda p: abs(p[1] - target))
            return x0 * target / y0
        return x1 + (target - y1) * (x2 - x1) / (y2 - y1)

    @staticmethod
    def _throttle_family(device):
        text = ' '.join(str(getattr(device, k, '') or '') for k in ('name', 'model', 'device_id')).lower()
        if 'nano' in text:
            return 'nano'
        if 'a15' in text or '15-' in text:
            return 'a15'
        if re.search(r'(^|\W)q(\W|$)', text) or 'avalonq' in text:
            return 'q'
        return None

    THROTTLE_MODE_OPTIONS = {
        'nano': [{'value': 0, 'label': 'Low'}, {'value': 1, 'label': 'Medium'}, {'value': 2, 'label': 'High'}],
        'q': [{'value': 0, 'label': 'Eco'}, {'value': 1, 'label': 'Standard'}, {'value': 2, 'label': 'Super'}],
        'a15': [{'value': lv, 'label': A15_LEVEL_LABELS[lv]} for lv in (4, 3, 2, 1, 0)],
    }

    @action(detail=True, methods=['post'], url_path='workmode')
    def workmode(self, request, pk=None):
        """POST {mode}: switch the work mode (Nano 3S, Avalon Q) or the power level (A15)."""
        device = self.get_object()
        family = self._throttle_family(device)
        options = self.THROTTLE_MODE_OPTIONS.get(family)
        if device.protocol != Device.PROTOCOL_CGMINER_TCP or not options:
            return Response({'error': 'Für dieses Gerät nur im Webinterface des Miners möglich.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            mode = int(request.data.get('mode'))
        except (TypeError, ValueError):
            mode = -1
        if mode not in [o['value'] for o in options]:
            return Response({'error': 'Unbekannter Modus.'}, status=status.HTTP_400_BAD_REQUEST)
        if CalibrationRun.objects.filter(device=device, status__in=self.CAL_ACTIVE).exists():
            return Response({'error': 'Takt-Messung läuft – Moduswechsel ist gesperrt.'}, status=status.HTTP_409_CONFLICT)
        if family == 'a15':
            try:
                ok = _a15_set_level(device.ip_address, mode)
            except (OSError, ValueError) as exc:
                return Response({'error': 'A15 nicht erreichbar: {}'.format(exc)}, status=status.HTTP_502_BAD_GATEWAY)
            if not ok:
                return Response({'error': 'Der A15 hat die Stufe nicht übernommen.'}, status=status.HTTP_502_BAD_GATEWAY)
            reply = 'power_level %02d' % mode
        else:
            reply = self._workmode_cgminer(device, mode)
            if isinstance(reply, Response):
                return reply
        config = dict(device.connection_config or {})
        st = dict(config.get('throttle') or {})
        st['last_event'] = None
        st['stable_since'] = None
        st['rev'] = int(st.get('rev', 0) or 0) + 1
        self._throttle_keep_measured(device, st)
        config['throttle'] = st
        device.connection_config = config
        device.save(update_fields=['connection_config'])
        label = [o['label'] for o in options if o['value'] == mode][0]
        return Response({'status': 'ok', 'mode': mode, 'label': label, 'device_reply': reply[:300]})

    def _workmode_cgminer(self, device, mode):
        command = json.dumps({'command': 'ascset', 'parameter': '0,workmode,set,%d' % mode})
        try:
            reply = _cgminer_command(device.ip_address, device.port, command, timeout=8)
        except OSError as exc:
            return Response({'error': 'Miner nicht erreichbar: {}'.format(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if '"STATUS":"E"' in reply.replace(' ', ''):
            return Response({'error': 'Der Miner hat den Modus abgelehnt.', 'device_reply': reply[:300]},
                            status=status.HTTP_502_BAD_GATEWAY)
        return reply

    @staticmethod
    def _throttle_resolve(profiles, live, active):
        """(profile key, profile) for the current state: the active throttle's profile, else the
        profile of the work mode the miner reports."""
        pkey = active.get('profile') if active else live['mode_key']
        return pkey, profiles.get(pkey) or {}

    def _throttle_payload(self, device, live):
        st = (device.connection_config or {}).get('throttle') or {}
        profiles = st.get('profiles') or {}
        active = st.get('active')
        points = live['points']
        cur_avg = sum(points) / len(points)
        pkey, prof = self._throttle_resolve(profiles, live, active)
        max_ths = prof.get('measured_max_ths')
        from django.utils.dateparse import parse_datetime
        seen_at = parse_datetime(live['seen_at']) if live.get('seen_at') else None
        stock_avg = prof.get('avg_mhz')
        auto = FanAutomation.objects.filter(device=device).first()
        set_mhz, set_by = self._last_set_clock(device, auto)
        return {
            'supported': True,
            'set_mhz': set_mhz,
            'set_by': set_by,
            'automation_active': bool(auto and auto.enabled),
            'calibration_paused': CalibrationRun.objects.filter(device=device, status='paused').exists(),
            'min_mhz': round(stock_avg * self.THROTTLE_FLOOR) if stock_avg else None,
            'max_mhz': round(stock_avg) if stock_avg else None,
            'calibration_active': CalibrationRun.objects.filter(device=device, status__in=self.CAL_ACTIVE).exists(),
            'seen_at': live.get('seen_at'),
            'stale': bool(seen_at and (timezone.now() - seen_at).total_seconds() > 60),
            'mode_label': live['mode_label'] if not active else (prof.get('mode_label') or live['mode_label']),
            'current_avg_mhz': round(cur_avg, 1),
            'stock_avg_mhz': prof.get('avg_mhz'),
            'max_ths': max_ths,
            'max_source': 'measured' if max_ths else None,
            'min_ths': round(max_ths * self.THROTTLE_MIN_SHARE, 2) if max_ths else None,
            'hashrate_ths': round(live['ghsspd'] / 1000.0, 2) if live['ghsspd'] else None,
            'throttled': bool(active),
            'active': {
                'clock_mhz': active.get('clock_mhz'),
                'manual_measured_at': active.get('manual_measured_at'),
                'target_ths': active.get('target_ths'),
                'status': active.get('status'),
                'measured_ths': active.get('measured_ths'),
                'estimate_ths': active.get('estimate_ths'),
                'reached_at': active.get('reached_at'),
                'set_at': active.get('set_at'),
                'last_change_at': active.get('last_change_at'),
                'steps': active.get('steps', 0),
            } if active else None,
            'last_event': st.get('last_event'),
            'profile_ready': bool(prof),
            'current_profile': pkey,
            'modes': [
                {'key': key, 'label': p.get('mode_label') or key, 'avg_mhz': p.get('avg_mhz'),
                 'max_ths': p.get('measured_max_ths'), 'power_w': p.get('measured_power_w'),
                 'efficiency_j_th': p.get('efficiency_j_th'), 'measured_at': p.get('measured_at')}
                for key, p in profiles.items()
            ],
            'mode_options': self.THROTTLE_MODE_OPTIONS.get(self._throttle_family(device)),
            'current_workmode': live.get('workmode'),
            'family': self._throttle_family(device),
        }

    def _last_set_clock(self, device, auto):
        """(MHz, by) of the clock MinerSentinel set last on the A15 - Takt-Messung or Temperatur-Automatik,
        whichever was latest; (None, None) if the firmware's own clocks are in effect. (The A15 has no
        Drosselung since 24.10.)"""
        from django.utils.dateparse import parse_datetime
        candidates = []
        run = CalibrationRun.objects.filter(device=device).order_by('-created_at').first()
        rst = (run.state or {}) if run else {}
        if run and run.status in self.CAL_ACTIVE:
            if run.kind == 'step' and rst.get('set_points') and rst.get('phase') in ('measure', 'set_clock'):
                points = rst['set_points']
                candidates.append((rst.get('changed_at'), round(sum(points) / len(points), 1), 'Takt-Messung'))
            else:
                candidates.append((rst.get('changed_at'), None, 'Takt-Messung'))    # mode switch: firmware clocks
        elif run and run.finished_at:
            candidates.append((run.finished_at.isoformat(), None, 'Takt-Messung'))  # run ended: firmware clocks
        ast = (auto.state or {}) if auto else {}
        if ast.get('clock_changed_at') and (auto.enabled or self._auto_clock_kept(device, ast)):
            # also when the automation is off: its clock stays as it is (Automatik aus sends no clock)
            candidates.append((ast['clock_changed_at'], ast.get('clock_set'), 'Automatik'))
        dated = [(parse_datetime(at), mhz, by) for at, mhz, by in candidates if at and parse_datetime(at)]
        if not dated:
            return None, None
        _, mhz, by = max(dated, key=lambda c: c[0])
        return (mhz, by) if mhz is not None else (None, None)

    AUTO_OFF_TOL_MHZ = 25      # as OVERRIDE_MHZ of the data-service: the miner runs another clock
    AUTO_OWN_SWITCH_S = 120    # as OWN_SWITCH_S of the data-service: a restart this soon follows its own mode switch

    @staticmethod
    def _auto_clock_kept(device, ast):
        """Automatik aus: the data-service no longer reads the miner, so its clock counts only while the miner
        (as the throttle controller last read it) still runs it - not after a restart or a mode switch."""
        from django.utils.dateparse import parse_datetime
        seen = ((device.connection_config or {}).get('throttle') or {}).get('last_seen') or {}
        points, clock = seen.get('points'), ast.get('clock_set')
        if points and clock and abs(sum(points) / len(points) - clock) > DeviceViewSet.AUTO_OFF_TOL_MHZ:
            return False
        if seen.get('mode_key') and ast.get('mode_key') and seen['mode_key'] != ast['mode_key']:
            return False                                        # mode switched since: its stock clock
        try:
            changed, at = parse_datetime(ast['clock_changed_at']), parse_datetime(seen.get('at') or '')
        except (TypeError, ValueError):
            return True
        elapsed = seen.get('elapsed')
        if changed and at and isinstance(elapsed, (int, float)):
            booted = at - timedelta(seconds=elapsed)
            if booted > changed + timedelta(seconds=DeviceViewSet.AUTO_OWN_SWITCH_S):
                return False                                    # restarted since: the firmware's own clocks
        return True

    @staticmethod
    def _throttle_keep_measured(device, st):
        """Never drop measured/learned profiles the controller wrote meanwhile."""
        fresh = Device.objects.filter(pk=device.pk).values_list('connection_config', flat=True).first() or {}
        fresh_profiles = (fresh.get('throttle') or {}).get('profiles') or {}
        mine = st.setdefault('profiles', {})
        for key, prof in fresh_profiles.items():
            have = mine.get(key)
            if have is None or (prof.get('measured_at') or '') > (have.get('measured_at') or ''):
                mine[key] = prof

    def _throttle_save(self, device, st):
        config = dict(device.connection_config or {})
        st['rev'] = int(st.get('rev', 0) or 0) + 1
        self._throttle_keep_measured(device, st)
        config['throttle'] = st
        device.connection_config = config
        device.save(update_fields=['connection_config'])

    @action(detail=True, methods=['get', 'post'], url_path='throttle')
    def throttle(self, request, pk=None):
        """GET: throttle state (A15: display only). POST {target_ths} or {reset: true} - Nano 3S and Avalon Q;
        the Drosselung of the A15 is abolished (24.10.), its clock is set by the Temperatur-Automatik."""
        device = self.get_object()
        if device.protocol != Device.PROTOCOL_CGMINER_TCP:
            return Response({'supported': False})
        if request.method == 'GET':
            # state as read by the throttle controller; no miner access when only displaying
            seen = self._throttle_seen(device)
            if not seen or not seen['supported']:
                return Response({'supported': False})
            return Response(self._throttle_payload(device, seen))
        if self._throttle_family(device) == 'a15':
            return Response({'error': 'Die Drosselung des A15 ist abgeschafft; den Takt regelt die '
                                      'Temperatur-Automatik.'}, status=status.HTTP_400_BAD_REQUEST)
        # a running Takt-Messung blocks
        if CalibrationRun.objects.filter(device=device, status__in=self.CAL_RUNNING).exists():
            return Response({'error': 'Takt-Messung läuft – Drosseln ist gesperrt.'}, status=status.HTTP_409_CONFLICT)
        try:
            live = self._throttle_live(device)
        except OSError as exc:
            return Response({'error': 'Miner nicht erreichbar: {}'.format(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if not live['supported']:
            return Response({'supported': False})

        st = json.loads(json.dumps((device.connection_config or {}).get('throttle') or {}))
        profiles = st.get('profiles') or {}
        active = st.get('active')
        pkey, prof = self._throttle_resolve(profiles, live, active)
        if not prof.get('points'):
            return Response({'error': 'Dieser Modus ist noch nicht erfasst – bitte in einer Minute erneut versuchen.'},
                            status=status.HTTP_409_CONFLICT)
        stock = prof['points']
        now = timezone.now().isoformat()

        if request.data.get('reset'):
            target_points = stock
            st['active'] = None
            st['last_event'] = {'type': 'reset', 'detail': 'Drosselung aufgehoben', 'at': now,
                                'target_ths': active.get('target_ths') if active else None,
                                'clock_mhz': active.get('clock_mhz') if active else None}
        else:
            max_ths = prof.get('measured_max_ths')
            if not max_ths:
                return Response({'error': 'Dieser Modus ist noch nicht gemessen. Drosseln ist möglich, sobald das '
                                          'Gerät 15 Minuten ungedrosselt in diesem Modus gelaufen ist.'},
                                status=status.HTTP_409_CONFLICT)
            min_ths = max_ths * self.THROTTLE_MIN_SHARE
            try:
                target = float(request.data.get('target_ths'))
            except (TypeError, ValueError):
                return Response({'error': 'Bitte eine Zahl in TH/s angeben.'}, status=status.HTTP_400_BAD_REQUEST)
            if target < min_ths or target > max_ths:
                return Response({'error': 'Erlaubt sind {:.1f}–{:.1f} TH/s.'.format(min_ths, max_ths)},
                                status=status.HTTP_400_BAD_REQUEST)
            want_avg = self._throttle_clock_for(prof, target)
            factor = want_avg / (sum(stock) / len(stock))
            target_points = [min(max(int(round(p * factor)), int(p * self.THROTTLE_FLOOR)), p) for p in stock]
            st['active'] = {'target_ths': round(target, 2), 'profile': pkey, 'applied_points': target_points,
                            'set_at': now, 'last_change_at': now, 'status': 'adjusting', 'steps': 0}
            st['last_event'] = None

        command = json.dumps({'command': 'ascset',
                              'parameter': '0,frequency,{}-0-0'.format(':'.join(str(v) for v in target_points))})
        try:
            reply = _cgminer_command(device.ip_address, device.port, command, timeout=8)
        except OSError as exc:
            return Response({'error': 'Miner nicht erreichbar: {}'.format(exc)},
                            status=status.HTTP_502_BAD_GATEWAY)
        if 'set OK' not in reply:
            return Response({'error': 'Der Miner hat den Wert abgelehnt.', 'device_reply': reply},
                            status=status.HTTP_502_BAD_GATEWAY)
        if st.get('active'):
            # the firmware ramps the clock over several seconds; the controller adopts
            # the settled values once this window has passed
            st['active']['applied_points'] = target_points
            st['active']['pending_until'] = (timezone.now() + timedelta(seconds=60)).isoformat()
        last_seen = dict(st.get('last_seen') or {})
        last_seen.update({'mode_key': live['mode_key'], 'mode_label': live['mode_label'],
                          'points': live['points'], 'throttled': bool(st.get('active'))})
        st['last_seen'] = last_seen
        st['stable_since'] = None          # a measurement must not include the throttled period
        self._throttle_save(device, st)
        return Response(self._throttle_payload(device, live))

    @action(detail=True, methods=['get'], url_path='details')
    def details(self, request, pk=None):
        """Full device detail payload (mining, hardware, system, trends for selected window)."""
        device = self.get_object()
        latest_mining = DeviceMiningStats.objects.filter(device=device).first()
        latest_hardware = DeviceHardwareStats.objects.filter(device=device).first()
        latest_system = DeviceSystemInfo.objects.filter(device=device).first()
        window = parse_time_window(request.query_params)
        time_filter = window.as_filter()
        hashrate_trend = list(
            DeviceMiningStats.objects.filter(device=device, **time_filter)
            .values('recorded_at', 'hashrate_ghs', 'shares_accepted', 'shares_rejected')
            .order_by('recorded_at')
        )
        temp_trend = list(
            DeviceHardwareStats.objects.filter(device=device, **time_filter)
            .values('recorded_at', 'temperature_c', 'power_watts', 'fan_speed_rpm')
            .order_by('recorded_at')
        )

        return Response({
            'device': DeviceSerializer(device).data,
            'make': device.make,
            'latest_mining': UnifiedMiningSerializer(latest_mining).data if latest_mining else None,
            'latest_hardware': UnifiedHardwareSerializer(latest_hardware).data if latest_hardware else None,
            'latest_system': UnifiedSystemSerializer(latest_system).data if latest_system else None,
            # Keep stable key names for frontend detail charts
            'hashrate_trend_24h': hashrate_trend,
            'temperature_trend_24h': temp_trend,
            'trend_hours': window.hours,
            'window_start': window.start.isoformat(),
            'window_end': window.end.isoformat(),
        })


class FleetMiningViewSet(viewsets.ReadOnlyModelViewSet):
    """All-device mining stats from unified tables."""
    serializer_class = UnifiedMiningSerializer

    def get_queryset(self):
        qs = DeviceMiningStats.objects.all().select_related('device')
        make = self.request.query_params.get('make')
        if make:
            qs = qs.filter(device__make=make)
        device_id = self.request.query_params.get('device_id')
        if device_id:
            qs = qs.filter(device__device_id=device_id)
        params = self.request.query_params
        if any(params.get(k) not in (None, '') for k in ('hours', 'days', 'from', 'to')):
            window = parse_time_window(params)
            qs = qs.filter(**window.as_filter())
        return qs

    @action(detail=False, methods=['get'])
    def latest(self, request):
        devices = Device.objects.filter(is_active=True)
        make = request.query_params.get('make')
        if make:
            devices = devices.filter(make=make)
        results = []
        for device in devices:
            latest = DeviceMiningStats.objects.filter(device=device).first()
            if not latest:
                continue
            data = UnifiedMiningSerializer(latest).data
            data['device_type'] = device.make
            data['device_id_str'] = device.device_id
            data['device_name'] = device.name
            data['difficulty'] = latest.best_difficulty
            results.append(data)
        return Response(results)


class FleetHardwareViewSet(viewsets.ReadOnlyModelViewSet):
    """All-device hardware stats from unified tables."""
    serializer_class = UnifiedHardwareSerializer

    def get_queryset(self):
        qs = DeviceHardwareStats.objects.all().select_related('device')
        make = self.request.query_params.get('make')
        if make:
            qs = qs.filter(device__make=make)
        device_id = self.request.query_params.get('device_id')
        if device_id:
            qs = qs.filter(device__device_id=device_id)
        params = self.request.query_params
        if any(params.get(k) not in (None, '') for k in ('hours', 'days', 'from', 'to')):
            window = parse_time_window(params)
            qs = qs.filter(**window.as_filter())
        return qs

    @action(detail=False, methods=['get'])
    def latest(self, request):
        devices = Device.objects.filter(is_active=True)
        make = request.query_params.get('make')
        if make:
            devices = devices.filter(make=make)
        results = []
        for device in devices:
            latest = DeviceHardwareStats.objects.filter(device=device).first()
            if not latest:
                continue
            data = UnifiedHardwareSerializer(latest).data
            data['device_type'] = device.make
            data['device_id_str'] = device.device_id
            data['device_name'] = device.name
            results.append(data)
        return Response(results)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def device_details_by_make_id(request, make, device_id):
    """Device details lookup by make + device_id (for frontend routes)."""
    try:
        device = Device.objects.get(make=make, device_id=device_id)
    except Device.DoesNotExist:
        return Response({'detail': 'Device not found'}, status=status.HTTP_404_NOT_FOUND)

    view = DeviceViewSet()
    view.request = request
    view.format_kwarg = None
    view.kwargs = {'pk': device.pk}
    view.action = 'details'
    return view.details(request, pk=device.pk)


class PoolStatsViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Pool statistics API (/api/pool/).
    Reads unified pool_stats; supports pool_type + pool_address filters.
    """
    serializer_class = UnifiedPoolSerializer

    def get_queryset(self):
        queryset = PoolStats.objects.all()
        pool_address = self.request.query_params.get('pool_address')
        if pool_address:
            queryset = queryset.filter(pool_address=pool_address)
        pool_type = self.request.query_params.get('pool_type')
        if pool_type:
            queryset = queryset.filter(pool_type=pool_type)
        params = self.request.query_params
        if any(params.get(k) not in (None, '') for k in ('hours', 'days', 'from', 'to')):
            window = parse_time_window(params)
            queryset = queryset.filter(**window.as_filter())
        return queryset

    @action(detail=False, methods=['get'])
    def latest(self, request):
        pool_address = request.query_params.get('pool_address')
        pool_type = request.query_params.get('pool_type')
        qs = PoolStats.objects.all().order_by('-recorded_at')
        if pool_address:
            qs = qs.filter(pool_address=pool_address)
        if pool_type:
            qs = qs.filter(pool_type=pool_type)
        latest_stat = qs.first()
        if latest_stat:
            return Response(self.get_serializer(latest_stat).data)
        return Response({'detail': 'No pool statistics found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['get'])
    def hashrate_trend(self, request):
        pool_address = request.query_params.get('pool_address')
        pool_type = request.query_params.get('pool_type')
        window = parse_time_window(request.query_params)
        queryset = PoolStats.objects.filter(**window.as_filter())
        if pool_address:
            queryset = queryset.filter(pool_address=pool_address)
        if pool_type:
            queryset = queryset.filter(pool_type=pool_type)
        stats = queryset.values(
            'recorded_at', 'hashrate_1m_display', 'hashrate_5m_display',
            'hashrate_1h_display', 'hashrate_1d_display',
            'hashrate_1m_ghs', 'hashrate_1d_ghs', 'shares', 'workers', 'best_share',
            'pool_type', 'pool_address',
        ).order_by('recorded_at')
        out = [
            {
                'recorded_at': row['recorded_at'],
                'hashrate_1m': row['hashrate_1m_display'],
                'hashrate_5m': row['hashrate_5m_display'],
                'hashrate_1hr': row['hashrate_1h_display'],
                'hashrate_1d': row['hashrate_1d_display'],
                'hashrate_1m_ghs': row['hashrate_1m_ghs'],
                'hashrate_1d_ghs': row['hashrate_1d_ghs'],
                'shares': row['shares'],
                'workers': row['workers'],
                'bestshare': row['best_share'],
                'best_share': row['best_share'],
                'pool_type': row['pool_type'],
                'pool_address': row['pool_address'],
            }
            for row in stats
        ]
        return Response(out)

    @action(detail=False, methods=['get'])
    def statistics(self, request):
        pool_address = request.query_params.get('pool_address')
        pool_type = request.query_params.get('pool_type')
        window = parse_time_window(request.query_params)
        queryset = PoolStats.objects.filter(**window.as_filter())
        if pool_address:
            queryset = queryset.filter(pool_address=pool_address)
        if pool_type:
            queryset = queryset.filter(pool_type=pool_type)

        if not queryset.exists():
            return Response({
                'total_shares': 0,
                'avg_workers': 0,
                'max_hashrate_ghs': 0,
                'best_share': 0,
                'data_points': 0,
            })

        stats = queryset.aggregate(
            latest_shares=Max('shares'),
            avg_workers=Avg('workers'),
            max_hashrate_ghs=Max('hashrate_1m_ghs'),
            best_share=Max('best_share'),
            data_points=Count('id'),
        )
        first_record = queryset.order_by('recorded_at').first()
        latest_record = queryset.order_by('-recorded_at').first()
        if first_record and latest_record and first_record.shares is not None and latest_record.shares is not None:
            stats['total_shares'] = latest_record.shares - first_record.shares
        else:
            stats['total_shares'] = 0
        return Response(stats)


# Authentication Views
@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    """
    Authenticate user and create session.
    Expects: {"username": "...", "password": "..."}
    """
    username = request.data.get('username')
    password = request.data.get('password')

    if not username or not password:
        return Response(
            {'error': 'Username and password required'},
            status=status.HTTP_400_BAD_REQUEST
        )

    user = authenticate(request, username=username, password=password)

    if user is not None:
        login(request, user)
        return Response({
            'success': True,
            'user': {
                'id': user.id,
                'username': user.username,
                'email': user.email,
            }
        })
    else:
        return Response(
            {'error': 'Invalid credentials'},
            status=status.HTTP_401_UNAUTHORIZED
        )


@api_view(['POST'])
def logout_view(request):
    """Logout user and destroy session."""
    logout(request)
    return Response({'success': True})


@api_view(['GET'])
@permission_classes([AllowAny])
def current_user_view(request):
    """Get current authenticated user info."""
    if request.user.is_authenticated:
        return Response({
            'authenticated': True,
            'user': {
                'id': request.user.id,
                'username': request.user.username,
                'email': request.user.email,
            }
        })
    else:
        return Response({'authenticated': False})


@api_view(['GET'])
@permission_classes([AllowAny])
def csrf_token_view(request):
    """
    Return CSRF token for the client.
    This endpoint ensures the CSRF cookie is set.
    """
    from django.middleware.csrf import get_token
    csrf_token = get_token(request)
    return Response({'csrfToken': csrf_token})


# Data Collector Settings Views
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def collector_settings_view(request):
    """
    GET: Get current data collector settings from database.
    POST: Update data collector settings in database.
    The data collector service fetches settings from the database independently.
    """
    if request.method == 'GET':
        try:
            # Get settings from database
            settings = CollectorSettings.get_settings()
            settings_data = CollectorSettingsSerializer(settings).data
            return Response(settings_data)

        except Exception as e:
            logger.error(f"Error getting collector settings: {e}")
            return Response({
                'error': str(e),
                'polling_interval_minutes': 15,
                'device_check_interval_minutes': 5,
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    elif request.method == 'POST':
        try:
            # Get or create settings instance
            settings = CollectorSettings.get_settings()

            # Update with provided data
            serializer = CollectorSettingsSerializer(settings, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                logger.info(f"Collector settings updated: {serializer.data}")

                return Response({
                    'success': True,
                    'message': 'Settings saved successfully. Changes will take effect on the next polling cycle.',
                    'settings': serializer.data,
                })
            else:
                return Response({
                    'success': False,
                    'errors': serializer.errors,
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f"Error saving collector settings: {e}")
            return Response({
                'success': False,
                'error': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



def _weighted(rows, key, weight):
    """Mean of hourly means weighted with the number of samples of each hour (None if there are none)."""
    total = sum(r[weight] for r in rows if r[key] is not None and r[weight])
    if not total:
        return None
    return sum(r[key] * r[weight] for r in rows if r[key] is not None and r[weight]) / total


def _hourly_device_view(device, window, granularity):
    """Analytics of one device from device_stats_hourly (periods over 24 h, Nutzer 26.10.): the same series and
    aggregates as from the raw values, but from 24 rows per day instead of ~8,600."""
    from .models import DeviceStatsHourly
    start = window.start.replace(minute=0, second=0, microsecond=0)
    # whole hours: from the hour of the start, up to the hour before the end (the hour starting at the end is after it)
    rows = list(DeviceStatsHourly.objects.filter(device=device, hour__gte=start, hour__lt=window.end)
                .order_by('hour').values())
    groups = {}
    for row in rows:
        key = row['hour'].replace(hour=0) if granularity == 'day' else row['hour']
        groups.setdefault(key, []).append(row)
    series = [{
        't': key.isoformat(),
        'hashrate_ghs': _weighted(group, 'hashrate_avg', 'mining_samples'),
        'temperature_c': _weighted(group, 'temperature_avg', 'temperature_n'),
        'temperature_chip_c': _weighted(group, 'chip_temperature_avg', 'chip_temperature_n'),
        'power_watts': _weighted(group, 'power_avg', 'power_n'),
    } for key, group in sorted(groups.items())]

    def pick(fn, key):
        values = [r[key] for r in rows if r[key] is not None]
        return fn(values) if values else None
    mining_agg = {
        'avg_hashrate': _weighted(rows, 'hashrate_avg', 'mining_samples'),
        'max_hashrate': pick(max, 'hashrate_max'), 'min_hashrate': pick(min, 'hashrate_min'),
        'best_difficulty': pick(max, 'best_difficulty'),
        'first_accepted': pick(min, 'shares_accepted_min'), 'last_accepted': pick(max, 'shares_accepted_max'),
        'first_rejected': pick(min, 'shares_rejected_min'), 'last_rejected': pick(max, 'shares_rejected_max'),
        'samples': sum(r['mining_samples'] for r in rows),
    }
    hardware_agg = {
        'avg_temp': _weighted(rows, 'temperature_avg', 'temperature_n'),
        'max_temp': pick(max, 'temperature_max'),
        'avg_chip': _weighted(rows, 'chip_temperature_avg', 'chip_temperature_n'),
        'max_chip': pick(max, 'chip_temperature_max'),
        'avg_power': _weighted(rows, 'power_avg', 'power_n'),
        'max_power': pick(max, 'power_max'),
        'avg_efficiency': _weighted(rows, 'efficiency_avg', 'efficiency_n'),
        'samples': sum(r['hardware_samples'] for r in rows),
    }
    return series, mining_agg, hardware_agg, (pick(min, 'first_at'), pick(max, 'last_at'))


@api_view(['GET'])
def device_analytics(request):
    """Per-device analytics for a time window.

    Granularity adapts to the window: raw samples up to one hour, hourly means
    up to seven days, daily means beyond that - so a 30 day view stays fast
    even with a 10 second collection interval.
    """
    from django.db.models import Avg, Max, Min, Count
    from django.db.models.functions import TruncDay, TruncHour

    window = parse_time_window(request.query_params)
    time_filter = window.as_filter()
    hours = window.hours or 24

    if hours <= 1:
        granularity = 'raw'
        trunc = None
    elif hours <= 24 * 7:
        granularity = 'hour'
        trunc = TruncHour
    else:
        granularity = 'day'
        trunc = TruncDay

    settings_obj = CollectorSettings.get_settings()
    energy_rate = float(settings_obj.energy_rate or 0)
    currency = settings_obj.energy_currency or 'EUR'
    btc_price = float(settings_obj.cached_btc_price or 0)

    devices = list(Device.objects.all().order_by('name'))
    payload_devices = []
    comparison = []

    # over 24 h from the hourly values (the raw values are kept only 30 days; Nutzer 26.10.)
    use_hourly = hours > 24

    for device in devices:
        mining_qs = DeviceMiningStats.objects.filter(device=device, **time_filter)
        hardware_qs = DeviceHardwareStats.objects.filter(device=device, **time_filter)
        span = None

        if use_hourly:
            series, mining_agg, hardware_agg, span = _hourly_device_view(device, window, granularity)
        else:
            if granularity == 'raw':
                mining_rows = list(
                    mining_qs.order_by('recorded_at').values('recorded_at', 'hashrate_ghs')
                )
                hardware_rows = list(
                    hardware_qs.order_by('recorded_at').values(
                        'recorded_at', 'temperature_c', 'temperature_chip_c', 'power_watts'
                    )
                )
                series = []
                hardware_by_time = {row['recorded_at']: row for row in hardware_rows}
                for row in mining_rows:
                    hardware = hardware_by_time.get(row['recorded_at'], {})
                    series.append({
                        't': row['recorded_at'].isoformat(),
                        'hashrate_ghs': row['hashrate_ghs'],
                        'temperature_c': hardware.get('temperature_c'),
                        'temperature_chip_c': hardware.get('temperature_chip_c'),
                        'power_watts': hardware.get('power_watts'),
                    })
                if not series:
                    for row in hardware_rows:
                        series.append({
                            't': row['recorded_at'].isoformat(),
                            'hashrate_ghs': None,
                            'temperature_c': row['temperature_c'],
                            'temperature_chip_c': row['temperature_chip_c'],
                            'power_watts': row['power_watts'],
                        })
            else:
                mining_buckets = (
                    mining_qs.annotate(bucket=trunc('recorded_at'))
                    .values('bucket')
                    .annotate(hashrate_ghs=Avg('hashrate_ghs'), samples=Count('id'))
                    .order_by('bucket')
                )
                hardware_buckets = (
                    hardware_qs.annotate(bucket=trunc('recorded_at'))
                    .values('bucket')
                    .annotate(
                        temperature_c=Avg('temperature_c'),
                        temperature_chip_c=Avg('temperature_chip_c'),
                        power_watts=Avg('power_watts'),
                    )
                    .order_by('bucket')
                )
                hardware_by_bucket = {row['bucket']: row for row in hardware_buckets}
                series = []
                for row in mining_buckets:
                    hardware = hardware_by_bucket.pop(row['bucket'], {})
                    series.append({
                        't': row['bucket'].isoformat(),
                        'hashrate_ghs': row['hashrate_ghs'],
                        'temperature_c': hardware.get('temperature_c'),
                        'temperature_chip_c': hardware.get('temperature_chip_c'),
                        'power_watts': hardware.get('power_watts'),
                    })
                for bucket, hardware in sorted(hardware_by_bucket.items()):
                    series.append({
                        't': bucket.isoformat(),
                        'hashrate_ghs': None,
                        'temperature_c': hardware.get('temperature_c'),
                        'temperature_chip_c': hardware.get('temperature_chip_c'),
                        'power_watts': hardware.get('power_watts'),
                    })
                series.sort(key=lambda item: item['t'])

            mining_agg = mining_qs.aggregate(
                avg_hashrate=Avg('hashrate_ghs'),
                max_hashrate=Max('hashrate_ghs'),
                min_hashrate=Min('hashrate_ghs'),
                best_difficulty=Max('best_difficulty'),
                first_accepted=Min('shares_accepted'),
                last_accepted=Max('shares_accepted'),
                first_rejected=Min('shares_rejected'),
                last_rejected=Max('shares_rejected'),
                samples=Count('id'),
            )
            hardware_agg = hardware_qs.aggregate(
                avg_temp=Avg('temperature_c'),
                max_temp=Max('temperature_c'),
                avg_chip=Avg('temperature_chip_c'),
                max_chip=Max('temperature_chip_c'),
                avg_power=Avg('power_watts'),
                max_power=Max('power_watts'),
                avg_efficiency=Avg('efficiency_j_per_th'),
                samples=Count('id'),
            )

        avg_power = hardware_agg['avg_power'] or 0
        energy_kwh = (avg_power / 1000.0) * hours
        energy_cost = energy_kwh * energy_rate
        accepted_delta = max(
            0, (mining_agg['last_accepted'] or 0) - (mining_agg['first_accepted'] or 0)
        )
        rejected_delta = max(
            0, (mining_agg['last_rejected'] or 0) - (mining_agg['first_rejected'] or 0)
        )
        shares_total = accepted_delta + rejected_delta

        expected_samples = None
        availability = None
        if mining_agg['samples']:
            if span:
                first, last = span
            else:
                first = mining_qs.order_by('recorded_at').values_list('recorded_at', flat=True).first()
                last = mining_qs.order_by('-recorded_at').values_list('recorded_at', flat=True).first()
            if first and last and last > first:
                covered = (last - first).total_seconds()
                availability = min(100.0, (covered / (hours * 3600.0)) * 100.0)

        summary = {
            'avg_hashrate_ghs': mining_agg['avg_hashrate'],
            'max_hashrate_ghs': mining_agg['max_hashrate'],
            'min_hashrate_ghs': mining_agg['min_hashrate'],
            'best_difficulty': mining_agg['best_difficulty'],
            'shares_accepted': accepted_delta,
            'shares_rejected': rejected_delta,
            'rejection_rate': round((rejected_delta / shares_total) * 100, 2) if shares_total else 0,
            'avg_temperature_c': hardware_agg['avg_temp'],
            'max_temperature_c': hardware_agg['max_temp'],
            'avg_chip_temperature_c': hardware_agg['avg_chip'],
            'max_chip_temperature_c': hardware_agg['max_chip'],
            'avg_power_watts': avg_power,
            'max_power_watts': hardware_agg['max_power'],
            'avg_efficiency_j_per_th': hardware_agg['avg_efficiency'],
            'energy_kwh': round(energy_kwh, 3),
            'energy_cost': round(energy_cost, 2),
            'availability_percent': round(availability, 1) if availability is not None else None,
            'samples': mining_agg['samples'],
            'expected_samples': expected_samples,
        }

        config = device.connection_config or {}
        pool_config = config.get('pool_stats') or {}
        worker_name = (pool_config.get('worker') or '').strip()
        pool_view = None
        if pool_config.get('address'):
            latest_pool = (
                PoolStats.objects.filter(
                    pool_type=pool_config.get('type'), pool_address=pool_config.get('address')
                )
                .order_by('-recorded_at')
                .first()
            )
            if latest_pool:
                details = latest_pool.details or {}
                worker_rows = details.get('workers') or []
                match = None
                for entry in worker_rows:
                    name = str(entry.get('name') or '')
                    if worker_name and name.lower() == worker_name.lower():
                        match = entry
                        break
                if match is None and len(worker_rows) == 1 and not worker_name:
                    match = worker_rows[0]
                pool_view = {
                    'pool_type': latest_pool.pool_type,
                    'account': latest_pool.pool_address,
                    'recorded_at': latest_pool.recorded_at.isoformat(),
                    'worker': match.get('name') if match else worker_name or None,
                    'worker_hashrate_ghs': match.get('hash_rate_ghs') if match else None,
                    'worker_status': match.get('status') if match else None,
                    'last_share_at': match.get('last_share_at') if match else None,
                    'matched': bool(match),
                    'available_workers': [str(entry.get('name')) for entry in worker_rows],
                }

        payload_devices.append({
            'device': DeviceSerializer(device).data,
            'summary': summary,
            'pool': pool_view,
            'series': series,
        })
        comparison.append({
            'device_id': device.device_id,
            'device_name': device.name,
            'make': device.make,
            'model': device.model,
            'avg_hashrate_ghs': summary['avg_hashrate_ghs'],
            'avg_power_watts': summary['avg_power_watts'],
            'avg_efficiency_j_per_th': summary['avg_efficiency_j_per_th'],
            'max_chip_temperature_c': summary['max_chip_temperature_c'],
            'energy_kwh': summary['energy_kwh'],
            'energy_cost': summary['energy_cost'],
            'rejection_rate': summary['rejection_rate'],
            'availability_percent': summary['availability_percent'],
            'best_difficulty': summary['best_difficulty'],
        })

    accounts = []
    seen_accounts = set()
    for device in devices:
        pool_config = (device.connection_config or {}).get('pool_stats') or {}
        address = pool_config.get('address')
        pool_type = pool_config.get('type')
        if not address or (pool_type, address) in seen_accounts:
            continue
        seen_accounts.add((pool_type, address))
        latest_pool = (
            PoolStats.objects.filter(pool_type=pool_type, pool_address=address)
            .order_by('-recorded_at')
            .first()
        )
        if not latest_pool:
            continue
        details = latest_pool.details or {}
        balance = details.get('balance') or {}
        accounts.append({
            'pool_type': latest_pool.pool_type,
            'account': latest_pool.pool_address,
            'recorded_at': latest_pool.recorded_at.isoformat(),
            'hashrate_ghs': latest_pool.hashrate_1m_ghs,
            'hashrate_1h_ghs': latest_pool.hashrate_1h_ghs,
            'hashrate_1d_ghs': latest_pool.hashrate_1d_ghs,
            'workers': latest_pool.workers,
            'balance_btc': balance.get('balance'),
            'paid_btc': balance.get('paid'),
            'total_income_btc': balance.get('total_income'),
            'yesterday_income_btc': balance.get('yesterday_income'),
            'estimated_today_income_btc': balance.get('estimated_today_income'),
            'immature_balance_btc': balance.get('immature_balance'),
        })

    return Response({
        'window': {
            'start': window.start.isoformat(),
            'end': window.end.isoformat(),
            'hours': hours,
            'granularity': granularity,
        },
        'money': {
            'currency': currency,
            'btc_price': btc_price,
            'energy_rate': energy_rate,
            'price_updated_at': (
                settings_obj.network_data_updated_at.isoformat()
                if settings_obj.network_data_updated_at
                else None
            ),
        },
        'accounts': accounts,
        'devices': payload_devices,
        'comparison': comparison,
    })

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def refresh_network_data(request):
    """
    Fetch and cache latest Bitcoin price and network hashrate from public APIs.
    """
    import requests
    from django.utils import timezone as tz

    try:
        settings = CollectorSettings.get_settings()

        btc_price = None
        network_hashrate = None
        network_difficulty = None
        errors = []

        # Fetch BTC price from CoinGecko (free, no API key required)
        try:
            price_response = requests.get(
                'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,eur',
                timeout=10
            )
            if price_response.ok:
                price_data = price_response.json()
                prices = price_data.get('bitcoin', {})
                currency = (settings.energy_currency or 'EUR').lower()
                btc_price = prices.get(currency) or prices.get('eur') or prices.get('usd')
                if btc_price:
                    settings.cached_btc_price = btc_price
                    logger.info(f"Updated BTC price: {btc_price} {settings.energy_currency}")
        except Exception as e:
            errors.append(f"Failed to fetch BTC price: {str(e)}")
            logger.warning(f"Failed to fetch BTC price: {e}")

        # Fetch network stats from mempool.space (free, no API key required)
        try:
            hashrate_response = requests.get(
                'https://mempool.space/api/v1/mining/hashrate/3d',
                timeout=10
            )
            if hashrate_response.ok:
                hashrate_data = hashrate_response.json()
                # Get latest hashrate (in H/s), convert to EH/s
                if hashrate_data.get('currentHashrate'):
                    network_hashrate = hashrate_data['currentHashrate'] / 1e18  # Convert to EH/s
                    settings.cached_network_hashrate = network_hashrate
                    logger.info(f"Updated network hashrate: {network_hashrate:.2f} EH/s")
                if hashrate_data.get('currentDifficulty'):
                    network_difficulty = hashrate_data['currentDifficulty']
                    settings.cached_network_difficulty = network_difficulty
                    logger.info(f"Updated network difficulty: {network_difficulty}")
        except Exception as e:
            errors.append(f"Failed to fetch network hashrate: {str(e)}")
            logger.warning(f"Failed to fetch network hashrate: {e}")

        # Update timestamp
        settings.network_data_updated_at = tz.now()
        settings.save()

        return Response({
            'success': True,
            'message': 'Network data refreshed',
            'data': {
                'btc_price': float(settings.cached_btc_price),
                'network_hashrate_ehs': float(settings.cached_network_hashrate),
                'network_difficulty': float(settings.cached_network_difficulty),
                'updated_at': settings.network_data_updated_at.isoformat() if settings.network_data_updated_at else None,
            },
            'errors': errors if errors else None,
        })

    except Exception as e:
        logger.error(f"Error refreshing network data: {e}")
        return Response({
            'success': False,
            'error': str(e),
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_network_data(request):
    """
    Get cached network data (BTC price, hashrate, difficulty).
    """
    try:
        settings = CollectorSettings.get_settings()

        return Response({
            'btc_price': float(settings.cached_btc_price),
            'network_hashrate_ehs': float(settings.cached_network_hashrate),
            'network_difficulty': float(settings.cached_network_difficulty),
            'updated_at': settings.network_data_updated_at.isoformat() if settings.network_data_updated_at else None,
        })

    except Exception as e:
        logger.error(f"Error getting network data: {e}")
        return Response({
            'error': str(e),
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_collector_poll(request):
    """
    Trigger a manual data collection poll.
    """
    import os

    import requests

    data_service_url = os.environ.get('DATA_SERVICE_URL', 'http://data-service:5000')

    try:
        response = requests.post(f'{data_service_url}/poll', timeout=60)
        if response.ok:
            return Response({
                'success': True,
                'message': 'Poll cycle triggered successfully',
                'result': response.json()
            })
        return Response(
            {'error': 'Failed to trigger poll'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Error triggering poll: {e}")
        return Response(
            {'error': 'Data collector service unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_telegram_notification(request):
    """Send a test Telegram message using stored credentials."""
    import requests as http_requests

    settings = CollectorSettings.get_settings()
    token = (settings.telegram_bot_token or '').strip()
    chat_id = (settings.telegram_chat_id or '').strip()
    # Allow one-off token/chat from body without persisting
    body = request.data or {}
    if body.get('telegram_bot_token'):
        token = str(body['telegram_bot_token']).strip()
    if body.get('telegram_chat_id'):
        chat_id = str(body['telegram_chat_id']).strip()

    if not token or not chat_id:
        return Response(
            {'success': False, 'error': 'Telegram bot token and chat ID are required'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not settings.telegram_enabled and not body.get('force'):
        return Response(
            {
                'success': False,
                'error': 'Telegram channel is disabled. Enable it or pass force=true to test.',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        resp = http_requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={
                'chat_id': chat_id,
                'text': (
                    '✅ <b>MinerSentinel test</b>\n\n'
                    'Telegram notifications are working correctly.'
                ),
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            },
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.ok and data.get('ok'):
            return Response({'success': True, 'message': 'Test message sent to Telegram'})
        return Response(
            {
                'success': False,
                'error': data.get('description') or f'Telegram API error ({resp.status_code})',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"Telegram test failed: {e}", exc_info=True)
        return Response(
            {'success': False, 'error': str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_discord_notification(request):
    """Send a test Discord webhook embed using stored credentials."""
    import requests as http_requests

    settings = CollectorSettings.get_settings()
    webhook = (settings.discord_webhook_url or '').strip()
    body = request.data or {}
    if body.get('discord_webhook_url'):
        webhook = str(body['discord_webhook_url']).strip()

    if not webhook:
        return Response(
            {'success': False, 'error': 'Discord webhook URL is required'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not settings.discord_enabled and not body.get('force'):
        return Response(
            {
                'success': False,
                'error': 'Discord channel is disabled. Enable it or pass force=true to test.',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        resp = http_requests.post(
            webhook,
            json={
                'username': 'MinerSentinel',
                'embeds': [
                    {
                        'title': '✅ MinerSentinel test',
                        'description': 'Discord webhook notifications are working correctly.',
                        'color': 3066993,
                    }
                ],
            },
            timeout=15,
        )
        if resp.status_code in (200, 204):
            return Response({'success': True, 'message': 'Test message sent to Discord'})
        return Response(
            {
                'success': False,
                'error': f'Discord webhook error ({resp.status_code}): {resp.text[:200]}',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"Discord test failed: {e}", exc_info=True)
        return Response(
            {'success': False, 'error': str(e)},
            status=status.HTTP_502_BAD_GATEWAY,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_push_notification(request):
    """Send a test message via ntfy / Gotify / generic webhook using stored settings."""
    import requests as http_requests

    settings = CollectorSettings.get_settings()
    body = request.data or {}
    channel = (body.get('channel') or 'auto').strip().lower()

    ntfy_url = (body.get('ntfy_url') or settings.ntfy_url or '').strip()
    ntfy_token = (body.get('ntfy_token') or settings.ntfy_token or '').strip()
    gotify_url = (body.get('gotify_url') or settings.gotify_url or '').strip().rstrip('/')
    gotify_token = (body.get('gotify_token') or settings.gotify_token or '').strip()
    webhook_url = (body.get('webhook_url') or settings.webhook_url or '').strip()

    ntfy_on = settings.ntfy_enabled or channel == 'ntfy' or body.get('force')
    gotify_on = settings.gotify_enabled or channel == 'gotify' or body.get('force')
    webhook_on = settings.webhook_enabled or channel == 'webhook' or body.get('force')

    if channel == 'ntfy':
        gotify_on = webhook_on = False
        ntfy_on = True
    elif channel == 'gotify':
        ntfy_on = webhook_on = False
        gotify_on = True
    elif channel == 'webhook':
        ntfy_on = gotify_on = False
        webhook_on = True

    sent = []
    errors = []

    if ntfy_on and ntfy_url:
        try:
            headers = {'Title': 'MinerSentinel test', 'Priority': '3', 'Tags': 'white_check_mark'}
            if ntfy_token:
                headers['Authorization'] = f'Bearer {ntfy_token}'
            resp = http_requests.post(
                ntfy_url,
                data=b'ntfy push notifications are working correctly for MinerSentinel.',
                headers=headers,
                timeout=15,
            )
            if resp.ok:
                sent.append('ntfy')
            else:
                errors.append(f'ntfy ({resp.status_code}): {resp.text[:160]}')
        except Exception as e:
            errors.append(f'ntfy: {e}')
    elif channel == 'ntfy':
        errors.append('ntfy topic URL is required')

    if gotify_on and gotify_url and gotify_token:
        try:
            resp = http_requests.post(
                f'{gotify_url}/message',
                params={'token': gotify_token},
                json={
                    'title': 'MinerSentinel test',
                    'message': 'Gotify notifications are working correctly for MinerSentinel.',
                    'priority': 3,
                },
                timeout=15,
            )
            if resp.ok:
                sent.append('gotify')
            else:
                errors.append(f'gotify ({resp.status_code}): {resp.text[:160]}')
        except Exception as e:
            errors.append(f'gotify: {e}')
    elif channel == 'gotify':
        errors.append('Gotify URL and token are required')

    if webhook_on and webhook_url:
        try:
            resp = http_requests.post(
                webhook_url,
                json={
                    'source': 'minersentinel',
                    'title': 'MinerSentinel test',
                    'message': 'Generic webhook notifications are working correctly.',
                    'severity': 'info',
                    'event_key': 'test',
                },
                timeout=15,
            )
            if resp.ok:
                sent.append('webhook')
            else:
                errors.append(f'webhook ({resp.status_code}): {resp.text[:160]}')
        except Exception as e:
            errors.append(f'webhook: {e}')
    elif channel == 'webhook':
        errors.append('Webhook URL is required')

    if not sent and not errors:
        return Response(
            {
                'success': False,
                'error': 'Enable ntfy, Gotify, or a webhook and provide a URL (or pass force=true).',
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if sent and not errors:
        return Response({'success': True, 'message': f'Test sent via {", ".join(sent)}'})
    if sent and errors:
        return Response(
            {
                'success': True,
                'message': f'Partial success via {", ".join(sent)}; errors: {"; ".join(errors)}',
            }
        )
    return Response(
        {'success': False, 'error': '; '.join(errors)},
        status=status.HTTP_400_BAD_REQUEST,
    )


# ---------------------------------------------------------------------------
# Device control — proxies to data-service LAN adapters
# ---------------------------------------------------------------------------

CONTROL_CAPABILITIES = {
    'bitaxe': {
        'reboot': True, 'fan': True, 'frequency': True, 'voltage': True,
        'pause': True, 'resume': True, 'set_pool': True, 'workmode': False,
    },
    'avalon': {
        'reboot': True, 'fan': True, 'frequency': False, 'voltage': False,
        'pause': False, 'resume': False, 'set_pool': False, 'workmode': True,
    },
    'nmaxe': {
        'reboot': True, 'fan': True, 'frequency': True, 'voltage': True,
        'pause': False, 'resume': False, 'set_pool': True, 'workmode': False,
    },
    'nerdnos': {
        'reboot': False, 'fan': False, 'frequency': False, 'voltage': False,
        'pause': False, 'resume': False, 'set_pool': False, 'workmode': False,
    },
}

CONTROL_CONFIRM_ACTIONS = frozenset({'reboot', 'set_pool', 'workmode'})


def _data_service_url():
    import os
    return os.environ.get('DATA_SERVICE_URL', 'http://data-service:5000')


def _log_control_event(device, action, success, result=None, error=None, user=None):
    """Persist control action to AlertEvent when the model is available."""
    event_type_map = {
        'reboot': 'user_reboot',
        'fan': 'fan_changed',
        'frequency': 'frequency_changed',
        'voltage': 'voltage_changed',
        'pause': 'mining_paused',
        'resume': 'mining_resumed',
        'set_pool': 'pool_changed',
        'workmode': 'workmode_changed',
    }
    event_type = event_type_map.get(action, f'control_{action}')
    severity = AlertEvent.SEVERITY_INFO if success else AlertEvent.SEVERITY_WARN
    msg = (
        f"{'OK' if success else 'FAILED'}: {action} on "
        f"{device.name} ({device.make}:{device.device_id})"
    )
    if error:
        msg = f"{msg} — {error}"
    payload = {
        'action': action,
        'success': success,
        'result': result if isinstance(result, dict) else {},
        'error': error,
        'user': getattr(user, 'username', None),
    }
    try:
        make = getattr(device, 'make', '') or ''
        key = getattr(device, 'device_id', '') or ''
        AlertEvent.objects.create(
            event_type=event_type,
            severity=severity,
            device=device,
            device_make=make,
            device_key=key,
            device_name=getattr(device, 'name', '') or '',
            message=msg,
            payload=payload,
            fingerprint=f"{event_type}:{make}:{key}"[:191],
        )
    except Exception as e:  # noqa: BLE001 — never fail control response on audit write
        # Keep control path resilient if schema lags
        logger.warning('Could not persist control AlertEvent: %s', e)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def device_capabilities(request, make, device_id):
    """Capability-gated control map for a device."""
    try:
        device = Device.objects.get(make=make, device_id=device_id)
    except Device.DoesNotExist:
        return Response({'detail': 'Device not found'}, status=status.HTTP_404_NOT_FOUND)

    caps = dict(CONTROL_CAPABILITIES.get(device.make, {}))
    if not caps:
        caps = {k: False for k in (
            'reboot', 'fan', 'frequency', 'voltage', 'pause', 'resume', 'set_pool', 'workmode',
        )}

    from .lib.devices_status import is_device_online_server
    online = is_device_online_server(device)

    return Response({
        'make': device.make,
        'device_id': device.device_id,
        'offline': not online,
        'capabilities': caps,
        'confirm_actions': sorted(CONTROL_CONFIRM_ACTIONS),
        'calibration_active': _calibration_active(device),
    })


def _calibration_active(device):
    """A Takt-Tabellen run is active on this device: the generic controls are locked meanwhile."""
    return CalibrationRun.objects.filter(device=device, status__in=DeviceViewSet.CAL_ACTIVE).exists()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def device_control(request, make, device_id):
    """
    POST /api/devices/{make}/{device_id}/control/
    Body: { "action": "reboot"|"fan"|..., "params": {...} }
    """
    import requests

    try:
        device = Device.objects.get(make=make, device_id=device_id)
    except Device.DoesNotExist:
        return Response({'detail': 'Device not found'}, status=status.HTTP_404_NOT_FOUND)

    body = request.data or {}
    action = str(body.get('action') or '').strip().lower()
    params = body.get('params') if isinstance(body.get('params'), dict) else {}

    if not action:
        return Response({'error': 'action is required'}, status=status.HTTP_400_BAD_REQUEST)

    caps = CONTROL_CAPABILITIES.get(device.make, {})
    if not caps.get(action):
        return Response(
            {'error': f'Action "{action}" is not supported for {device.make}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from .lib.devices_status import is_device_online_server
    if not is_device_online_server(device):
        return Response(
            {'error': 'Device appears offline — controls are disabled'},
            status=status.HTTP_409_CONFLICT,
        )
    if _calibration_active(device):
        return Response(
            {'error': 'Takt-Messung läuft – die Steuerung ist bis zum Ende der Messung gesperrt.'},
            status=status.HTTP_409_CONFLICT,
        )

    try:
        resp = requests.post(
            f'{_data_service_url()}/control',
            json={
                'make': device.make,
                'device_id': device.device_id,
                'action': action,
                'params': params,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        logger.error('Control proxy failed: %s', e)
        _log_control_event(device, action, False, error='data-service unavailable', user=request.user)
        return Response(
            {'error': 'Data collector service unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {'ok': False, 'error': resp.text[:300]}

    success = resp.ok and data.get('ok', False)
    _log_control_event(
        device,
        action,
        success,
        result=data if success else None,
        error=None if success else data.get('error') or f'HTTP {resp.status_code}',
        user=request.user,
    )

    if success:
        return Response(data)
    status_code = resp.status_code if resp.status_code >= 400 else status.HTTP_502_BAD_GATEWAY
    return Response(data, status=status_code)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def discover_devices_view(request):
    """Proxy LAN discovery to data-service."""
    import requests

    body = request.data or {}
    try:
        resp = requests.post(
            f'{_data_service_url()}/discover',
            json={
                'cidr': body.get('cidr'),
                'seed_ips': body.get('seed_ips'),
            },
            timeout=120,
        )
    except requests.exceptions.RequestException as e:
        logger.error('Discovery proxy failed: %s', e)
        return Response(
            {'error': 'Data collector service unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return Response({'error': 'Invalid discovery response'}, status=status.HTTP_502_BAD_GATEWAY)

    # Annotate which discovered IPs are already registered
    registered = {
        (d.make, d.ip_address): d
        for d in Device.objects.filter(is_active=True)
    }
    for item in data.get('devices') or []:
        key = (item.get('make'), item.get('ip_address'))
        existing = registered.get(key)
        item['already_registered'] = bool(existing)
        if existing:
            item['registered_device_id'] = existing.device_id
            item['registered_name'] = existing.name

    return Response(data, status=resp.status_code if not resp.ok else status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def advisor_view(request):
    """
    Ranked next-actions with one-click control hooks where possible.
    Lightweight Phase 3 advisor (does not replace Overview open-problems strip).
    """
    suggestions = []
    now = timezone.now()

    for device in Device.objects.filter(is_active=True).order_by('name'):
        caps = CONTROL_CAPABILITIES.get(device.make, {})
        age = None
        if device.last_seen_at:
            age = (now - device.last_seen_at).total_seconds()

        offline = bool(device.error_message) or age is None or (age is not None and age > 600)
        if offline and caps.get('reboot'):
            suggestions.append({
                'priority': 10 if age is None or age > 3600 else 20,
                'kind': 'device_offline',
                'title': f'{device.name} appears offline',
                'detail': device.error_message or 'No recent successful poll',
                'device': {'make': device.make, 'device_id': device.device_id, 'name': device.name},
                'actions': [
                    {
                        'label': 'Reboot',
                        'control': {'action': 'reboot', 'params': {}},
                        'confirm': True,
                    },
                ],
            })

        latest_hw = DeviceHardwareStats.objects.filter(device=device).first()
        if latest_hw and latest_hw.temperature_c and latest_hw.temperature_c >= 85 and caps.get('fan'):
            suggestions.append({
                'priority': 15,
                'kind': 'high_temp',
                'title': f'{device.name} at {latest_hw.temperature_c:.0f}°C',
                'detail': 'Raise fan speed to cool the miner',
                'device': {'make': device.make, 'device_id': device.device_id, 'name': device.name},
                'actions': [
                    {
                        'label': 'Fan 100%',
                        'control': {'action': 'fan', 'params': {'percent': 100, 'auto': False}},
                        'confirm': False,
                    },
                ],
            })

        if device.make == 'avalon' and caps.get('workmode') and not offline:
            suggestions.append({
                'priority': 80,
                'kind': 'avalon_workmode',
                'title': f'{device.name}: Avalon workmode',
                'detail': 'Switch Low / Mid / High power preset',
                'device': {'make': device.make, 'device_id': device.device_id, 'name': device.name},
                'actions': [
                    {'label': 'Low', 'control': {'action': 'workmode', 'params': {'mode': 0}}, 'confirm': True},
                    {'label': 'Mid', 'control': {'action': 'workmode', 'params': {'mode': 1}}, 'confirm': True},
                    {'label': 'High', 'control': {'action': 'workmode', 'params': {'mode': 2}}, 'confirm': True},
                ],
            })

    settings = CollectorSettings.get_settings()
    if not settings.telegram_enabled and not settings.discord_enabled:
        suggestions.append({
            'priority': 50,
            'kind': 'alerts_not_configured',
            'title': 'Alerts not configured',
            'detail': 'Enable Telegram or Discord in Settings → Notifications',
            'device': None,
            'actions': [],
            'href': '/settings',
        })

    suggestions.sort(key=lambda s: s['priority'])
    return Response({'suggestions': suggestions[:20]})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def export_inventory_csv(request):
    """Phase 4 foundation: export device inventory (+ optional alert history) as CSV."""
    import csv
    from io import StringIO

    from django.http import HttpResponse

    include_alerts = str(request.query_params.get('alerts', 'false')).lower() == 'true'
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'make', 'device_id', 'name', 'ip_address', 'port', 'protocol',
        'model', 'is_active', 'last_seen_at', 'error_message',
    ])
    for d in Device.objects.all().order_by('make', 'name'):
        writer.writerow([
            d.make, d.device_id, d.name, d.ip_address, d.port or '',
            d.protocol, d.model or '', d.is_active,
            d.last_seen_at.isoformat() if d.last_seen_at else '',
            (d.error_message or '').replace('\n', ' ')[:500],
        ])

    if include_alerts:
        writer.writerow([])
        writer.writerow(['event_type', 'severity', 'device_make', 'device_id', 'message', 'created_at', 'acknowledged_at'])
        for ev in AlertEvent.objects.select_related('device').order_by('-created_at')[:5000]:
            writer.writerow([
                ev.event_type,
                ev.severity,
                ev.device.make if ev.device else '',
                ev.device.device_id if ev.device else '',
                (ev.message or '').replace('\n', ' ')[:500],
                ev.created_at.isoformat() if ev.created_at else '',
                ev.acknowledged_at.isoformat() if ev.acknowledged_at else '',
            ])

    resp = HttpResponse(buf.getvalue(), content_type='text/csv')
    resp['Content-Disposition'] = 'attachment; filename="minersentinel-inventory.csv"'
    return resp


# Push channel status (ntfy / Gotify / webhook live; Web Push VAPID optional/deferred)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def push_channels_status(request):
    """Report configured push delivery channels (no secrets)."""
    settings = CollectorSettings.get_settings()
    ntfy_ready = bool(settings.ntfy_enabled and (settings.ntfy_url or '').strip())
    gotify_ready = bool(
        settings.gotify_enabled
        and (settings.gotify_url or '').strip()
        and (settings.gotify_token or '').strip()
    )
    webhook_ready = bool(settings.webhook_enabled and (settings.webhook_url or '').strip())
    return Response({
        'web_push': {
            'available': False,
            'status': 'deferred',
            'message': 'Browser Web Push (VAPID) deferred — use ntfy on Umbrel/LAN',
        },
        'ntfy': {
            'available': True,
            'status': 'ready' if ntfy_ready else 'configured' if (settings.ntfy_url or '').strip() else 'off',
            'enabled': settings.ntfy_enabled,
            'url_configured': bool((settings.ntfy_url or '').strip()),
            'token_configured': bool((settings.ntfy_token or '').strip()),
            'message': 'POST to topic URL (ntfy.sh or self-hosted)',
        },
        'gotify': {
            'available': True,
            'status': 'ready' if gotify_ready else 'off',
            'enabled': settings.gotify_enabled,
            'url_configured': bool((settings.gotify_url or '').strip()),
            'token_configured': bool((settings.gotify_token or '').strip()),
            'message': 'Gotify /message with app token',
        },
        'webhook': {
            'available': True,
            'status': 'ready' if webhook_ready else 'off',
            'enabled': settings.webhook_enabled,
            'url_configured': bool((settings.webhook_url or '').strip()),
            'message': 'Generic JSON POST {title, message, severity, event_key}',
        },
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def stratum_pool_compare(request):
    """
    Side-by-side: each device's primary/fallback stratum vs selected pool stats.
    Does not poll every pool type concurrently — uses configured pool_type + latest sample.
    """
    settings = CollectorSettings.get_settings()
    pool_type = settings.pool_type or 'ckpool'
    address_field = {
        'ckpool': 'ckpool_address',
        'publicpool': 'publicpool_address',
        'btcpowlab': 'btcpowlab_address',
        'parasite': 'parasite_address',
    }.get(pool_type, 'ckpool_address')
    configured_address = getattr(settings, address_field, '') or ''

    latest = PoolStats.objects.filter(pool_type=pool_type).order_by('-recorded_at').first()
    selected_pool = {
        'pool_type': pool_type,
        'configured_address': configured_address,
        'latest': UnifiedPoolSerializer(latest).data if latest else None,
    }

    devices = []
    for device in Device.objects.filter(is_active=True).order_by('make', 'name'):
        sysinfo = DeviceSystemInfo.objects.filter(device=device).order_by('-recorded_at').first()
        primary = (sysinfo.primary_pool_url if sysinfo else None) or ''
        fallback = (sysinfo.fallback_pool_url if sysinfo else None) or ''
        devices.append({
            'id': device.id,
            'device_id': device.device_id,
            'name': device.name,
            'make': device.make,
            'primary_pool_url': primary or None,
            'primary_pool_user': sysinfo.primary_pool_user if sysinfo else None,
            'fallback_pool_url': fallback or None,
            'fallback_pool_user': sysinfo.fallback_pool_user if sysinfo else None,
            'using_fallback': sysinfo.using_fallback_pool if sysinfo else None,
            'stratum_host': _stratum_host(primary),
            'matches_selected_pool_type': _stratum_hints_pool_type(primary, pool_type),
        })

    return Response({
        'selected_pool': selected_pool,
        'devices': devices,
    })


def _stratum_host(url: str) -> str:
    if not url:
        return ''
    raw = url.replace('stratum+tcp://', '').replace('stratum+ssl://', '').replace('stratum://', '')
    return raw.split('/')[0].split(':')[0].lower()


def _stratum_hints_pool_type(url: str, pool_type: str) -> bool | None:
    """Best-effort host keyword match; None when unknown."""
    host = _stratum_host(url)
    if not host:
        return None
    hints = {
        'ckpool': ('ckpool', 'solo.ckpool', 'eusolo'),
        'publicpool': ('public-pool', 'publicpool'),
        'btcpowlab': ('btcpowlab',),
        'parasite': ('parasite',),
    }
    keys = hints.get(pool_type) or ()
    return any(k in host for k in keys) if keys else None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def collector_status_view(request):
    """Proxy data-service /status for honest collector health in Settings + Overview."""
    import requests

    data_service_url = _data_service_url()
    try:
        response = requests.get(f'{data_service_url}/status', timeout=5)
        if response.ok:
            data = response.json()
            data['reachable'] = True
            return Response(data)
        return Response(
            {
                'reachable': False,
                'status': 'unreachable',
                'error': f'Data service returned {response.status_code}',
            },
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f'Collector status unreachable: {e}')
        return Response(
            {
                'reachable': False,
                'status': 'unreachable',
                'error': 'Data collector service unavailable',
                'last_success_at': None,
                'next_run': None,
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_events_list(request):
    """Activity / alert journal feed.

    kind=problem — warn/critical needing attention (default when open=true)
    kind=highlight — celebrations / info / control audits
    kind=all — everything
    """
    HIGHLIGHT_TYPES = ('best_difficulty', 'device_online')
    PROBLEM_TYPES = (
        'device_offline',
        'hashrate_stagnation',
        'auto_restart',
        'temperature_high',
        'fan_dead',
        'pool_down',
        'collector_down',
        'expected_hashrate_drop',
    )

    # One-time cleanup: leftover highlight rows must not linger as "open problems"
    AlertEvent.objects.filter(
        event_type__in=HIGHLIGHT_TYPES,
        acknowledged_at__isnull=True,
        resolved_at__isnull=True,
    ).update(acknowledged_at=timezone.now())

    qs = AlertEvent.objects.all().order_by('-created_at')
    open_only = str(request.query_params.get('open', '')).lower() in ('1', 'true', 'yes')
    kind = (request.query_params.get('kind') or '').lower()
    if not kind:
        kind = 'problem' if open_only else 'all'

    if open_only:
        qs = qs.filter(resolved_at__isnull=True, acknowledged_at__isnull=True)

    if kind == 'problem':
        qs = qs.filter(
            Q(severity__in=(AlertEvent.SEVERITY_WARN, AlertEvent.SEVERITY_CRITICAL))
            | Q(event_type__in=PROBLEM_TYPES)
        ).exclude(event_type__in=HIGHLIGHT_TYPES).exclude(event_type__startswith='user_')
    elif kind == 'highlight':
        qs = qs.filter(
            Q(event_type__in=HIGHLIGHT_TYPES)
            | Q(event_type__startswith='user_')
            | Q(severity=AlertEvent.SEVERITY_INFO)
        )

    severity = request.query_params.get('severity')
    if severity in ('info', 'warn', 'critical'):
        qs = qs.filter(severity=severity)

    event_type = request.query_params.get('event_type')
    if event_type:
        qs = qs.filter(event_type=event_type)

    device_make = request.query_params.get('make')
    device_key = request.query_params.get('device_id')
    if device_make:
        qs = qs.filter(Q(device_make=device_make) | Q(device__make=device_make))
    if device_key:
        qs = qs.filter(Q(device_key=device_key) | Q(device__device_id=device_key))

    try:
        limit = min(int(request.query_params.get('limit', 50)), 200)
    except (TypeError, ValueError):
        limit = 50

    # Markers for charts: since= ISO timestamp
    since = request.query_params.get('since')
    if since:
        try:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(since)
            if dt:
                qs = qs.filter(created_at__gte=dt)
        except Exception:
            pass

    open_base = AlertEvent.objects.filter(
        resolved_at__isnull=True, acknowledged_at__isnull=True
    ).exclude(event_type__in=HIGHLIGHT_TYPES).exclude(event_type__startswith='user_')
    total_open = open_base.filter(
        Q(severity__in=(AlertEvent.SEVERITY_WARN, AlertEvent.SEVERITY_CRITICAL))
        | Q(event_type__in=PROBLEM_TYPES)
    ).count()
    critical_open = AlertEvent.objects.filter(
        resolved_at__isnull=True,
        acknowledged_at__isnull=True,
        severity=AlertEvent.SEVERITY_CRITICAL,
    ).exclude(event_type__in=HIGHLIGHT_TYPES).count()

    events = list(qs[:limit])
    return Response({
        'results': AlertEventSerializer(events, many=True).data,
        'open_count': total_open,
        'critical_open_count': critical_open,
        'kind': kind,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def activity_event_acknowledge(request, pk):
    try:
        event = AlertEvent.objects.get(pk=pk)
    except AlertEvent.DoesNotExist:
        return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
    event.acknowledged_at = timezone.now()
    event.save(update_fields=['acknowledged_at'])
    return Response(AlertEventSerializer(event).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def activity_event_snooze(request, pk):
    try:
        event = AlertEvent.objects.get(pk=pk)
    except AlertEvent.DoesNotExist:
        return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
    try:
        minutes = int((request.data or {}).get('minutes', 60))
    except (TypeError, ValueError):
        minutes = 60
    minutes = max(5, min(24 * 60, minutes))
    event.muted_until = timezone.now() + timedelta(minutes=minutes)
    event.save(update_fields=['muted_until'])
    return Response(AlertEventSerializer(event).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def activity_event_resolve(request, pk):
    try:
        event = AlertEvent.objects.get(pk=pk)
    except AlertEvent.DoesNotExist:
        return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
    event.resolved_at = timezone.now()
    event.save(update_fields=['resolved_at'])
    return Response(AlertEventSerializer(event).data)


def _format_difficulty(difficulty):
    """Format difficulty number to human-readable string."""
    if difficulty is None or difficulty == 0:
        return '0'
    if difficulty >= 1e15:
        return f'{difficulty / 1e15:.2f}P'
    if difficulty >= 1e12:
        return f'{difficulty / 1e12:.2f}T'
    if difficulty >= 1e9:
        return f'{difficulty / 1e9:.2f}G'
    if difficulty >= 1e6:
        return f'{difficulty / 1e6:.2f}M'
    if difficulty >= 1e3:
        return f'{difficulty / 1e3:.2f}K'
    return str(int(difficulty))


def _format_time_duration(hours):
    """Format hours to human-readable duration string."""
    if hours < 1:
        return f'{int(hours * 60)} minutes'
    if hours < 24:
        return f'{hours:.1f} hours'
    if hours < 168:  # 7 days
        return f'{hours / 24:.1f} days'
    if hours < 720:  # 30 days
        return f'{hours / 168:.1f} weeks'
    if hours < 8760:  # 365 days
        return f'{hours / 720:.1f} months'
    return f'{hours / 8760:.1f} years'

