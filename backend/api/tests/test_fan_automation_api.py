"""Temperatur-Automatik: settings via /api/fans/automation/, user events in the protocol, locks for manual fan
buttons; the Drosselung of the A15 is abolished; Takt-Messung, Temperatur-Test and Obergrenzen are removed (v2.2.7)."""
from unittest.mock import patch

import pytest
from api import acinfinity as ac
from api.fan_views import STATE_KEYS
from api.models import AutomationLog, Device, FanAutomation
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='autouser', password='testpass123'))
    return c


@pytest.fixture
def a15(db):
    return Device.objects.create(
        make='avalon', device_id='A15', name='Avalon A15', ip_address='10.0.0.15', port=4028,
        protocol=Device.PROTOCOL_CGMINER_TCP,
        connection_config={'throttle': {'freq_ok': True, 'active': {'clock_mhz': 300.0}, 'profiles': {
            '2/L4': {'mode_key': '2/L4', 'mode_label': 'Power Mode 04', 'points': [360, 360, 360],
                     'avg_mhz': 360.0}}}})


@pytest.fixture
def backup():
    with patch.object(ac, 'has_backup', return_value=True):
        yield


def test_enable_lifts_old_manual_clock(client, a15, backup):
    res = client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 6},
                      format='json')
    assert res.status_code == 200 and res.data['enabled'] and res.data['level'] == 6
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.ac_dev_id, auto.ac_port, auto.level) == ('dev1', 1, 6)
    throttle = Device.objects.get(pk=a15.pk).connection_config['throttle']
    assert throttle['active'] is None and throttle['last_event']['type'] == 'automation'


def test_enable_needs_backup(client, a15):
    with patch.object(ac, 'has_backup', return_value=False):
        res = client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 6},
                          format='json')
    assert res.status_code == 409


def test_level_and_disable(client, a15, backup):
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 6}, format='json')
    assert client.post('/api/fans/automation/', {'action': 'level', 'level': 3}, format='json').data['level'] == 3
    first = FanAutomation.objects.get(device=a15).level_at
    client.post('/api/fans/automation/', {'action': 'level', 'level': 3}, format='json')
    assert first and FanAutomation.objects.get(device=a15).level_at > first     # same level again: new click
    assert client.post('/api/fans/automation/', {'action': 'level', 'level': 11}, format='json').status_code == 400
    assert client.post('/api/fans/automation/', {'action': 'disable'}, format='json').data['enabled'] is False


def test_manual_fan_buttons_locked_and_no_manual_clock(client, a15, backup):
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 6}, format='json')
    res = client.post('/api/fans/test/', {'dev_id': 'dev1', 'port': 1, 'speed': 4, 'action': 'set'}, format='json')
    assert res.status_code == 409                    # the automation controls the fan
    with patch('api.views._cgminer_command', return_value='set OK') as cmd:
        res = client.post('/api/devices/%d/throttle/' % a15.id, {'clock_mhz': 300}, format='json')
    assert res.status_code == 400 and not cmd.called       # the Drosselung of the A15 is abolished


def test_takt_messung_removed(client, a15, backup):
    """Takt-Messung removed (v2.2.7, Nutzer 06.11.): no calibration endpoint; old runs stay in the table but lock
    nothing (mode switch, Zieltemperatur) and are not touched by Wartung."""
    from api.models import CalibrationRun
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=5, enabled=False)
    run = CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={})
    cal_url = '/api/devices/%d/calibration/' % a15.id
    assert client.get(cal_url).status_code == 404
    for body in ({'action': 'start_step', 'mode': '2/L4'}, {'action': 'resume'}, {'action': 'cancel'}):
        assert client.post(cal_url, body, format='json').status_code == 404
    assert FanAutomation.objects.get(device=a15).enabled is False            # not switched on by a run any more
    with patch('api.views._a15_set_level', return_value=True) as set_level:
        res = client.post('/api/devices/%d/workmode/' % a15.id, {'mode': 3}, format='json')
    assert res.status_code == 200 and set_level.called                     # no lock „Takt-Messung läuft“
    url = '/api/fans/automation/'
    assert client.post(url, {'action': 'target', 'low': 80, 'high': 84}, format='json').status_code == 200
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    assert client.post(url, {'action': 'maintenance_start'}, format='json').status_code == 200
    run.refresh_from_db()
    assert (run.status, run.pause_reason) == ('running', '')                # Wartung no longer pauses a run
    assert 'calibration' not in client.get('/api/live/').data


