"""Temperatur-Automatik: settings via /api/fans/automation/, user events in the protocol, locks for manual fan
buttons; the Drosselung of the A15 is abolished."""
from unittest.mock import patch

import pytest
from api import acinfinity as ac
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


def test_run_start_needs_fan_and_switches_automation_on(client, a15, backup):
    res = client.post('/api/devices/%d/calibration/' % a15.id, {'action': 'start_step', 'mode': '2/L4'},
                      format='json')
    assert res.status_code == 409                               # fan never set up
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=5, enabled=False)
    res = client.post('/api/devices/%d/calibration/' % a15.id, {'action': 'start_step', 'mode': '2/L4'},
                      format='json')
    assert res.status_code == 200
    assert FanAutomation.objects.get(device=a15).enabled is True


def test_resume_lifts_old_manual_clock(client, a15, backup):
    from api.models import CalibrationRun
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=5, enabled=True)
    CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='paused', state={})
    res = client.post('/api/devices/%d/calibration/' % a15.id, {'action': 'resume'}, format='json')
    assert res.status_code == 200
    assert Device.objects.get(pk=a15.pk).connection_config['throttle']['active'] is None


def test_set_clock_is_the_latest_setter(client, a15):
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
    auto.state = {'clock_set': 220, 'clock_changed_at': '2026-10-21T00:30:00+00:00'}
    CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={
        'phase': 'measure', 'set_points': [150, 160, 170], 'changed_at': '2026-10-21T01:20:00+00:00'})
    assert view._last_set_clock(a15, auto) == (160.0, 'Takt-Messung')     # the run set later
    auto.state = {'clock_set': 230, 'clock_changed_at': '2026-10-21T01:30:00+00:00'}
    assert view._last_set_clock(a15, auto) == (230, 'Automatik')
    auto.state = {'clock_set': 220, 'clock_changed_at': '2026-10-21T00:30:00+00:00'}
    run = CalibrationRun.objects.get(device=a15)
    run.status, run.finished_at = 'done', timezone.now()
    run.save()
    assert view._last_set_clock(a15, auto) == (None, None)                 # run ended later: firmware clocks


def test_maintenance_start_pauses_run_and_end(client, a15, backup):
    from api.models import CalibrationRun
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    run = CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={})
    res = client.post('/api/fans/automation/', {'action': 'maintenance_start'}, format='json')
    assert res.status_code == 200 and res.data['maintenance_at']
    run.refresh_from_db()
    assert run.status == 'paused' and run.pause_reason == 'wartung'
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
    auto.state = {'v': 2, 'phase': 'normal', 't': 82, 'tendency': 1, 'seen_at': timezone.now().isoformat()}
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
    assert (events[1].phase, events[1].chip_temp, events[1].tendency) == ('normal', 82.0, 1)
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
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    auto.state = {
        'v': 2, 'phase': 'korrektur', 't': 86, 'tendency': 1, 'boards': [86, 81, 78], 'clock_set': 250,
        'clock_changed_at': '2026-10-24T20:00:00+00:00', 'clock_now': 251.5, 'mode_key': '2/L4', 'stock_mhz': 357.5,
        'fan_target': 4, 'fan_set': 4, 'fan_read': 4, 'fan_read_at': '2026-10-24T20:00:10+00:00',
        'fan_set_at': '2026-10-24T20:00:00+00:00', 'window_until': '2026-10-24T20:00:30+00:00', 'window_active': True,
        'pending': {'kind': 'takt', 'value': 250, 'sent_at': '2026-10-24T20:00:00+00:00', 'attempt': 2,
                    'points': [230, 250, 260, 270], 'log_id': 17, 'cmd': {'why': 'x'}},
        'learn_since': None, 'cal': False, 'over_hot': None, 'offline': None, 'ac_error': None, 'last_error': None,
        'seen_at': '2026-10-24T20:00:05+00:00', 'exc_active': False, 'queue': [], 'temps': [[1, 2]],
        'learned': {'3': {'mhz': 240, 'temp_avg': 82.1, 'at': '2026-10-24T19:00:00+00:00', 'count': 2},
                    '2/L4': {'3': {'mhz': 231}}},
        'log': [['2026-10-24T20:00:00+00:00', 'Korrekturphase']],
    }
    auto.save()
    data = client.get('/api/fans/automation/').data
    st = data['state']
    assert st['phase'] == 'korrektur' and st['t'] == 86 and st['clock_set'] == 250 and st['fan_target'] == 4
    assert st['clock_changed_at'] and st['window_active'] is True and st['base'] == 3
    assert st['pending'] == {'kind': 'takt', 'value': 250, 'sent_at': '2026-10-24T20:00:00+00:00', 'attempt': 2}
    assert 'queue' not in st and 'temps' not in st
    assert data['learned'] == {'3': {'mhz': 240, 'temp_avg': 82.1, 'at': '2026-10-24T19:00:00+00:00', 'count': 2}}
    assert data['log'] == [['2026-10-24T20:00:00+00:00', 'Korrekturphase']]


