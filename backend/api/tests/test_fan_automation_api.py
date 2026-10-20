"""Temperatur-Automatik: settings via /api/fans/automation/, locks for manual fan buttons and A15 clock."""
from unittest.mock import patch

import pytest
from api import acinfinity as ac
from api.models import Device, FanAutomation
from django.contrib.auth.models import User
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
    assert client.post('/api/fans/automation/', {'action': 'level', 'level': 11}, format='json').status_code == 400
    assert client.post('/api/fans/automation/', {'action': 'disable'}, format='json').data['enabled'] is False


def test_manual_buttons_and_clock_locked(client, a15, backup):
    client.post('/api/fans/automation/', {'action': 'enable', 'dev_id': 'dev1', 'port': 1, 'level': 6}, format='json')
    res = client.post('/api/fans/test/', {'dev_id': 'dev1', 'port': 1, 'speed': 4, 'action': 'set'}, format='json')
    assert res.status_code == 409
    res = client.post('/api/devices/%d/throttle/' % a15.id, {'clock_mhz': 300}, format='json')
    assert res.status_code == 409