def test_set_clock_of_the_automation(client, a15):
    """The A15 card shows the clock of the Temperatur-Automatik; a run of the former Takt-Messung counts no more."""
    from api.models import CalibrationRun
    from api.views import DeviceViewSet
    auto = FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=True,
                                        state={'clock_set': 220, 'clock_changed_at': '2026-10-21T00:30:00+00:00'})
    view = DeviceViewSet()
    assert view._last_set_clock(a15, auto) == (220, 'Automatik')
    auto.enabled = False                                                         # Automatik aus: the clock stays
    assert view._last_set_clock(a15, auto) == (220, 'Automatik')
    auto.state = {'clock_set': 220, 'changed_at': '2026-10-21T00:30:00+00:00'}   # old regulation: not used
    assert view._last_set_clock(a15, auto) == (None, None)
    auto.state = {'clock_set': None, 'clock_changed_at': '2026-10-21T00:30:00+00:00'}
    assert view._last_set_clock(a15, auto) == (None, None)
    assert view._last_set_clock(a15, None) == (None, None)
    auto.state = {'clock_set': 220, 'clock_changed_at': '2026-10-21T00:30:00+00:00'}
    run = CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={
        'phase': 'measure', 'set_points': [150, 160, 170], 'changed_at': '2026-10-21T01:20:00+00:00'})
    assert view._last_set_clock(a15, auto) == (220, 'Automatik')            # a later run: not used
    run.status, run.finished_at = 'done', timezone.now()
    run.save()
    assert view._last_set_clock(a15, auto) == (220, 'Automatik')
    a15.connection_config['throttle']['last_seen'] = {'points': [220, 220, 220], 'mode_key': '2/L4'}
    a15.save()
    data = client.get('/api/devices/%d/throttle/' % a15.id).data
    assert (data['set_mhz'], data['set_by'], data['automation_active']) == (220, 'Automatik', True)
    assert 'calibration_active' not in data and 'calibration_paused' not in data


def test_maintenance_start_and_end(client, a15, backup):
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    res = client.post('/api/fans/automation/', {'action': 'maintenance_start'}, format='json')
    assert res.status_code == 200 and res.data['maintenance_at']
    assert client.post('/api/fans/automation/', {'action': 'maintenance_start'}, format='json').status_code == 400
    assert client.get('/api/live/').data['automation']['maintenance_at']
    res = client.post('/api/fans/automation/', {'action': 'maintenance_end'}, format='json')
    assert res.data['maintenance_at'] is None


def test_disable_keeps_clock_without_throttle(client, a15, backup):
    a15.connection_config['throttle']['active'] = None
    a15.save()
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    auto.state = {'v': 2, 'clock_set': 164, 'clock_changed_at': '2026-10-24T20:00:00+00:00', 'mode_key': '2/L4'}
    auto.save()
    with patch('api.views._cgminer_command') as cmd:
        res = client.post('/api/fans/automation/', {'action': 'disable'}, format='json')
    assert res.data['enabled'] is False and not cmd.called                # no clock is sent
    dev = Device.objects.get(pk=a15.pk)
    assert dev.connection_config['throttle']['active'] is None
    dev.connection_config['throttle']['last_seen'] = {'points': [164, 164, 164], 'mode_key': '2/L4'}
    dev.save()
    res = client.get('/api/devices/%d/throttle/' % a15.id)
    assert (res.data['set_mhz'], res.data['set_by'], res.data['automation_active']) == (164, 'Automatik', False)


