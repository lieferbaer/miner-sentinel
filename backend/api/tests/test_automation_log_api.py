"""Protokoll der Temperatur-Automatik: /api/automation/log/, log/<id>/, log/stats/, cycles/, export.csv."""
import csv
import io
from datetime import timedelta

import pytest
from api.models import AutomationCycle, AutomationLog, Device
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='loguser', password='testpass123'))
    return c


@pytest.fixture
def a15(db):
    return Device.objects.create(make='avalon', device_id='A15', name='Avalon A15', ip_address='10.0.0.15', port=4028,
                                 protocol=Device.PROTOCOL_CGMINER_TCP)


def _log(dev, ago_s, kind, **fields):
    return AutomationLog.objects.create(device=dev, recorded_at=timezone.now() - timedelta(seconds=ago_s), kind=kind,
                                        **fields)


def test_list_filters_and_pages(client, a15):
    for i in range(105):
        _log(a15, 1000 + i, 'takt', ok=True, response='x' * 300, clock_new=200 + i)
    _log(a15, 10, 'takt', ok=False, error='Zeitüberschreitung')
    _log(a15, 5, 'neustart', trigger='Laufzeit zurückgesetzt')
    _log(a15, 400 * 86400, 'takt', ok=True)                         # older than 360 days: never shown
    res = client.get('/api/automation/log/')
    assert res.status_code == 200
    assert (res.data['count'], res.data['page'], res.data['pages']) == (107, 1, 2)
    first = res.data['results'][0]
    assert first['kind'] == 'neustart' and first['device'] == a15.id      # newest first
    assert 'response' not in first and len(res.data['results']) == 100
    assert len(res.data['results'][2]['response_short']) == 200
    assert len(client.get('/api/automation/log/?page=2').data['results']) == 7
    assert client.get('/api/automation/log/?page=9').data['page'] == 2
    assert client.get('/api/automation/log/?ok=false').data['count'] == 1
    assert [r['kind'] for r in client.get('/api/automation/log/?ok=null').data['results']] == ['neustart']
    assert client.get('/api/automation/log/?kind=neustart,fehler').data['count'] == 1
    since = (timezone.now() - timedelta(seconds=60)).isoformat()
    assert client.get('/api/automation/log/', {'from': since}).data['count'] == 2
    assert client.get('/api/automation/log/?from=' + since).data['count'] == 2      # '+' not encoded
    assert client.get('/api/automation/log/?ok=vielleicht').status_code == 400
    assert client.get('/api/automation/log/?from=gestern').status_code == 400


def test_detail(client, a15):
    entry = _log(a15, 5, 'takt', ok=True, response='set OK ' * 100, thermal=[83, 82, 82], details={'schritt': -1},
                 temp_before=84)
    res = client.get('/api/automation/log/%d/' % entry.id)
    assert res.status_code == 200
    assert res.data['response'] == 'set OK ' * 100 and res.data['thermal'] == [83, 82, 82]
    assert res.data['details'] == {'schritt': -1} and res.data['temp_before'] == 84
    assert client.get('/api/automation/log/999999/').status_code == 404


def test_stats(client, a15):
    _log(a15, 50, 'modus', ok=True, mode_old='2/L4', mode_new='2/L3', api_ms=100, takeover_s=20.0,
         takeover_state='gemessen')
    _log(a15, 40, 'modus', ok=True, mode_old='2/L4', mode_new='2/L3', api_ms=300, takeover_s=40.0,
         takeover_state='gemessen')
    _log(a15, 30, 'modus', ok=True, mode_old='2/L4', mode_new='2/L3', api_ms=200, takeover_state='nicht gemessen')
    _log(a15, 30, 'modus', ok=False, mode_old='2/L3', mode_new='2/L2', api_ms=5000)     # failed: not counted
    thermal = [84, 83, 83, 82, 82, 81, 81, 80, 80, 79]
    _log(a15, 20, 'takt', ok=True, clock_new=250.4, api_ms=40, takeover_s=6.0, takeover_state='gemessen',
         temp_before=85, thermal=thermal)
    _log(a15, 10, 'takt', ok=True, clock_new=250, api_ms=60, takeover_s=8.0, takeover_state='gemessen',
         temp_before=84, thermal=[84, 84])
    _log(a15, 10, 'takt', ok=None, clock_new=250)                                        # no command sent
    _log(a15, 5, 'luefter', ok=True, fan_new=4, api_ms=900, takeover_s=12.5, takeover_state='gemessen')
    data = client.get('/api/automation/log/stats/').data
    assert data['modes'] == [{'von': '2/L4', 'nach': '2/L3', 'anzahl': 3, 'api_ms_mittel': 200.0,
                              'api_ms_median': 200.0, 'uebernahme_s_mittel': 30.0, 'uebernahme_s_median': 30.0,
                              'uebernahme_min': 20.0, 'uebernahme_max': 40.0}]
    assert data['clocks'] == [{'takt': 250, 'anzahl': 2, 'api_ms_mittel': 50.0, 'api_ms_median': 50.0,
                               'uebernahme_s_mittel': 7.0, 'uebernahme_s_median': 7.0,
                               'thermisch_1min': -1.0, 'thermisch_3min': -4.0, 'thermisch_5min': -6.0}]
    assert data['fans'] == [{'stufe': 4, 'anzahl': 1, 'api_ms_mittel': 900.0, 'api_ms_median': 900.0,
                             'uebernahme_s_mittel': 12.5, 'uebernahme_s_median': 12.5}]
    old = (timezone.now() - timedelta(days=1)).isoformat()
    assert client.get('/api/automation/log/stats/', {'to': old}).data['modes'] == []


