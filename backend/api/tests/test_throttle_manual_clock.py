"""Drosselung: abolished for the A15 (24.10.: the Temperatur-Automatik sets its clock); Nano/Q unchanged."""
from unittest.mock import patch

import pytest
from api.models import CalibrationPoint, Device
from api.views import DeviceViewSet
from django.contrib.auth.models import User
from rest_framework.test import APIClient

STOCK = [360, 362, 358]           # average 360 MHz


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='thruser', password='testpass123'))
    return c


def _device(device_id, name):
    return Device.objects.create(
        make='avalon', device_id=device_id, name=name, ip_address='10.0.0.15', port=4028,
        protocol=Device.PROTOCOL_CGMINER_TCP,
        connection_config={'throttle': {'freq_ok': True, 'profiles': {'2/L4': {
            'mode_key': '2/L4', 'mode_label': 'Power Mode 04', 'points': STOCK, 'avg_mhz': 360.0}}}})


def _live(device):
    return {'workmode': 4, 'supported': True, 'points': STOCK, 'mode_key': '2/L4', 'mode_label': 'Power Mode 04',
            'ghsspd': 150000.0, 'mhs5': None}


@pytest.fixture
def miner():
    with patch.object(DeviceViewSet, '_throttle_live', lambda self, d: _live(d)), \
            patch('api.views._cgminer_command', return_value='{"STATUS":[{"Msg":"ASC 0 set OK"}]}') as cmd:
        yield cmd


@pytest.mark.parametrize('body', [{'clock_mhz': 300}, {'reset': True}, {'target_ths': 100}])
def test_a15_throttle_abolished(client, miner, body):
    dev = _device('A15', 'Avalon A15')
    res = client.post('/api/devices/%d/throttle/' % dev.id, body, format='json')
    assert res.status_code == 400
    assert res.data['error'] == 'Die Drosselung des A15 ist abgeschafft; den Takt regelt die Temperatur-Automatik.'
    assert not miner.called
    assert 'active' not in Device.objects.get(pk=dev.pk).connection_config['throttle']


def test_a15_throttle_still_shown(client):
    dev = _device('A15', 'Avalon A15')
    dev.connection_config['throttle']['last_seen'] = {'points': STOCK, 'mode_key': '2/L4',
                                                      'mode_label': 'Power Mode 04', 'ghsspd_ths': 150.0}
    dev.save()
    res = client.get('/api/devices/%d/throttle/' % dev.id)
    assert res.status_code == 200 and res.data['family'] == 'a15' and res.data['max_mhz'] == 360


def test_nano_still_needs_target_hashrate(client, miner):
    dev = _device('nano3s-1', 'Avalon Nano 3S')
    res = client.post('/api/devices/%d/throttle/' % dev.id, {'clock_mhz': 300}, format='json')
    assert res.status_code == 409          # mode not measured yet: TH/s path unchanged
    assert not miner.called


def test_nano_reset_sends_stock_clock(client, miner):
    dev = _device('nano3s-1', 'Avalon Nano 3S')
    dev.connection_config['throttle']['active'] = {'target_ths': 3.0, 'profile': '2/L4',
                                                   'applied_points': [300, 300, 300]}
    dev.save()
    res = client.post('/api/devices/%d/throttle/' % dev.id, {'reset': True}, format='json')
    assert res.status_code == 200, res.data
    assert '0,frequency,360:362:358-0-0' in miner.call_args[0][2]
    assert Device.objects.get(pk=dev.pk).connection_config['throttle']['active'] is None


def test_migration_drops_old_a15_throttle(db):
    import importlib
    from django.apps import apps
    a15, nano = _device('A15', 'Avalon A15'), _device('nano3s-1', 'Avalon Nano 3S')
    a15.connection_config['throttle'].update(rev=3, active={'clock_mhz': 164.0, 'profile': '2/L4', 'status': 'manual',
                                                            'applied_points': [164, 165, 163]})
    a15.save()
    nano.connection_config['throttle']['active'] = {'target_ths': 3.0, 'profile': '2/L4', 'applied_points': [300]}
    nano.save()
    importlib.import_module('api.migrations.0024_automation_logging').drop_a15_throttle(apps, None)
    st = Device.objects.get(pk=a15.pk).connection_config['throttle']
    assert st['active'] is None and st['rev'] == 4 and st['last_event']['clock_mhz'] == 164.0
    assert st['profiles']['2/L4']['points'] == STOCK                       # nothing else changed
    assert Device.objects.get(pk=nano.pk).connection_config['throttle']['active']['target_ths'] == 3.0


def test_manual_points_listed_in_calibration(client):
    dev = _device('A15', 'Avalon A15')
    CalibrationPoint.objects.create(device=dev, kind='manual', mode_key='2/L4', mode_label='Power Mode 04',
                                    set_mhz=300.0, hashrate_ths=130.0)
    res = client.get('/api/devices/%d/calibration/' % dev.id)
    assert res.status_code == 200
    assert [p['set_mhz'] for p in res.data['manual']] == [300.0]
