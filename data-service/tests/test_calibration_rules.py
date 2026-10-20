"""Takt-Tabellen: stability +-3 % with the stored spread; resuming a max run switches away and back."""
from datetime import datetime, timedelta, timezone

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
