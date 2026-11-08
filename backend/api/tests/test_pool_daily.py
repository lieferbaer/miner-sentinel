"""Balance und Pool-Hashrate je Tag (v2.2.9, Nutzer 08.11.): GET /api/analytics/pool-daily/ – always the last 160
German calendar days (Europe/Berlin) up to today, per day the value of the day's last hour that has one."""
import json
from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from api.models import Device, PoolStats, PoolStatsHourly
from django.contrib.auth.models import User
from rest_framework.test import APIClient

URL = '/api/analytics/pool-daily/'
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=dt_timezone.utc)          # winter time: Berlin = UTC+1
TODAY = date(2026, 11, 8)
FIRST = TODAY - timedelta(days=159)


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr('django.utils.timezone.now', lambda: NOW)


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='pooldaily', password='testpass123'))
    return c


@pytest.fixture
def a15(db):
    dev = Device.objects.create(
        make='avalon', device_id='A15', name='Avalon A15', ip_address='10.0.0.15', port=4028,
        protocol=Device.PROTOCOL_CGMINER_TCP,
        connection_config={'pool_stats': {'type': 'f2pool', 'address': 'onr', 'token': 'secret-token-123',
                                          'url': 'https://api.f2pool.com', 'worker': 'a15'}})
    PoolStats.objects.create(pool_type='f2pool', pool_address='onr', recorded_at=NOW - timedelta(minutes=3),
                             details={'balance': {'balance': '0.001'}})
    return dev


def hour_row(at, balance=None, hashrate=None, pool_type='f2pool', address='onr'):
    return PoolStatsHourly.objects.create(pool_type=pool_type, pool_address=address, hour=at, samples=12,
                                          balance_last=balance, hashrate_1d_last=hashrate)


def series_of(res, account='onr'):
    acc = next(a for a in res.data['accounts'] if a['account'] == account)
    return {p['day']: p for p in acc['series']}


def test_dense_160_days_ending_today(client, a15):
    res = client.get(URL)
    assert res.status_code == 200
    assert res.data['days'] == 160
    assert res.data['start'] == FIRST.isoformat() == '2026-06-02' and res.data['end'] == '2026-11-08'
    acc = res.data['accounts']
    assert len(acc) == 1 and acc[0]['pool_type'] == 'f2pool' and acc[0]['account'] == 'onr'
    days = [p['day'] for p in acc[0]['series']]
    assert days == [(FIRST + timedelta(days=i)).isoformat() for i in range(160)]
    assert all(p['balance_btc'] is None and p['hashrate_ghs'] is None for p in acc[0]['series'])


def test_berlin_day_boundary(client, a15):
    hour_row(utc(2026, 11, 6, 22, 0), balance=0.1)              # 23:00 Berlin → 06.11.
    hour_row(utc(2026, 11, 6, 23, 0), hashrate=200000.0)        # 23:30 UTC is 00:30 Berlin → 07.11. (winter)
    hour_row(utc(2026, 7, 1, 21, 0), balance=0.2)               # summer: 23:00 Berlin → 01.07.
    hour_row(utc(2026, 7, 1, 22, 0), balance=0.3)               # summer: 00:00 Berlin → 02.07.
    s = series_of(client.get(URL))
    assert s['2026-11-06']['balance_btc'] == 0.1 and s['2026-11-06']['hashrate_ghs'] is None
    assert s['2026-11-07']['hashrate_ghs'] == 200000.0 and s['2026-11-07']['balance_btc'] is None
    assert s['2026-07-01']['balance_btc'] == 0.2 and s['2026-07-02']['balance_btc'] == 0.3


def test_last_value_of_the_day(client, a15):
    """ak A / al B: the balance and the 24-h hashrate of the day's last hour that has one (end of the day)."""
    hour_row(utc(2026, 11, 5, 8, 0), balance=0.0010, hashrate=190000.0)
    hour_row(utc(2026, 11, 5, 20, 0), balance=0.0014, hashrate=None)
    hour_row(utc(2026, 11, 5, 21, 0), balance=None, hashrate=205000.0)
    hour_row(utc(2026, 11, 5, 22, 0), balance=None, hashrate=None)     # 23:00 Berlin, nothing reported
    s = series_of(client.get(URL))
    assert s['2026-11-05'] == {'day': '2026-11-05', 'balance_btc': 0.0014, 'hashrate_ghs': 205000.0}


def test_null_days_and_today_partial(client, a15):
    hour_row(utc(2026, 11, 3, 10, 0), balance=0.001, hashrate=180000.0)
    hour_row(utc(2026, 11, 8, 9, 0), balance=0.002, hashrate=210000.0)   # today, partial
    s = series_of(client.get(URL))
    assert s['2026-11-03']['balance_btc'] == 0.001
    assert s['2026-11-04'] == {'day': '2026-11-04', 'balance_btc': None, 'hashrate_ghs': None}
    assert s['2026-11-08']['balance_btc'] == 0.002 and s['2026-11-08']['hashrate_ghs'] == 210000.0


def test_window_is_160_berlin_days(client, a15):
    hour_row(utc(2026, 6, 1, 22, 0), balance=0.5)               # 02.06. 00:00 Berlin (summer) → first day
    hour_row(utc(2026, 6, 1, 21, 0), balance=0.4)               # 01.06. → before the window
    hour_row(utc(2026, 11, 8, 23, 0), balance=0.6)              # 09.11. Berlin → tomorrow, not in the window
    res = client.get(URL)
    s = series_of(res)
    assert s['2026-06-02']['balance_btc'] == 0.5 and len(s) == 160
    assert '2026-06-01' not in s and '2026-11-09' not in s and s['2026-11-08']['balance_btc'] is None


def test_accounts_like_device_analytics_and_no_secrets(client, a15):
    # second device on the same account → one account; a device without pool → none
    Device.objects.create(make='avalon', device_id='A15b', name='Avalon A15 b', ip_address='10.0.0.16', port=4028,
                          protocol=Device.PROTOCOL_CGMINER_TCP,
                          connection_config={'pool_stats': {'type': 'f2pool', 'address': 'onr', 'token': 'other-tok'}})
    Device.objects.create(make='bitaxe', device_id='bx', name='Bitaxe', ip_address='10.0.0.17', port=80,
                          connection_config={})
    # an account without any pool values is left out, like on the AccountCard
    Device.objects.create(make='bitaxe', device_id='bx2', name='Bitaxe 2', ip_address='10.0.0.18', port=80,
                          connection_config={'pool_stats': {'type': 'ckpool', 'address': 'bc1qnodata'}})
    hour_row(utc(2026, 11, 7, 10, 0), balance=0.001, hashrate=200000.0)
    hour_row(utc(2026, 11, 7, 10, 0), balance=9.0, hashrate=1.0, address='someone-else')
    res = client.get(URL)
    assert [(a['pool_type'], a['account']) for a in res.data['accounts']] == [('f2pool', 'onr')]
    assert series_of(res)['2026-11-07']['balance_btc'] == 0.001
    raw = json.dumps(res.data)
    assert 'secret-token-123' not in raw and 'other-tok' not in raw and 'token' not in raw
    assert 'connection_config' not in raw and 'api.f2pool.com' not in raw
    assert set(res.data['accounts'][0]) == {'pool_type', 'account', 'series'}


def test_requires_login(db):
    res = APIClient().get(URL)
    assert res.status_code in (401, 403)
