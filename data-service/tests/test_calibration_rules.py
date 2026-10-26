"""Takt-Tabellen: stability +-3 % with the stored spread; resuming a max run switches away and back."""
from datetime import datetime, timedelta, timezone

import pytest

import calibration

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _feed(values):
    st = {}
    stable = False
    for i, v in enumerate(values):
        stable = calibration._power_stable(st, v, T0 + timedelta(seconds=15 * i))
    return st, stable


def test_stable_within_3_percent_and_spread_stored():
    st, stable = _feed([2700, 2760, 2650, 2700] * 4)        # 13 readings = 180 s, max 2.2 % off the mean
    assert stable is True
    assert 2.0 < st['pw_spread'] < 2.5


def test_not_stable_beyond_3_percent():
    st, stable = _feed([2700] * 12 + [2850])
    assert stable is False and st['pw_spread'] > 3


def test_resume_max_run_switches_away_and_back(monkeypatch):
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port, fresh=False: {
        'points': [360, 360, 360], 'mode_key': '2/L4', 'status': 'Work: In Work', 'conf': {}, 'elapsed': 100})
    sent = []
    monkeypatch.setattr(calibration, 'a15_set_mode', lambda ip, conf, mode, level: sent.append((mode, level)))

    class Conn:
        def cursor(self):
            class Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def execute(self, *a):
                    pass
            return Cur()

    run = {'id': 7, 'kind': 'max', 'status': 'running', 'modes': ['2/L4'], 'mode_index': 0, 'step_index': 0,
           'ip_address': '10.0.0.15', 'port': 4028, 'message': '', 'pause_reason': '', 'finished_at': None,
           'state': {'orig_mode': '2/L4', 'phase': 'measure', 'applied': ['2/L4'], 'resume': True, 'count': 1}}
    run = calibration.step_run(Conn(), run)
    assert sent == [(2, 3)]                                   # bounce 04 -> 03 (and back afterwards)
    assert run['state']['phase'] == 'wait_mode' and run['state']['count'] == 0


class _Conn:
    def cursor(self):
        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a):
                pass

            def fetchone(self):
                return None
        return Cur()


def _measure_round(monkeypatch, step_index, dh):
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port, fresh=False: {
        'points': [150, 150, 150], 'mode_key': '2/L4', 'status': 'Work: In Work', 'conf': {}, 'elapsed': 100,
        'ghsspd': 78000.0, 'power': 1800.0, 'dh': dh, 'tmax': 78.0, 'fields': {}, 'summary': {}, 'estats': ''})
    monkeypatch.setattr(calibration, '_power_stable', lambda st, power, now: True)
    saved = []
    monkeypatch.setattr(calibration, '_save_point', lambda *a, **k: saved.append(a[5]))
    run = {'id': 7, 'kind': 'step', 'status': 'running', 'modes': ['2/L4'], 'mode_index': 0,
           'step_index': step_index, 'ip_address': '10.0.0.15', 'port': 4028, 'message': '', 'pause_reason': '',
           'finished_at': None,
           'state': {'orig_mode': '2/L4', 'phase': 'measure', 'applied': ['2/L4'], 'last_speed': 77000.0,
                     'changed_at': '2026-10-01T10:00:00+00:00', 'set_points': [150, 150, 150]}}
    return calibration.step_run(_Conn(), run), saved


def test_series_ends_at_10_percent_errors_from_point_10(monkeypatch):
    run, saved = _measure_round(monkeypatch, 9, 10.4)          # 10th point, 10.4 % errors
    assert saved == [9] and run['state']['phase'] == 'restore'        # point saved, then the end
    assert 'Fehlerrate 10.4' in run['message']


def test_high_errors_before_point_10_continue(monkeypatch):
    run, saved = _measure_round(monkeypatch, 0, 18.9)          # first point at the stock clock
    assert saved and run['state']['phase'] == 'set_clock'