def test_caps_in_payload_and_clear(client, a15, backup):
    """Obergrenzen (Nutzer 28.10.): the payload shows the caps per fan stage; „Obergrenzen löschen“ only writes the
    user event (the data-service clears the caps in its next round), with the automation on or off."""
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    auto.state = {'v': 2, 'phase': 'normal', 't': 83, 'caps': {'3': 325, '5': 395}, 'caps_cleared_seen': 'x',
                  'seen_at': timezone.now().isoformat()}
    auto.save()
    st = client.get(url).data['state']
    assert st['caps'] == {'3': 325, '5': 395} and 'caps_cleared_seen' not in st
    updated = FanAutomation.objects.get(device=a15).updated_at
    res = client.post(url, {'action': 'caps_clear'}, format='json')
    assert res.status_code == 200 and res.data['state']['caps'] == {'3': 325, '5': 395}   # until the data-service ran
    auto = FanAutomation.objects.get(device=a15)
    assert auto.updated_at == updated and auto.enabled and auto.state['caps'] == {'3': 325, '5': 395}
    client.post(url, {'action': 'disable'}, format='json')
    assert client.post(url, {'action': 'caps_clear'}, format='json').status_code == 200     # automation off: allowed
    events = list(AutomationLog.objects.filter(kind='obergrenzen_loeschen').order_by('id'))
    assert [(e.trigger, e.source, e.device_id, e.ok) for e in events] == [
        ('Nutzer: Obergrenzen gelöscht', 'nutzer', a15.id, None)] * 2
    assert (events[0].phase, events[0].chip_temp) == ('normal', 83.0)


def test_caps_clear_without_a15(client):
    assert client.post('/api/fans/automation/', {'action': 'caps_clear'}, format='json').status_code == 404
    assert not AutomationLog.objects.exists()


def test_temp_test_start_and_abort(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    res = client.post(url, {'action': 'temp_test_start'}, format='json')
    assert res.status_code == 200 and res.data['temp_test_at'] and res.data['state']['test'] is None
    assert FanAutomation.objects.get(device=a15).temp_test_at
    res = client.post(url, {'action': 'temp_test_start'}, format='json')
    assert res.status_code == 409 and res.data['error'] == 'Ein Temperatur-Test läuft bereits.'
    res = client.post(url, {'action': 'temp_test_abort'}, format='json')
    assert res.status_code == 200 and res.data['temp_test_at'] is None
    assert client.post(url, {'action': 'temp_test_abort'}, format='json').status_code == 200   # no test: no entry
    events = list(AutomationLog.objects.filter(kind__startswith='test_').order_by('id'))
    assert [(e.kind, e.trigger, e.source) for e in events] == [
        ('test_start', 'Nutzer: Temperatur-Test gestartet', 'nutzer'),
        ('test_ende', 'Nutzer: Temperatur-Test abgebrochen', 'nutzer')]
    assert events[1].details == {'ergebnis': 'abgebrochen', 'grund': 'Nutzer'}


def test_temp_test_start_refused(client, a15, backup):
    from api.models import CalibrationRun
    url = '/api/fans/automation/'
    res = client.post(url, {'action': 'temp_test_start'}, format='json')
    assert res.status_code == 409 and res.data['error'] == 'Temperatur-Test nur bei eingeschalteter Automatik.'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    client.post(url, {'action': 'maintenance_start'}, format='json')
    res = client.post(url, {'action': 'temp_test_start'}, format='json')
    assert res.status_code == 409 and res.data['error'] == 'Während der Wartung ist kein Temperatur-Test möglich.'
    client.post(url, {'action': 'maintenance_end'}, format='json')
    run = CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={})
    res = client.post(url, {'action': 'temp_test_start'}, format='json')
    assert res.status_code == 409 and res.data['error'] == 'Während einer Takt-Messung ist kein Temperatur-Test möglich.'
    run.status = 'paused'
    run.save()
    assert client.post(url, {'action': 'temp_test_start'}, format='json').status_code == 200
    assert FanAutomation.objects.get(device=a15).temp_test_at
    assert not AutomationLog.objects.filter(kind='test_ende').exists()


