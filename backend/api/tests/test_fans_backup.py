"""Lüfter (AC Infinity): delete a backup of the port settings; the last backup of a controller stays (Nutzer 08.11.)."""
import json
import threading
import time

import pytest
from api import acinfinity as ac
from django.contrib.auth.models import User
from rest_framework.test import APIClient

URL = '/api/fans/backup/delete/'


@pytest.fixture
def client(db):
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user(username='backupuser', password='testpass123'))
    return c


@pytest.fixture
def acdir(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(ac, 'CRED_FILE', str(data / 'credentials'))
    monkeypatch.setattr(ac, 'TOKEN_FILE', str(data / 'token'))
    monkeypatch.setattr(ac, 'SESSION_FILE', str(data / 'test_session'))
    monkeypatch.setattr(ac, 'PAUSE_FILE', str(data / 'relogin_after'))
    monkeypatch.setattr(ac, 'DATA_DIR', str(data))
    (data / 'credentials').write_text('AC_EMAIL=test@example.com\nAC_PASSWORD=geheim\n')
    (tmp_path / 'credentials').write_text('outside\n')            # one level above the data directory
    return data


def _backup(data, stamp, dev_id):
    name = 'backup-%s-%s.json' % (stamp, dev_id)
    (data / name).write_text(json.dumps({'dev_id': dev_id, 'created_at': '2026-11-08T12:00:00+00:00',
                                         'ports': {'0': {'ok': True}, '1': {'ok': True}}}))
    return name


def test_delete_backup(client, acdir):
    old = _backup(acdir, '20261101-100000', 'dev1')
    new = _backup(acdir, '20261108-100000', 'dev1')
    other = _backup(acdir, '20261105-100000', 'dev2')
    res = client.post(URL, {'file': old}, format='json')
    assert res.status_code == 200 and res.data['deleted'] == old
    assert not (acdir / old).exists() and (acdir / new).exists() and (acdir / other).exists()
    assert [b['file'] for b in res.data['backups']] == [new, other]
    assert ac.has_backup('dev1')


def test_delete_unknown_backup(client, acdir):
    keep = _backup(acdir, '20261101-100000', 'dev1')
    _backup(acdir, '20261108-100000', 'dev1')
    res = client.post(URL, {'file': 'backup-20260101-000000-dev1.json'}, format='json')
    assert res.status_code == 404
    assert res.data == {'error': 'Sicherung backup-20260101-000000-dev1.json nicht gefunden.'}
    assert client.post(URL, {}, format='json').status_code == 404
    assert (acdir / keep).exists()


@pytest.mark.parametrize('name', ['../credentials', 'credentials', 'token', '../data/credentials',
                                  'backup-20261101-100000-dev1.json/../credentials'])
def test_only_backup_files_can_be_deleted(client, acdir, name):
    _backup(acdir, '20261101-100000', 'dev1')
    _backup(acdir, '20261108-100000', 'dev1')
    (acdir / 'token').write_text('tok')
    res = client.post(URL, {'file': name}, format='json')
    assert res.status_code == 404 and 'nicht gefunden' in res.data['error']
    assert (acdir / 'credentials').read_text() == 'AC_EMAIL=test@example.com\nAC_PASSWORD=geheim\n'
    assert (acdir.parent / 'credentials').read_text() == 'outside\n'
    assert (acdir / 'token').read_text() == 'tok'
    assert len(ac.list_backups()) == 2


def test_last_backup_of_a_controller_stays(client, acdir):
    first = _backup(acdir, '20261101-100000', 'dev1')
    second = _backup(acdir, '20261108-100000', 'dev1')
    only = _backup(acdir, '20261105-100000', 'dev2')
    res = client.post(URL, {'file': only}, format='json')       # dev2 has only this one (dev1 has two)
    assert res.status_code == 409 and res.data == {'error': 'Die letzte Sicherung dieses Controllers bleibt erhalten.'}
    assert (acdir / only).exists()
    assert client.post(URL, {'file': first}, format='json').status_code == 200
    res = client.post(URL, {'file': second}, format='json')
    assert res.status_code == 409
    assert (acdir / second).exists() and ac.has_backup('dev1') and ac.has_backup('dev2')


def test_delete_needs_login(db, acdir):
    first = _backup(acdir, '20261101-100000', 'dev1')
    _backup(acdir, '20261108-100000', 'dev1')
    res = APIClient().post(URL, {'file': first}, format='json')
    assert res.status_code in (401, 403)
    assert (acdir / first).exists()


def test_parallel_deletes_keep_the_last_backup(acdir, monkeypatch):
    first = _backup(acdir, '20261101-100000', 'dev1')
    second = _backup(acdir, '20261108-100000', 'dev1')
    real = ac.list_backups

    def slow_list():                       # widen the window between the check and the remove
        out = real()
        time.sleep(0.2)
        return out

    monkeypatch.setattr(ac, 'list_backups', slow_list)
    results = {}

    def run(name):
        try:
            ac.delete_backup(name)
            results[name] = 'ok'
        except ac.LastBackup:
            results[name] = 'last'

    threads = [threading.Thread(target=run, args=(n,)) for n in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ['last', 'ok']
    assert ac.has_backup('dev1') and len(ac._backup_files()) == 1