def test_low_errors_continue(monkeypatch):
    run, saved = _measure_round(monkeypatch, 15, 5.0)
    assert run['state']['phase'] == 'set_clock'


def test_end_with_automation_no_switching(monkeypatch):
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port, fresh=False: {
        'points': [150, 150, 150], 'mode_key': '2/L4', 'status': 'Work: In Work', 'conf': {}, 'elapsed': 100})
    sent = []
    monkeypatch.setattr(calibration, 'a15_set_mode', lambda ip, conf, mode, level: sent.append((mode, level)))
    base = {'id': 7, 'kind': 'step', 'modes': ['2/L4'], 'mode_index': 0, 'step_index': 20,
            'ip_address': '10.0.0.15', 'port': 4028, 'message': '', 'pause_reason': '', 'finished_at': None}
    run = dict(base, status='cancelling', fan_auto=True,
               state={'orig_mode': '2/L4', 'phase': 'measure', 'applied': ['2/L4'], 'set_points': [150, 150, 150]})
    run = calibration.step_run(_Conn(), run)
    assert sent == [] and run['status'] == 'cancelled'        # clocks stay, the automation takes over
    run = dict(base, status='cancelling', fan_auto=False,
               state={'orig_mode': '2/L4', 'phase': 'measure', 'applied': ['2/L4'], 'set_points': [150, 150, 150]})
    run = calibration.step_run(_Conn(), run)
    assert sent == [(2, 3)]                                   # without automation: switch away and back as before


def test_read_miner_uses_last_settings_while_only_the_page_is_down(monkeypatch):
    """26.10.: antwortet nur die Einstellungsseite nicht, gelten die zuletzt gelesenen Einstellungen (als alt markiert);
    ein Moduswechsel wird damit nie gesendet."""
    estats = 'ID0 MM0 WORKMODE[2] SF0[300 320 340 360] TMax[80]'
    monkeypatch.setattr(calibration, '_cmd', lambda ip, port, payload, timeout=8: (
        '{"SUMMARY":[{"Elapsed":100}]}' if 'summary' in payload else 'STATS=0,' + estats))
    monkeypatch.setattr(calibration.miner_cache, 'fetch', lambda ip, key, age, read: read())
    monkeypatch.setattr(calibration.time, 'sleep', lambda s: None)
    calibration._last_conf.clear()
    monkeypatch.setattr(calibration, 'a15_conf', lambda ip: {'mode': 2, 'power_level': '03', 'pool1': 'p'})
    m = calibration.read_miner('10.0.0.15', 4028)
    assert m['conf_fresh'] and m['mode_key'] == '2/L3'

    def down(ip):
        raise OSError('timed out')
    monkeypatch.setattr(calibration, 'a15_conf', down)
    m = calibration.read_miner('10.0.0.15', 4028)
    assert not m['conf_fresh'] and m['mode_key'] == '2/L3' and 'Einstellungsseite' in m['web_error']
    with pytest.raises(RuntimeError):
        calibration.a15_set_mode('10.0.0.15', m['conf'], 2, 4)    # nie mit alten Pool-Daten
    calibration._last_conf.clear()
    m = calibration.read_miner('10.0.0.15', 4028)                 # ohne gelesene Einstellungen: Modus unbekannt
    assert m['conf'] is None and m['mode_key'] is None and not m['conf_fresh']


def test_calibration_mode_switch_waits_for_the_settings_page(monkeypatch):
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port, fresh=False: {
        'points': [360, 360, 360], 'mode_key': '2/L4', 'status': 'Work: In Work', 'conf': {'_stale': True},
        'conf_fresh': False, 'elapsed': 100})
    sent = []
    monkeypatch.setattr(calibration, 'a15_set_mode', lambda ip, conf, mode, level: sent.append((mode, level)))
    run = {'id': 7, 'kind': 'max', 'status': 'running', 'modes': ['2/L3'], 'mode_index': 0, 'step_index': 0,
           'ip_address': '10.0.0.15', 'port': 4028, 'message': '', 'pause_reason': '', 'finished_at': None,
           'state': {'orig_mode': '2/L4', 'phase': 'switch', 'applied': []}}
    run = calibration.step_run(_Conn(), run)
    assert sent == [] and run['message'] == 'Moduswechsel wartet: A15-Einstellungsseite antwortet nicht.'


