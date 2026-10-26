"""f2pool every 5 min as its own job (Nutzer 26.10.): only f2pool targets, a failure is reported as pool_down."""
import app


class FakeConn:
    def close(self):
        pass


def test_poll_f2pool_only_polls_f2pool_and_reports_failures(monkeypatch):
    monkeypatch.setattr(app, 'get_pool_targets', lambda: [('f2pool', 'onr', '', 'tok'), ('ckpool', 'bc1x', 'u', '')])
    monkeypatch.setattr(app.psycopg2, 'connect', lambda url: FakeConn())
    polled, alerts = [], []
    monkeypatch.setattr(app, 'collect_f2pool_data', lambda conn, address, token: polled.append((address, token)))
    monkeypatch.setattr(app, 'check_pool_staleness', lambda **kw: alerts.append(kw))
    app.poll_f2pool()
    assert polled == [('onr', 'tok')] and alerts == []

    def broken(conn, address, token):
        raise RuntimeError('f2pool timeout')
    monkeypatch.setattr(app, 'collect_f2pool_data', broken)
    app.poll_f2pool()
    assert len(alerts) == 1 and alerts[0]['force_failure'] and alerts[0]['pool_type'] == 'f2pool'
    assert alerts[0]['error_message'] == 'f2pool timeout'


def test_f2pool_job_every_5_min():
    assert app.F2POOL_EVERY_S == 300
