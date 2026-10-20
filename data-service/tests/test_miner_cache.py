"""Shared miner readings (miner_cache) and their use by the Takt-Tabellen and the throttle controller."""
import json
import threading
import time

import pytest

import calibration
import miner_cache
import throttle_controller

IP = '10.0.0.15'
ESTATS = 'MM ID0=Ver[15-xx] WORKMODE[2] SF0[400 410 420] GHSspd[150000] WALLPOWER[2200] SYSTEMSTATU[Work: In Work]'
SUMMARY = json.dumps({'SUMMARY': [{'Elapsed': 1000, 'MHS 5m': 150000000}]})
CONF = {'mode': 2, 'power_level': 4, 'pool1': 'stratum+tcp://pool', 'worker1': 'onr.A15', 'passwd1': 'x'}


@pytest.fixture(autouse=True)
def empty_cache():
    miner_cache.clear(IP)
    yield
    miner_cache.clear(IP)


def test_get_respects_max_age(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(miner_cache.time, 'monotonic', lambda: now[0])
    miner_cache.put(IP, 'estats', 'a')
    now[0] = 110.0
    assert miner_cache.get(IP, 'estats', 15) == 'a'
    now[0] = 116.0
    assert miner_cache.get(IP, 'estats', 15) is None


def test_fetch_reads_only_when_needed():
    calls = []
    read = lambda: calls.append(1) or 'value'
    assert miner_cache.fetch(IP, 'k', 15, read) == 'value'
    assert miner_cache.fetch(IP, 'k', 15, read) == 'value'
    assert len(calls) == 1
    assert miner_cache.fetch(IP, 'k', None, read) == 'value'      # None: always read
    assert len(calls) == 2


def test_fetch_does_not_store_empty_answers():
    miner_cache.fetch(IP, 'k', 15, lambda: '')
    assert miner_cache.get(IP, 'k', 15) is None


def test_command_clears_readings_also_on_error():
    miner_cache.put(IP, 'estats', 'old')
    with pytest.raises(ConnectionResetError):
        with miner_cache.command(IP):
            raise ConnectionResetError('reset by peer')
    assert miner_cache.get(IP, 'estats', 15) is None


def test_one_request_at_a_time():
    active, peak = [0], [0]

    def request():
        with miner_cache.lock(IP):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            active[0] -= 1

    threads = [threading.Thread(target=request) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_no_old_reading_after_a_command():
    """A reading that started before a command must not be stored after the command cleared the cache."""
    started = threading.Event()

    def slow_read():
        started.set()
        time.sleep(0.05)
        return 'before command'

    reader = threading.Thread(target=lambda: miner_cache.fetch(IP, 'estats', 15, slow_read))
    reader.start()
    started.wait()
    with miner_cache.command(IP):
        pass
    reader.join()
    # the command waited for the reading; the reading was stored before it and cleared by it
    assert miner_cache.get(IP, 'estats', 15) is None


def _count_calibration_requests(monkeypatch):
    calls = []

    def fake_cmd(ip, port, payload, timeout=8):
        calls.append(json.loads(payload)['command'])
        return ESTATS if 'estats' in payload else SUMMARY

    def fake_conf(ip):
        calls.append('web')
        return dict(CONF)

    monkeypatch.setattr(calibration, '_cmd', fake_cmd)
    monkeypatch.setattr(calibration, 'a15_conf', fake_conf)
    return calls


def test_calibration_uses_collector_readings(monkeypatch):
    calls = _count_calibration_requests(monkeypatch)
    miner_cache.put(IP, 'estats', ESTATS)
    miner_cache.put(IP, 'summary', SUMMARY)
    miner_cache.put(IP, 'web', dict(CONF))
    m = calibration.read_miner(IP, 4028)
    assert calls == []
    assert m['mode_key'] == '2/L4' and m['points'] == [400, 410, 420] and m['elapsed'] == 1000


def test_calibration_reads_fresh_while_measuring(monkeypatch):
    calls = _count_calibration_requests(monkeypatch)
    miner_cache.put(IP, 'estats', ESTATS)
    miner_cache.put(IP, 'summary', SUMMARY)
    miner_cache.put(IP, 'web', dict(CONF))
    calibration.read_miner(IP, 4028, fresh=True)
    assert calls == ['estats', 'summary']        # web interface settings from the stored reading


def test_calibration_mode_switch_clears_readings(monkeypatch):
    miner_cache.put(IP, 'web', dict(CONF))
    monkeypatch.setattr(calibration.urllib.request, 'urlopen',
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionResetError('reset')))
    calibration.a15_set_mode(IP, CONF, 2, 3)     # dropped connection: logged, checked and resent later
    assert miner_cache.get(IP, 'web', 15) is None


def test_throttle_reads_fresh_only_while_regulating(monkeypatch):
    calls = []

    def fake_cmd(ip, port, payload, timeout=6):
        calls.append(payload['command'])
        return ESTATS if payload['command'] == 'estats' else SUMMARY

    monkeypatch.setattr(throttle_controller, '_cmd', fake_cmd)
    miner_cache.put(IP, 'estats', ESTATS)
    miner_cache.put(IP, 'summary', SUMMARY)
    m = throttle_controller.read_miner(IP, 4028)
    assert calls == [] and m['ghsspd_ths'] == 150.0
    throttle_controller.read_miner(IP, 4028, fresh=True)
    assert calls == ['estats', 'summary']


def test_calibration_fresh_estats_only(monkeypatch):
    calls = _count_calibration_requests(monkeypatch)
    miner_cache.put(IP, 'estats', ESTATS)
    miner_cache.put(IP, 'summary', SUMMARY)
    miner_cache.put(IP, 'web', dict(CONF))
    calibration.read_miner(IP, 4028, fresh='estats')
    assert calls == ['estats']                   # Temperatur-Automatik every 5 s: one request