@pytest.mark.parametrize('seen, kept', [
    ({'points': [165, 164, 163], 'mode_key': '2/L4', 'elapsed': 90000, 'at': '2026-10-25T00:00:00+00:00'}, True),
    ({'points': [360, 360, 360], 'mode_key': '2/L4'}, False),                  # the miner runs another clock
    ({'points': [164, 164, 164], 'mode_key': '2/L3'}, False),                  # mode switched since
    ({'points': [164, 164, 164], 'mode_key': '2/L4', 'elapsed': 600,
      'at': '2026-10-25T00:00:00+00:00'}, False),                              # restarted since
    ({'points': [164, 164, 164], 'mode_key': '2/L4', 'elapsed': 86370,
      'at': '2026-10-25T00:00:00+00:00'}, True),                               # restart right after its own mode switch
])
def test_clock_of_automation_off_only_while_in_effect(a15, seen, kept):
    from api.views import DeviceViewSet
    auto = FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=False, state={
        'clock_set': 164, 'clock_changed_at': '2026-10-24T00:00:00+00:00', 'mode_key': '2/L4'})
    a15.connection_config['throttle']['last_seen'] = seen
    assert DeviceViewSet()._last_set_clock(a15, auto) == ((164, 'Automatik') if kept else (None, None))
    auto.enabled = True                                         # automation on: the data-service re-sends its clock
    assert DeviceViewSet()._last_set_clock(a15, auto) == (164, 'Automatik')


def test_user_events_in_protocol(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    auto.state = {'v': 2, 'phase': 'basismodus', 't': 82, 'tendency': 1, 'seen_at': timezone.now().isoformat()}
    auto.save()
    client.post(url, {'action': 'level', 'level': 4}, format='json')
    client.post(url, {'action': 'level', 'level': 4}, format='json')            # the same level again
    client.post(url, {'action': 'maintenance_start'}, format='json')
    client.post(url, {'action': 'maintenance_end'}, format='json')
    client.post(url, {'action': 'maintenance_end'}, format='json')             # no maintenance: no entry
    client.post(url, {'action': 'disable'}, format='json')
    assert client.post(url, {'action': 'disable'}, format='json').status_code == 200   # already off: no entry
    events = list(AutomationLog.objects.order_by('id'))
    assert [e.kind for e in events] == ['automatik_ein', 'basisstufe', 'basisstufe', 'wartung_start',
                                        'wartung_ende', 'automatik_aus']
    assert all(e.source == 'nutzer' and e.device_id == a15.id and e.ok is None for e in events)
    assert events[0].base_new == 3 and events[0].trigger == 'Nutzer: Automatik eingeschaltet (Basisstufe 3)'
    assert (events[1].base_old, events[1].base_new) == (3, 4)
    assert events[1].trigger == 'Nutzer: Basisstufe 4 gewählt'
    assert (events[1].phase, events[1].chip_temp, events[1].tendency) == ('basismodus', 82.0, 1)
    assert (events[2].base_old, events[2].base_new) == (4, 4)


def test_user_event_chip_temperature_only_when_current(client, a15, backup):
    from datetime import timedelta
    from api.models import DeviceSystemInfo
    url = '/api/fans/automation/'
    now = timezone.now()
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=False, state={
        'v': 2, 'phase': 'aus', 'off': True, 't': 83, 'tendency': 2, 'seen_at': (now - timedelta(days=2)).isoformat()})
    DeviceSystemInfo.objects.create(device=a15, recorded_at=now - timedelta(seconds=60),
                                    details={'chip_temp_boards': [90, 88, 87]})
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    DeviceSystemInfo.objects.create(device=a15, recorded_at=timezone.now() - timedelta(seconds=10),
                                    details={'chip_temp_boards': [71, 74, 69]})
    client.post(url, {'action': 'level', 'level': 4}, format='json')     # the data-service has not run yet
    auto = FanAutomation.objects.get(device=a15)
    auto.state = dict(auto.state, seen_at=timezone.now().isoformat(), over_hot=True)
    auto.save()
    client.post(url, {'action': 'level', 'level': 5}, format='json')     # Over Hot: readings not used
    events = list(AutomationLog.objects.order_by('id'))
    assert [(e.kind, e.chip_temp, e.tendency) for e in events] == [
        ('automatik_ein', None, None), ('basisstufe', 74.0, None), ('basisstufe', None, None)]