@pytest.mark.parametrize('action, grund, kinds', [
    ('disable', 'Automatik aus', ['automatik_aus', 'test_ende']),
    ('maintenance_start', 'Wartung', ['wartung_start', 'test_ende']),
])
def test_disable_and_maintenance_end_the_test(client, a15, backup, action, grund, kinds):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    client.post(url, {'action': 'temp_test_start'}, format='json')
    start = AutomationLog.objects.get(kind='test_start').id
    res = client.post(url, {'action': action}, format='json')
    assert res.status_code == 200 and res.data['temp_test_at'] is None
    assert FanAutomation.objects.get(device=a15).temp_test_at is None
    events = list(AutomationLog.objects.filter(id__gt=start).order_by('id'))
    assert [e.kind for e in events] == kinds
    assert events[1].details == {'ergebnis': 'abgebrochen', 'grund': grund}
    assert events[1].trigger == 'Nutzer: Temperatur-Test abgebrochen (%s)' % grund


def test_level_keeps_the_test_the_data_service_ended(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    client.post(url, {'action': 'temp_test_start'}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    FanAutomation.objects.filter(pk=auto.pk).update(temp_test_at=None)      # data-service: test over
    with patch('api.fan_views._automation', return_value=(a15, auto)):     # loaded before that
        client.post(url, {'action': 'level', 'level': 3}, format='json')
    assert FanAutomation.objects.get(device=a15).temp_test_at is None


def test_payload_temp_test(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    test = {'started': '2026-10-25T18:00:00+00:00', 'stage': 2, 'delta': 4, 'band': [84, 88],
            'stage_ends': '2026-10-25T20:00:00+00:00', 'ends': '2026-10-25T22:00:00+00:00'}
    auto = FanAutomation.objects.get(device=a15)
    auto.state = {'v': 2, 'phase': 'normal', 'test': test}
    auto.save()
    data = client.get(url).data
    assert data['temp_test_at'] is None and data['state']['test'] is None    # no test set: state of an old one
    client.post(url, {'action': 'temp_test_start'}, format='json')
    data = client.get(url).data
    assert data['temp_test_at'] and data['state']['test'] is None             # old test: not shown for the new one
    auto.refresh_from_db()
    test = dict(test, started=auto.temp_test_at.isoformat())                  # the data-service took the new test
    auto.state = {'v': 2, 'phase': 'normal', 'test': test}
    auto.save(update_fields=['state'])
    assert client.get(url).data['state']['test'] == test
    assert client.get('/api/live/').data['automation']['state']['test'] == test


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
    """Zielbereich (Nutzer 27.10.): 76–92 °C, 2–4 Grad; stored, user event with old and new range, in the payload."""
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.target_low, auto.target_high) == (82, 85)                    # start value
    auto.state = {'v': 2, 'phase': 'normal', 'zb': [82, 85]}
    auto.save()
    res = client.post(url, {'action': 'target', 'low': 80, 'high': 83}, format='json')
    assert res.status_code == 200 and (res.data['target_low'], res.data['target_high']) == (80, 83)
    assert res.data['state']['zb'] == [82, 85]                                 # not yet taken over
    auto.refresh_from_db()
    assert (auto.target_low, auto.target_high, auto.level, auto.enabled) == (80, 83, 3, True)
    live = client.get('/api/live/').data['automation']
    assert (live['target_low'], live['target_high']) == (80, 83)
    for low, high in ((76, 79), (91, 92)):                                     # edges: 4 and 2 Grad
        assert client.post(url, {'action': 'target', 'low': low, 'high': high}, format='json').status_code == 200
    events = list(AutomationLog.objects.filter(kind='zielbereich').order_by('id'))
    assert [(e.trigger, e.source, e.details) for e in events] == [
        ('Nutzer: Zielbereich 80–83 °C gewählt (vorher 82–85 °C)', 'nutzer', {'alt': '82–85 °C', 'neu': '80–83 °C'}),
        ('Nutzer: Zielbereich 76–79 °C gewählt (vorher 80–83 °C)', 'nutzer', {'alt': '80–83 °C', 'neu': '76–79 °C'}),
        ('Nutzer: Zielbereich 91–92 °C gewählt (vorher 76–79 °C)', 'nutzer', {'alt': '76–79 °C', 'neu': '91–92 °C'})]
    assert events[0].phase == 'normal'


@pytest.mark.parametrize('low, high', [
    (75, 78), (90, 93), (82, 82), (82, 86), (85, 82), ('x', 85), (82.5, 85), (82, None), (True, 85)])
def test_target_range_invalid(client, a15, backup, low, high):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    res = client.post(url, {'action': 'target', 'low': low, 'high': high}, format='json')
    assert res.status_code == 400 and res.data == {'error': 'Zielbereich 76–92 °C mit 2 bis 4 Grad wählen.'}
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.target_low, auto.target_high) == (82, 85)
    assert not AutomationLog.objects.filter(kind='zielbereich').exists()


def test_target_range_allowed_with_automation_off_wartung_and_takt_messung(client, a15):
    from api.models import CalibrationRun
    url = '/api/fans/automation/'
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=False)
    res = client.post(url, {'action': 'target', 'low': 79, 'high': 84}, format='json')
    assert res.status_code == 400                                              # 6 Grad
    res = client.post(url, {'action': 'target', 'low': 81, 'high': 84}, format='json')
    assert res.status_code == 200 and res.data['enabled'] is False and res.data['target_low'] == 81
    auto = FanAutomation.objects.get(device=a15)
    auto.maintenance_at = timezone.now()
    auto.save()
    assert client.post(url, {'action': 'target', 'low': 80, 'high': 83}, format='json').status_code == 200
    auto.maintenance_at = None
    auto.save()
    CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={})
    assert client.post(url, {'action': 'target', 'low': 79, 'high': 82}, format='json').status_code == 200
    auto.refresh_from_db()
    assert (auto.target_low, auto.target_high, auto.enabled) == (79, 82, False)
    assert AutomationLog.objects.filter(kind='zielbereich').count() == 3


