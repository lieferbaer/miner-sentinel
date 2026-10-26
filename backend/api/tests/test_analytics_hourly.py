"""Analytics over 24 h from the hourly values (device_stats_hourly, Nutzer 26.10.): same answer shape as from the raw
values, means weighted with the samples of each hour."""
from datetime import timedelta

import pytest
from api.models import Device, DeviceMiningStats, DeviceStatsHourly
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='hourly', password='testpass123'))
    return c


@pytest.fixture
def a15(db):
    return Device.objects.create(make='avalon', device_id='A15', name='Avalon A15', ip_address='10.0.0.15', port=4028,
                                 protocol=Device.PROTOCOL_CGMINER_TCP)


def _hour(dev, ago_h, hashrate, samples, temp, power, acc=(100, 200), chip_n=None):
    hour = (timezone.now() - timedelta(hours=ago_h)).replace(minute=0, second=0, microsecond=0)
    return DeviceStatsHourly.objects.create(
        device=dev, hour=hour, mining_samples=samples, first_at=hour, last_at=hour + timedelta(minutes=59),
        hashrate_avg=hashrate, hashrate_max=hashrate + 10, hashrate_min=hashrate - 10, best_difficulty=1e9,
        shares_accepted_min=acc[0], shares_accepted_max=acc[1], shares_rejected_min=0, shares_rejected_max=2,
        hardware_samples=samples, temperature_avg=temp, temperature_max=temp + 3, temperature_n=samples,
        chip_temperature_avg=temp + 10, chip_temperature_max=temp + 15, chip_temperature_n=chip_n if chip_n is not None
        else samples, power_avg=power, power_max=power + 50, power_n=samples, efficiency_avg=18.0, efficiency_n=samples)


def test_seven_days_come_from_the_hourly_values(client, a15):
    _hour(a15, 30, 120000.0, 300, 70.0, 2200.0, acc=(100, 150))
    _hour(a15, 29, 140000.0, 100, 74.0, 2400.0, acc=(150, 220))
    # raw values only for the last hours: over 24 h they are not used
    DeviceMiningStats.objects.create(device=a15, recorded_at=timezone.now(), hashrate_ghs=1.0)
    res = client.get('/api/analytics/devices/', {'hours': 168})
    assert res.status_code == 200
    dev = next(d for d in res.data['devices'] if d['device']['id'] == a15.id)
    series = dev['series']
    assert [round(p['hashrate_ghs']) for p in series] == [120000, 140000]
    summary = dev['summary']
    assert round(summary['avg_hashrate_ghs']) == 125000                    # (120000·300 + 140000·100) / 400
    assert summary['max_hashrate_ghs'] == 140010 and summary['min_hashrate_ghs'] == 119990
    assert summary['shares_accepted'] == 120 and summary['samples'] == 400
    assert round(summary['avg_power_watts']) == 2250 and summary['max_chip_temperature_c'] == 89.0


def test_thirty_days_are_grouped_per_day(client, a15):
    for h in range(48, 72):
        _hour(a15, h, 130000.0, 360, 72.0, 2300.0)
    res = client.get('/api/analytics/devices/', {'hours': 720})
    assert res.status_code == 200
    dev = next(d for d in res.data['devices'] if d['device']['id'] == a15.id)
    assert res.data['window']['granularity'] == 'day'
    assert 1 <= len(dev['series']) <= 2 and all(round(p['hashrate_ghs']) == 130000 for p in dev['series'])
    assert dev['summary']['samples'] == 24 * 360


def test_means_weighted_by_the_values_behind_them(client, a15):
    """Review 26.10.: hours with missing chip temperatures must not count with all their rows."""
    _hour(a15, 30, 120000.0, 360, 50.0, 2200.0, chip_n=60)        # chip 60 °C from only 60 values
    _hour(a15, 29, 120000.0, 360, 70.0, 2200.0)                   # chip 80 °C from 360 values
    dev = client.get('/api/analytics/devices/', {'hours': 168}).data['devices'][0]
    assert round(dev['summary']['avg_chip_temperature_c'], 1) == round((60 * 60 + 80 * 360) / 420, 1)


def test_hour_starting_at_the_end_is_not_included(client, a15):
    end = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=10)
    start = end - timedelta(hours=48)
    for h in (58, 11, 10):                                        # in, last hour inside, starts at the end
        _hour(a15, h, 100000.0 + h, 360, 70.0, 2200.0)
    res = client.get('/api/analytics/devices/', {'from': start.isoformat(), 'to': end.isoformat()})
    dev = res.data['devices'][0]
    assert [round(p['hashrate_ghs']) for p in dev['series']] == [100058, 100011]