def test_calibration_cancel_finishes_while_the_settings_page_is_down(monkeypatch):
    """Review 26.10.: nur wo ein Umschaltbefehl gesendet würde, wird gewartet – Abbrechen ohne Moduswechsel endet."""
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port, fresh=False: {
        'points': [360, 360, 360], 'mode_key': '2/L3', 'status': 'Work: In Work', 'conf': {'_stale': True},
        'conf_fresh': False, 'elapsed': 100})
    sent = []
    monkeypatch.setattr(calibration, 'a15_set_mode', lambda ip, conf, mode, level: sent.append((mode, level)))
    run = {'id': 7, 'kind': 'max', 'status': 'cancelling', 'modes': ['2/L3'], 'mode_index': 0, 'step_index': 0,
           'ip_address': '10.0.0.15', 'port': 4028, 'message': '', 'pause_reason': '', 'finished_at': None,
           'fan_auto': True, 'state': {'orig_mode': '2/L4', 'phase': 'measure', 'applied': ['2/L3']}}
    run = calibration.step_run(_Conn(), run)
    assert sent == [] and run['status'] == 'cancelled'


def test_calibration_wait_mode_gives_no_verdict_while_the_page_is_down(monkeypatch):
    """Review 26.10.: mit nicht lesbarem Modus kein „ließ sich nicht umschalten“ nach Ablauf der Zeit – warten."""
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port, fresh=False: {
        'points': [360, 360, 360], 'mode_key': '2/L4', 'status': 'Work: In Work', 'conf': {'_stale': True},
        'conf_fresh': False, 'elapsed': 100})
    sent = []
    monkeypatch.setattr(calibration, 'a15_set_mode', lambda ip, conf, mode, level: sent.append((mode, level)))
    old = (datetime.now(timezone.utc) - timedelta(seconds=calibration.SWITCH_TIMEOUT + 60)).isoformat()
    run = {'id': 7, 'kind': 'step', 'status': 'running', 'modes': ['2/L2'], 'mode_index': 0, 'step_index': 0,
           'ip_address': '10.0.0.15', 'port': 4028, 'message': '', 'pause_reason': '', 'finished_at': None,
           'state': {'orig_mode': '2/L4', 'phase': 'wait_mode', 'expect': '2/L2', 'after': 'target',
                     'switch_sent_at': old, 'post_at': old, 'applied': []}}
    run = calibration.step_run(_Conn(), run)
    assert sent == [] and run['state']['phase'] == 'wait_mode' and not run['state'].get('notes')


def test_settings_page_http_error_counts_like_a_timeout(monkeypatch):
    import http.client

    def broken(url, timeout=5):
        raise http.client.IncompleteRead(b'')
    monkeypatch.setattr(calibration.urllib.request, 'urlopen', broken)
    with pytest.raises(OSError):
        calibration.a15_conf('10.0.0.99')


def test_mode_switch_updates_the_last_settings(monkeypatch):
    class Reply:
        def read(self):
            return b'ok'
    monkeypatch.setattr(calibration.urllib.request, 'urlopen', lambda req, timeout=10: Reply())
    calibration._last_conf['10.0.0.98'] = {'mode': 2, 'power_level': '04', 'pool1': 'p'}
    calibration.a15_set_mode('10.0.0.98', {'mode': 2, 'power_level': '04', 'pool1': 'p'}, 2, 3)
    assert calibration._last_conf['10.0.0.98']['power_level'] == '03'
    calibration._last_conf.pop('10.0.0.98')

