"""/api/live/: one payload with everything the web UI shows live (database only)."""
import pytest
from django.utils import timezone
from api.models import CalibrationRun, Device, DeviceHardwareStats, DeviceMiningStats, FanAutomation
from django.contrib.auth.models import User
from rest_framework.test import APIClient


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='liveuser', password='testpass123'))
    return c


@pytest.fixture
def a15(db):
    dev = Device.objects.create(
        make='avalon', device_id='A15', name='Avalon A15', ip_address='10.0.0.15', port=4028,
        protocol=Device.PROTOCOL_CGMINER_TCP,
        connection_config={'throttle': {'freq_ok': True, 'last_seen': {
            'points': [300, 300, 300], 'mode_key': '2/L4', 'mode_label': 'Power Mode 04', 'ghsspd_ths': 140.0,
            'at': '2026-10-21T00:00:00+00:00'}, 'profiles': {'2/L4': {'points': [360, 360, 360], 'avg_mhz': 360.0}}}})
    DeviceMiningStats.objects.create(device=dev, hashrate_ghs=140000, recorded_at=timezone.now())
    DeviceHardwareStats.objects.create(device=dev, power_watts=2400, temperature_chip_c=82, recorded_at=timezone.now())
    CalibrationRun.objects.create(device=dev, kind='step', modes=['2/L4'], status='paused', state={})
    return dev


def test_live_payload(client, a15):
    res = client.get('/api/live/')
    assert res.status_code == 200
    data = res.data
    assert [d['device_id'] for d in data['devices']] == ['A15']
    assert data['mining'][0]['device_id_str'] == 'A15'
    assert data['hardware'][0]['device_id_str'] == 'A15'
    assert 'A15' in data['system']
    assert data['throttle'][a15.id]['family'] == 'a15' and data['throttle'][a15.id]['max_mhz'] == 360
    assert data['automation']['device']['id'] == a15.id
    assert data['calibration']['run']['status'] == 'paused'
    assert data['server_now']


def test_live_automation_clock(client, a15):
    FanAutomation.objects.create(device=a15, ac_dev_id='dev1', ac_port=1, level=3, enabled=True, state={
        'v': 2, 'phase': 'normal', 't': 82, 'clock_set': 240, 'clock_changed_at': timezone.now().isoformat(),
        'window_active': False})
    data = client.get('/api/live/').data
    assert data['automation']['state']['phase'] == 'normal' and data['automation']['state']['clock_set'] == 240
    throttle = data['throttle'][a15.id]
    assert (throttle['set_mhz'], throttle['set_by'], throttle['automation_active']) == (240, 'Automatik', True)


def test_live_needs_login(db):
    assert APIClient().get('/api/live/').status_code in (401, 403)
