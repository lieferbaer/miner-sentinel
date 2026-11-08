"""Shared A15 access (calibration.py): the last settings stand in while only the settings page does not answer, a
mode switch is never sent with them; the Takt-Messung itself is removed (v2.2.7, Nutzer 06.11.)."""
import pytest

import calibration


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