def test_payload_state_of_the_new_regulation(client, a15, backup):
    """State keys of v2.2.7 (Nutzer 06.11., SPEC 7): Heiz-/Basis-/Kühlmodus, locks of the clock increase, excursion
    above ZTO; keys of the removed rules are not passed on."""
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    locks = {'erhoehung': None, 'heizmodus': None, 'kuehlmodus': '2026-10-24T20:03:00+00:00',
             'senkung': '2026-10-24T20:05:00+00:00'}
    hot = {'band': 1, 'since': '2026-10-24T19:59:00+00:00', 'band_at': '2026-10-24T19:59:00+00:00',
           'next_cut_at': '2026-10-24T20:01:00+00:00', 'dwell_s': 180, 'minus1': 10}
    auto.state = {
        'v': 2, 'phase': 'kuehlmodus', 't': 88, 'tendency': 1, 'boards': [88, 81, 78], 'clock_set': 250,
        'clock_changed_at': '2026-10-24T20:00:00+00:00', 'clock_now': 251.5, 'mode_key': '2/L4', 'stock_mhz': 357.5,
        'fan_target': 4, 'fan_set': 4, 'fan_read': 4, 'fan_read_at': '2026-10-24T20:00:10+00:00',
        'fan_set_at': '2026-10-24T20:00:00+00:00', 'fan_changed_at': '2026-10-24T19:59:30+00:00',
        'pending': {'kind': 'takt', 'value': 250, 'sent_at': '2026-10-24T20:00:00+00:00', 'attempt': 2,
                    'points': [230, 250, 260, 270], 'log_id': 17, 'cmd': {'why': 'x'}},
        'learn_since': None, 'over_hot': None, 'offline': None, 'ac_error': None, 'last_error': None,
        'seen_at': '2026-10-24T20:00:05+00:00', 'start_hold': None, 'restart_req': None,
        'heat': None, 'cool': {'since': '2026-10-24T19:59:30+00:00', 'plus': 1}, 'locks': locks, 'hot': hot,
        'cut_at': '2026-10-24T20:00:00+00:00', 'zb': [82, 85], 'queue': [], 'temps': [[1, 2]],
        # keys of the removed rules (an old state until the data-service dropped them)
        'window_until': '2026-10-24T20:00:30+00:00', 'window_active': True, 'cal': False, 'exc_active': False,
        'calm': None, 'stoss': None, 'caps': {'3': 325}, 'test': {'started': '2026-10-24T18:00:00+00:00'},
        'learned': {'3': {'mhz': 240, 'temp_avg': 82.1, 'at': '2026-10-24T19:00:00+00:00', 'count': 2},
                    '2/L4': {'3': {'mhz': 231}}},
        'log': [['2026-10-24T20:00:00+00:00', 'Kühlmodus +1']],
    }
    auto.temp_test_at = timezone.now()                                         # old column: not shown
    auto.save()
    data = client.get('/api/fans/automation/').data
    st = data['state']
    assert st['phase'] == 'kuehlmodus' and st['t'] == 88 and st['clock_set'] == 250 and st['fan_target'] == 4
    assert st['clock_changed_at'] and st['base'] == 3 and st['fan_changed_at'] == '2026-10-24T19:59:30+00:00'
    assert st['heat'] is None and st['cool'] == {'since': '2026-10-24T19:59:30+00:00', 'plus': 1}
    assert st['locks'] == locks and st['hot'] == hot and st['cut_at'] == '2026-10-24T20:00:00+00:00'
    assert st['zb'] == [82, 85] and st['start_hold'] is None
    assert st['pending'] == {'kind': 'takt', 'value': 250, 'sent_at': '2026-10-24T20:00:00+00:00', 'attempt': 2}
    assert set(st) == set(STATE_KEYS) | {'base', 'pending'}
    for key in ('queue', 'temps', 'window_until', 'window_active', 'cal', 'exc_active', 'calm', 'stoss', 'caps',
                'test'):
        assert key not in st
    assert 'temp_test_at' not in data
    assert data['learned'] == {'3': {'mhz': 240, 'temp_avg': 82.1, 'at': '2026-10-24T19:00:00+00:00', 'count': 2}}
    assert data['log'] == [['2026-10-24T20:00:00+00:00', 'Kühlmodus +1']]
    assert client.get('/api/live/').data['automation']['state']['cool'] == {'since': '2026-10-24T19:59:30+00:00',
                                                                             'plus': 1}


def test_state_keys_as_in_the_contract():
    """SPEC v2.2.7 section 7: exactly these keys of fan_automation.state are passed to the web."""
    assert STATE_KEYS == (
        'phase', 't', 'tendency', 'boards', 'clock_set', 'clock_changed_at', 'clock_now', 'mode_key', 'stock_mhz',
        'fan_target', 'fan_set', 'fan_read', 'fan_read_at', 'fan_set_at', 'fan_changed_at', 'learn_since',
        'over_hot', 'offline', 'ac_error', 'last_error', 'seen_at', 'start_hold', 'restart_req', 'heat', 'cool',
        'locks', 'hot', 'cut_at', 'zb')


