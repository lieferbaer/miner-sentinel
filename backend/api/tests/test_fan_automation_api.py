"""Temperatur-Automatik: settings via /api/fans/automation/, locks for manual fan buttons and A15 clock."""
from unittest.mock import patch

import pytest
from api import acinfinity as ac
from api.models import Device, FanAutomation
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


def test_enable_lifts_manual_clock(client, a15, backup):
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


def test_manual_fan_buttons_locked_but_manual_clock_allowed(client, a15, backup):
    from api.views import DeviceViewSet
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 6}, format='json')
    res = client.post('/api/fans/test/', {'dev_id': 'dev1', 'port': 1, 'speed': 4, 'action': 'set'}, format='json')
    assert res.status_code == 409                    # the automation controls the fan
    live = {'workmode': 4, 'supported': True, 'points': [360, 360, 360], 'mode_key': '2/L4',
            'mode_label': 'Power Mode 04', 'ghsspd': 150000.0, 'mhs5': None}
    with patch.object(DeviceViewSet, '_throttle_live', lambda self, d: live), \
            patch('api.views._cgminer_command', return_value='set OK'):
        res = client.post('/api/devices/%d/throttle/' % a15.id, {'clock_mhz': 300}, format='json')
    assert res.status_code == 200                    # a manual clock overrides the automation's clock control
    assert res.data['active']['clock_mhz'] == 300.0


def test_run_start_needs_fan_and_switches_automation_on(client, a15, backup):
    res = client.post('/api/devices/%d/calibration/' % a15.id, {'action': 'start_step', 'mode': '2/L4'},
                      format='json')
    assert res.status_code == 409                               # fan never set up
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=5, enabled=False)
    res = client.post('/api/devices/%d/calibration/' % a15.id, {'action': 'start_step', 'mode': '2/L4'},
                      format='json')
    assert res.status_code == 200
    assert FanAutomation.objects.get(device=a15).enabled is True


def test_throttle_allowed_while_paused_and_lifted_on_resume(client, a15, backup):
    from api.models import CalibrationRun
    from api.views import DeviceViewSet
    a15.connection_config['throttle']['active'] = None          # no throttle before the run
    a15.save()
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=5, enabled=True)
    run = CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={})
    live = {'workmode': 4, 'supported': True, 'points': [360, 360, 360], 'mode_key': '2/L4',
            'mode_label': 'Power Mode 04', 'ghsspd': 150000.0, 'mhs5': None}
    url = '/api/devices/%d/throttle/' % a15.id
    with patch.object(DeviceViewSet, '_throttle_live', lambda self, d: live), \
            patch('api.views._cgminer_command', return_value='set OK'):
        assert client.post(url, {'clock_mhz': 300}, format='json').status_code == 409     # running
        run.status = 'paused'
        run.save()
        res = client.post(url, {'clock_mhz': 300}, format='json')
        assert res.status_code == 200, res.data
        assert res.data['active']['clock_mhz'] == 300.0
    res = client.post('/api/devices/%d/calibration/' % a15.id, {'action': 'resume'}, format='json')
    assert res.status_code == 200
    assert Device.objects.get(pk=a15.pk).connection_config['throttle']['active'] is None


def test_lift_throttle_with_automation_sends_no_clock(client, a15, backup):
    from api.models import CalibrationRun
    from api.views import DeviceViewSet
    a15.connection_config['throttle']['active'] = {'clock_mhz': 300.0, 'profile': '2/L4',
                                                   'applied_points': [300, 300, 300]}
    a15.save()
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=5, enabled=True)
    CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='paused', state={})
    live = {'workmode': 4, 'supported': True, 'points': [300, 300, 300], 'mode_key': '2/L4',
            'mode_label': 'Power Mode 04', 'ghsspd': 150000.0, 'mhs5': None}
    with patch.object(DeviceViewSet, '_throttle_live', lambda self, d: live), \
            patch('api.views._cgminer_command', return_value='set OK') as cmd:
        res = client.post('/api/devices/%d/throttle/' % a15.id, {'reset': True}, format='json')
    assert res.status_code == 200 and not cmd.called           # the automation continues from 300 MHz
    assert Device.objects.get(pk=a15.pk).connection_config['throttle']['active'] is None


def test_set_clock_is_the_latest_setter(client, a15):
    from api.models import CalibrationRun
    from api.views import DeviceViewSet
    auto = FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=True,
                                        state={'clock_set': 220.0, 'changed_at': '2026-10-21T00:30:00+00:00'})
    view = DeviceViewSet()
    assert view._last_set_clock(a15, None, auto) == (220.0, 'Automatik')
    CalibrationRun.objects.create(device=a15, kind='step', modes=['2/L4'], status='running', state={
        'phase': 'measure', 'set_points': [150, 160, 170], 'changed_at': '2026-10-21T01:20:00+00:00'})
    assert view._last_set_clock(a15, None, auto) == (160.0, 'Takt-Messung')     # the run set later
    auto.state = {'clock_set': None, 'changed_at': '2026-10-21T01:30:00+00:00'}
    assert view._last_set_clock(a15, None, auto) == (None, None)                 # back to the firmware's clocks
    auto.state = {'clock_set': 220.0, 'changed_at': '2026-10-21T00:30:00+00:00'}
    run = CalibrationRun.objects.get(device=a15)
    run.status, run.finished_at = 'done', timezone.now()
    run.save()
    assert view._last_set_clock(a15, None, auto) == (None, None)                 # run ended later: firmware clocks