def test_cycles_raw_and_per_minute(client, a15):
    base = (timezone.now() - timedelta(minutes=1)).replace(second=30, microsecond=0)
    for i in range(24):                                  # every 5 s, base - 115 s ... base
        AutomationCycle.objects.create(
            device=a15, recorded_at=base - timedelta(seconds=5 * i), phase='normal' if i else 'korrektur',
            chip_temp=80 + (i % 2), clock_set=240, clock_reported=241, fan_level=3 if i else 4, base_level=3,
            decision='Takt 240 MHz gesendet' if i == 3 else '')
    _log(a15, 20, 'takt', ok=True, trigger='Feintuning')
    _log(a15, 15, 'lernen_start')                        # not shown in the chart
    _log(a15, 9 * 3600, 'neustart')                     # outside the default 2 h
    data = client.get('/api/automation/cycles/').data
    assert data['bucket_s'] == 5 and len(data['points']) == 24
    assert data['points'][-1]['phase'] == 'korrektur' and data['points'][0]['chip_temp'] == 81
    assert [p.get('decision') for p in data['points'] if p.get('decision')] == ['Takt 240 MHz gesendet']
    assert [(e['kind'], e['trigger']) for e in data['events']] == [('takt', 'Feintuning')]

    since = (base - timedelta(hours=12)).isoformat()
    data = client.get('/api/automation/cycles/', {'from': since}).data
    assert data['bucket_s'] == 60
    minutes = data['points']
    full = base.replace(second=0)
    assert [p['t'] for p in minutes] == [full - timedelta(minutes=2), full - timedelta(minutes=1), full]
    assert [p['chip_temp'] for p in minutes] == [80.6, 80.5, 80.4]          # 5, 12 and 7 rounds
    assert minutes[0]['clock_set'] == 240.0 and minutes[0]['clock_reported'] == 241.0
    last = minutes[-1]
    assert last['phase'] == 'korrektur' and last['fan_level'] == 4 and last['base_level'] == 3
    assert minutes[1]['phase'] == 'normal' and 'decision' not in minutes[1]
    assert minutes[2]['decision'] == 'Takt 240 MHz gesendet'
    assert [e['kind'] for e in data['events']] == ['neustart', 'takt']


def test_export_csv(client, a15):
    _log(a15, 30, 'takt', ok=True, clock_new=250, details={'punkte': [230, 250]}, trigger='Feintuning; −1 MHz')
    AutomationCycle.objects.create(device=a15, recorded_at=timezone.now(), phase='normal', chip_temp=82,
                                   boards=[82, 78, 76], window_active=True)
    res = client.get('/api/automation/export.csv')
    assert res.status_code == 200
    body = b''.join(res.streaming_content).decode('utf-8')
    assert body.startswith('﻿')
    rows = list(csv.reader(io.StringIO(body[1:]), delimiter=';'))
    assert rows[0][:4] == ['id', 'device_id', 'recorded_at', 'kind'] and 'response' in rows[0]
    row = dict(zip(rows[0], rows[1]))
    assert row['ok'] == 'true' and row['details'] == '{"punkte": [230, 250]}'
    assert row['trigger'] == 'Feintuning; −1 MHz'
    assert 'attachment; filename="automation_log_' in res['Content-Disposition']
    res = client.get('/api/automation/export.csv', {'table': 'cycle'})
    rows = list(csv.reader(io.StringIO(b''.join(res.streaming_content).decode('utf-8')[1:]), delimiter=';'))
    row = dict(zip(rows[0], rows[1]))
    assert row['boards'] == '[82, 78, 76]' and row['window_active'] == 'true' and row['chip_temp'] == '82.0'
    assert 'automation_cycle_' in res['Content-Disposition']
    assert client.get('/api/automation/export.csv', {'table': 'x'}).status_code == 400
    future = (timezone.now() + timedelta(days=100)).isoformat()
    assert client.get('/api/automation/export.csv', {'to': future}).status_code == 400      # more than 400 days


@pytest.mark.parametrize('param', [{'from': '2026-13-01T00:00:00'}, {'to': '2026-02-30T10:00:00'}])
def test_invalid_date_is_400(client, a15, param):
    for url in ('/api/automation/log/', '/api/automation/log/stats/', '/api/automation/cycles/',
                '/api/automation/export.csv'):
        res = client.get(url, param)
        assert res.status_code == 400 and res.data['error'].startswith('Ungültige Zeitangabe')


def test_needs_login(db):
    for url in ('/api/automation/log/', '/api/automation/log/1/', '/api/automation/log/stats/',
                '/api/automation/cycles/', '/api/automation/export.csv'):
        assert APIClient().get(url).status_code in (401, 403)
