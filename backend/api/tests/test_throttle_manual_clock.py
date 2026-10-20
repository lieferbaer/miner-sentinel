"""Drosselung A15: manual clock (MHz) instead of a target hashrate; Nano/Q unchanged."""
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


def test_a15_sets_manual_clock(client, miner):
    dev = _device('A15', 'Avalon A15')
    res = client.post('/api/devices/%d/throttle/' % dev.id, {'clock_mhz': 300}, format='json')
    assert res.status_code == 200, res.data
    sent = miner.call_args[0][2]
    assert '0,frequency,300:302:298-0-0' in sent
    active = Device.objects.get(pk=dev.pk).connection_config['throttle']['active']
    assert active['clock_mhz'] == 300.0 and active['status'] == 'manual'
    assert res.data['min_mhz'] == 54 and res.data['max_mhz'] == 360
    assert res.data['active']['clock_mhz'] == 300.0


@pytest.mark.parametrize('clock', [40, 361, 'abc'])
def test_a15_clock_out_of_range(client, miner, clock):
    dev = _device('A15', 'Avalon A15')
    res = client.post('/api/devices/%d/throttle/' % dev.id, {'clock_mhz': clock}, format='json')
    assert res.status_code == 400
    assert not miner.called


def test_nano_still_needs_target_hashrate(client, miner):
    dev = _device('nano3s-1', 'Avalon Nano 3S')
    res = client.post('/api/devices/%d/throttle/' % dev.id, {'clock_mhz': 300}, format='json')
    assert res.status_code == 409          # mode not measured yet: TH/s path unchanged
    assert not miner.called


def test_manual_points_listed_in_calibration(client):
    dev = _device('A15', 'Avalon A15')
    CalibrationPoint.objects.create(device=dev, kind='manual', mode_key='2/L4', mode_label='Power Mode 04',
                                    set_mhz=300.0, hashrate_ths=130.0)
    res = client.get('/api/devices/%d/calibration/' % dev.id)
    assert res.status_code == 200
    assert [p['set_mhz'] for p in res.data['manual']] == [300.0]
