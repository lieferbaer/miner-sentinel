"""Central reading: one reading per miner, then the automation; throttle + Takt-Tabellen every 3rd reading."""
import json

import pytest

import calibration
import live_reader
import miner_cache

A15 = {'id': 3, 'ip_address': '10.0.0.15', 'port': 4028, 'device_name': 'Avalon A15', 'model': '',
       'device_id': 'A15'}
NANO = {'id': 1, 'ip_address': '10.0.0.37', 'port': 4028, 'device_name': 'Avalon Nano 3S', 'model': '',
        'device_id': 'nano3s-1'}


@pytest.fixture(autouse=True)
def clean():
    for d in (A15, NANO):
        miner_cache.clear(d['ip_address'])
    yield


@pytest.fixture
def miners(monkeypatch):
    calls = []

    def cmd(ip, port, payload, timeout=8):
        calls.append((ip, json.loads(payload)['command']))
        if ip == NANO['ip_address']:
            raise OSError('Host is unreachable')
        return '{"STATUS":[{"STATUS":"S"}]}'

    monkeypatch.setattr(calibration, '_cmd', cmd)
    monkeypatch.setattr(calibration, 'a15_conf', lambda ip: calls.append((ip, 'web')) or {'mode': 2, 'power_level': 4})
    return calls


def test_read_device_stores_readings(miners):
    assert live_reader.read_device(A15) is True
    assert miners == [('10.0.0.15', 'estats'), ('10.0.0.15', 'summary'), ('10.0.0.15', 'web')]
    assert miner_cache.get('10.0.0.15', 'estats', 5) and miner_cache.get('10.0.0.15', 'web', 5)
    live_reader.read_device(A15)
    assert miners.count(('10.0.0.15', 'web')) == 1            # web settings only every 30 s


def test_offline_miner_does_not_stop_the_cycle(miners, monkeypatch):
    monkeypatch.setattr(live_reader, '_devices', lambda url: [NANO, A15])
    ran = []
    monkeypatch.setattr(live_reader, 'run_fan_automation', lambda url: ran.append('auto'))
    live_reader._cycle[0] = 0
    control = []
    for _ in range(3):
        live_reader.run_live_cycle('db', lambda url: control.append('throttle'), lambda url: control.append('cal'))
    live_reader._control_worker.submit(lambda: None).result()  # wait for the worker
    assert ran == ['auto'] * 3                                # automation after every reading
    assert control == ['throttle', 'cal']                      # every 3rd reading
    assert ('10.0.0.15', 'estats') in miners