@pytest.mark.parametrize('action', ['caps_clear', 'temp_test_start', 'temp_test_abort'])
def test_removed_actions_refused(client, a15, backup, action):
    """Obergrenzen and Temperatur-Test removed (v2.2.7, Nutzer 06.11.): their actions are refused like any unknown
    action, change nothing and write no protocol entry."""
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    before = FanAutomation.objects.get(device=a15)
    count = AutomationLog.objects.count()
    res = client.post(url, {'action': action}, format='json')
    assert res.status_code == 400 and res.data == {'error': 'Diese Aktion ist gerade nicht möglich.'}
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.updated_at, auto.temp_test_at, auto.enabled) == (before.updated_at, None, True)
    assert AutomationLog.objects.count() == count


@pytest.mark.parametrize('action, body, kind', [
    ('disable', {}, 'automatik_aus'),
    ('maintenance_start', {}, 'wartung_start'),
    ('target', {'low': 80, 'high': 84}, 'zielbereich'),
])
def test_actions_no_longer_end_a_test(client, a15, backup, action, body, kind):
    """Automatik aus, Wartung and a new Zieltemperatur leave the unused column temp_test_at alone and write no
    test_ende (Temperatur-Test removed, v2.2.7)."""
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    old = timezone.now()
    FanAutomation.objects.filter(device=a15).update(temp_test_at=old)          # left from before 06.11.
    start = AutomationLog.objects.order_by('-id').first().id
    res = client.post(url, dict(body, action=action), format='json')
    assert res.status_code == 200 and 'temp_test_at' not in res.data
    assert FanAutomation.objects.get(device=a15).temp_test_at == old
    assert [e.kind for e in AutomationLog.objects.filter(id__gt=start).order_by('id')] == [kind]


def test_restart_of_the_a15_is_logged(client, a15):
    """A15 neu starten (Lüfter-Seite, Nutzer 25.10.): the existing restart sends ascset 0,reboot,0; for the A15 of the
    automation it is a user event in the protocol."""
    nano = Device.objects.create(make='avalon', device_id='N3S', name='Avalon Nano 3S', ip_address='10.0.0.16',
                                 port=4028, protocol=Device.PROTOCOL_CGMINER_TCP)
    with patch('api.views.socket.create_connection', side_effect=OSError('timed out')):
        assert client.post('/api/devices/%d/restart/' % a15.id).status_code == 502
    with patch('api.views.socket.create_connection', return_value=_Sock([b'STATUS=S,Msg=ASC 0 set info: reboot'])) as conn:
        assert client.post('/api/devices/%d/restart/' % a15.id).status_code == 200
        assert client.post('/api/devices/%d/restart/' % nano.id).status_code == 200
    assert conn.return_value.sent == [b'ascset|0,reboot,0'] * 2
    events = list(AutomationLog.objects.filter(kind='neustart_nutzer'))
    assert len(events) == 1 and events[0].device_id == a15.id and events[0].source == 'nutzer'
    assert events[0].trigger == 'Nutzer: A15-Neustart gesendet' and events[0].ok is True
    assert events[0].request == 'ascset|0,reboot,0'


class _Sock:
    """Fake miner socket: replies with `chunks`, then the connection closes (or `error` is raised)."""

    def __init__(self, chunks=(), error=None):
        self.chunks, self.error, self.sent = list(chunks), error, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.error:
            raise self.error
        return b''


def test_restart_without_reply_counts_as_sent(client, a15):
    """Nutzer 27.10.: the A15 restarts without answering – no "Miner nicht erreichbar: timed out", but the restart
    entry (so the page shows "A15 startet neu")."""
    import socket
    with patch('api.views.socket.create_connection', return_value=_Sock(error=socket.timeout('timed out'))):
        r = client.post('/api/devices/%d/restart/' % a15.id)
    assert r.status_code == 200 and r.data['device_reply'] == 'keine Antwort – Miner startet neu'
    events = list(AutomationLog.objects.filter(kind='neustart_nutzer'))
    assert len(events) == 1 and events[0].ok is True and events[0].response == 'keine Antwort – Miner startet neu'
    with patch('api.views.socket.create_connection', return_value=_Sock(error=ConnectionResetError('reset'))):
        assert client.post('/api/devices/%d/restart/' % a15.id).status_code == 200