def test_target_range_same_range_no_entry(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 3}, format='json')
    client.post(url, {'action': 'temp_test_start'}, format='json')
    updated = FanAutomation.objects.get(device=a15).updated_at
    res = client.post(url, {'action': 'target', 'low': 82, 'high': 85}, format='json')
    assert res.status_code == 200 and (res.data['target_low'], res.data['target_high']) == (82, 85)
    assert res.data['temp_test_at']                                            # the test goes on
    auto = FanAutomation.objects.get(device=a15)
    assert auto.temp_test_at and auto.updated_at == updated
    assert not AutomationLog.objects.filter(kind__in=('zielbereich', 'test_ende')).exists()


def test_target_range_ends_running_test(client, a15, backup):
    url = '/api/fans/automation/'
    client.post(url, {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 2}, format='json')
    client.post(url, {'action': 'temp_test_start'}, format='json')
    start = AutomationLog.objects.get(kind='test_start').id
    res = client.post(url, {'action': 'target', 'low': 80, 'high': 82}, format='json')
    assert res.status_code == 200 and res.data['temp_test_at'] is None
    auto = FanAutomation.objects.get(device=a15)
    assert (auto.temp_test_at, auto.target_low, auto.target_high) == (None, 80, 82)
    events = list(AutomationLog.objects.filter(id__gt=start).order_by('id'))
    assert [e.kind for e in events] == ['zielbereich', 'test_ende']
    assert events[1].trigger == 'Nutzer: Temperatur-Test abgebrochen (Zielbereich geändert)'
    assert events[1].details == {'ergebnis': 'abgebrochen', 'grund': 'Zielbereich geändert'}
