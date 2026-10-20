"""Drosselung A15: measured point after 15 min with an unchanged manual clock."""
from datetime import datetime, timedelta, timezone

import pytest

import calibration
import throttle_controller as tc

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
DEV = {'id': 3, 'ip_address': '10.0.0.15', 'port': 4028}


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port: {
        'ghsspd': 130000.0, 'status': 'Work: In Work', 'mode_key': '2/L4'})
    monkeypatch.setattr(calibration, 'save_manual_point',
                        lambda conn, device_id, m, key, points: calls.append((device_id, key, points)))
    return calls


def _active(minutes_ago, last_speed=129.0):
    return {'clock_mhz': 300.0, 'applied_points': [300, 302, 298], 'last_speed': last_speed,
            'last_change_at': (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def test_point_after_15_min_on_fresh_value(saved):
    active = _active(16)
    tc._manual_point(None, DEV, active, {'ghsspd_ths': 130.0}, '2/L4', NOW)
    assert saved == [(3, '2/L4', [300, 302, 298])]
    assert active['manual_measured_at'] == NOW.isoformat()
    tc._manual_point(None, DEV, active, {'ghsspd_ths': 131.0}, '2/L4', NOW)
    assert len(saved) == 1                       # once per setting


def test_no_point_before_15_min(saved):
    tc._manual_point(None, DEV, _active(14), {'ghsspd_ths': 130.0}, '2/L4', NOW)
    assert saved == []


def test_no_point_without_fresh_value(saved):
    tc._manual_point(None, DEV, _active(16, last_speed=130.0), {'ghsspd_ths': 130.0}, '2/L4', NOW)
    assert saved == []


def test_no_point_when_over_hot(saved, monkeypatch):
    monkeypatch.setattr(calibration, 'read_miner', lambda ip, port: {
        'ghsspd': 130000.0, 'status': 'Work: In Work, Over Hot', 'mode_key': '2/L4'})
    tc._manual_point(None, DEV, _active(16), {'ghsspd_ths': 130.0}, '2/L4', NOW)
    assert saved == []