def test_target_range_stored_with_event(client, a15, backup):
    """Zieltemperatur (Nutzer 06.11.): ZTU from 77, ZTO up to 87 °C, 4 to 5 Grad (both edges counted); stored, user
    event with old and new range, in the payload."""
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.target_low, auto.target_high) == (82, 85)                    # start value (4 Grad)
    auto.state = {'v': 2, 'phase': 'basismodus', 'zb': [82, 85]}
    auto.save()
    res = client.post(url, {'action': 'target', 'low': 81, 'high': 84}, format='json')
    assert res.status_code == 200 and (res.data['target_low'], res.data['target_high']) == (81, 84)
    assert res.data['state']['zb'] == [82, 85]                                 # not yet taken over
    auto.refresh_from_db()
    assert (auto.target_low, auto.target_high, auto.level, auto.enabled) == (81, 84, 3, True)
    live = client.get('/api/live/').data['automation']
    assert (live['target_low'], live['target_high']) == (81, 84)
    for low, high in ((80, 83), (81, 85), (77, 80), (83, 87), (77, 81)):     # 4 and 5 Grad, at both edges
        assert client.post(url, {'action': 'target', 'low': low, 'high': high}, format='json').status_code == 200
    events = list(AutomationLog.objects.filter(kind='zielbereich').order_by('id'))
    ranges = ['82–85', '81–84', '80–83', '81–85', '77–80', '83–87', '77–81']
    assert [(e.trigger, e.source, e.details) for e in events] == [
        ('Nutzer: Zieltemperatur %s °C gewählt (vorher %s °C)' % (new, old), 'nutzer',
         {'alt': '%s °C' % old, 'neu': '%s °C' % new}) for old, new in zip(ranges, ranges[1:])]
    assert events[1].trigger == 'Nutzer: Zieltemperatur 80–83 °C gewählt (vorher 81–84 °C)'      # SPEC example
    assert events[0].phase == 'basismodus'


@pytest.mark.parametrize('low, high', [
    (76, 79), (76, 80), (84, 88), (83, 88),                    # outside 77–87
    (82, 84), (77, 79), (85, 87), (82, 82),                    # 3 and 1 Grad: too narrow
    (81, 86), (77, 82), (82, 87),                              # 6 Grad: too wide
    (85, 82), ('x', 85), (82.5, 85), (82, None), (True, 85)])
def test_target_range_invalid(client, a15, backup, low, high):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    res = client.post(url, {'action': 'target', 'low': low, 'high': high}, format='json')
    assert res.status_code == 400 and res.data == {'error': 'Zieltemperatur 77–87 °C mit 4 bis 5 Grad wählen.'}
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.target_low, auto.target_high) == (82, 85)
    assert not AutomationLog.objects.filter(kind='zielbereich').exists()


def test_target_range_allowed_with_automation_off_and_wartung(client, a15):
    url = '/api/fans/automation/'
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=False)
    res = client.post(url, {'action': 'target', 'low': 79, 'high': 84}, format='json')
    assert res.status_code == 400                                              # 6 Grad
    res = client.post(url, {'action': 'target', 'low': 81, 'high': 84}, format='json')
    assert res.status_code == 200 and res.data['enabled'] is False and res.data['target_low'] == 81
    auto = FanAutomation.objects.get(device=a15)
    auto.maintenance_at = timezone.now()
    auto.save()
    assert client.post(url, {'action': 'target', 'low': 80, 'high': 84}, format='json').status_code == 200
    auto.refresh_from_db()
    assert (auto.target_low, auto.target_high, auto.enabled) == (80, 84, False)
    assert AutomationLog.objects.filter(kind='zielbereich').count() == 2


def test_target_range_same_range_no_entry(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    updated = FanAutomation.objects.get(device=a15).updated_at
    res = client.post(url, {'action': 'target', 'low': 82, 'high': 85}, format='json')
    assert res.status_code == 200 and (res.data['target_low'], res.data['target_high']) == (82, 85)
    assert FanAutomation.objects.get(device=a15).updated_at == updated
    assert not AutomationLog.objects.filter(kind='zielbereich').exists()
