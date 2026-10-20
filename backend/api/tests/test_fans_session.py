"""Lüfter (AC Infinity): test session of the test environment (max. 5 min, never logs in by itself)."""
import time

import pytest
from api import acinfinity as ac
from django.contrib.auth.models import User
from rest_framework.test import APIClient


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='fanuser', password='testpass123'))
    return c


@pytest.fixture
def acdir(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, 'CRED_FILE', str(tmp_path / 'credentials'))
    monkeypatch.setattr(ac, 'TOKEN_FILE', str(tmp_path / 'token'))
    monkeypatch.setattr(ac, 'SESSION_FILE', str(tmp_path / 'test_session'))
    monkeypatch.setattr(ac, 'PAUSE_FILE', str(tmp_path / 'relogin_after'))
    monkeypatch.setattr(ac, 'DATA_DIR', str(tmp_path))
    (tmp_path / 'credentials').write_text('AC_EMAIL=test@example.com\nAC_PASSWORD=geheim\n')
    return tmp_path


@pytest.fixture
def server(monkeypatch):
    """Fake AC Infinity server: counts logins; tokens in `dead` were logged out by another login."""
    state = {'logins': 0, 'dead': set()}

    def post(path, params=None, token=None, headers=None, query=None):
        if path == '/user/appUserLogin':
            state['logins'] += 1
            return {'code': ac.CODE_OK, 'data': {'appId': 'tok%d' % state['logins']}, '_http': 200}
        if path == '/user/devInfoListAll':
            code = ac.CODE_SESSION_EXPIRED if token in state['dead'] else ac.CODE_OK
            return {'code': code, 'data': [], '_http': 200}
        return {'code': ac.CODE_OK, 'data': {}, '_http': 200}

    monkeypatch.setattr(ac, '_post', post)
    return state


@pytest.fixture
def test_env(monkeypatch):
    monkeypatch.setattr(ac, 'IS_TEST', True)
    monkeypatch.setattr(ac, 'ENV_NAME', 'Test')


def test_test_env_does_not_log_in_by_itself(client, acdir, server, test_env):
    res = client.get('/api/fans/status/')
    assert res.status_code == 200
    assert res.data['devices'] == [] and res.data['session']['active'] is False
    assert server['logins'] == 0


def test_activate_and_end(client, acdir, server, test_env):
    res = client.post('/api/fans/session/', {'action': 'activate'}, format='json')
    assert res.status_code == 200 and res.data['active'] is True and res.data['until']
    assert server['logins'] == 1
    assert client.get('/api/fans/status/').status_code == 200
    res = client.post('/api/fans/session/', {'action': 'end'}, format='json')
    assert res.data['active'] is False and res.data['ended'] == 'manual'
    assert not (acdir / 'token').exists()


def test_session_expires_after_5_minutes(client, acdir, server, test_env, monkeypatch):
    client.post('/api/fans/session/', {'action': 'activate'}, format='json')
    start = time.time()
    monkeypatch.setattr(ac.time, 'time', lambda: start + ac.TEST_SESSION_S + 1)
    info = client.get('/api/fans/session/').data
    assert info['active'] is False and info['ended'] == 'expired'
    assert not (acdir / 'token').exists()
    client.get('/api/fans/status/')
    assert server['logins'] == 1          # no new login after expiry


def test_live_took_over(client, acdir, server, test_env):
    client.post('/api/fans/session/', {'action': 'activate'}, format='json')
    server['dead'].add('tok1')             # Live logged in meanwhile
    res = client.get('/api/fans/status/')
    assert res.status_code == 200 and res.data['devices'] == []
    assert res.data['session']['active'] is False and res.data['session']['ended'] == 'live'
    assert server['logins'] == 1          # the test environment does not take the session back


def test_live_takes_session_back_after_5_minutes(client, acdir, server, monkeypatch):
    (acdir / 'token').write_text('old')
    server['dead'].add('old')              # the test environment logged in meanwhile
    with pytest.raises(ac.SessionTaken):
        ac.devices()
    with pytest.raises(ac.SessionTaken):
        ac.devices()                       # still within the pause: no login
    assert server['logins'] == 0
    assert client.get('/api/fans/session/').data['paused_until']
    start = time.time()
    monkeypatch.setattr(ac.time, 'time', lambda: start + ac.RELOGIN_PAUSE_S + 1)
    ac.devices()
    assert server['logins'] == 1          # Live logs in again after the pause
    res = client.post('/api/fans/session/', {'action': 'activate'}, format='json')
    assert res.status_code == 502 and 'Test-Umgebung' in res.data['error']


def test_live_takeover_at_once(client, acdir, server):
    (acdir / 'token').write_text('old')
    server['dead'].add('old')
    with pytest.raises(ac.SessionTaken):
        ac.devices()
    res = client.post('/api/fans/session/', {'action': 'takeover'}, format='json')
    assert res.status_code == 200 and res.data['paused_until'] is None
    assert server['logins'] == 1
