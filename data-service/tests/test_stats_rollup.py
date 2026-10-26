"""Stundenwerte (Nutzer 26.10.): rollup starts at the last stored hour (or the oldest raw value), prune deletes only
raw values that are in the hourly tables and older than 30 days."""
from datetime import datetime, timedelta, timezone

import stats_rollup

NOW = datetime(2026, 10, 26, 14, 20, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.sql.append((sql, params))
        self._one = None
        if sql.startswith('SELECT max(hour) FROM'):
            self._one = (self.db.max_hour.get(sql.split()[-1]),)
        elif sql.startswith('SELECT min(hour) FROM'):
            self._one = (self.db.min_hour.get(sql.split()[-1]),)
        elif sql.startswith('SELECT min(recorded_at) FROM'):
            self._one = (self.db.oldest.get(sql.split()[-1]),)
        elif sql.startswith('DELETE'):
            self.rowcount = 0

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, max_hour=None, min_hour=None, oldest=None):
        self.sql = []
        self.max_hour, self.min_hour, self.oldest = max_hour or {}, min_hour or {}, oldest or {}

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_first_rollup_starts_at_the_oldest_raw_value():
    conn = FakeConn(oldest={'device_mining_stats': datetime(2026, 9, 9, 19, 23, tzinfo=timezone.utc),
                            'device_hardware_stats': datetime(2026, 9, 9, 19, 24, tzinfo=timezone.utc),
                            'pool_stats': datetime(2026, 9, 9, 21, 55, tzinfo=timezone.utc)})
    done = stats_rollup.rollup(conn, until=NOW)
    assert done == {'device_stats_hourly': datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc),
                    'pool_stats_hourly': datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc)}


def test_later_rollups_start_at_the_last_stored_hour():
    last = datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)
    conn = FakeConn(max_hour={'device_stats_hourly': last, 'pool_stats_hourly': last})
    done = stats_rollup.rollup(conn, until=NOW)
    back = last - timedelta(hours=1)                              # one hour back: rows that arrive late
    assert done == {'device_stats_hourly': back, 'pool_stats_hourly': back}
    inserts = [p for s, p in conn.sql if s.lstrip().startswith('INSERT')]
    assert all(p['since'] == back and p['until'] == NOW for p in inserts) and len(inserts) == 2


def test_prune_deletes_only_rolled_up_raw_values_older_than_30_days(monkeypatch):
    first = datetime(2026, 9, 9, 19, 0, tzinfo=timezone.utc)
    conn = FakeConn(max_hour={'device_stats_hourly': NOW, 'pool_stats_hourly': NOW},
                    min_hour={'device_stats_hourly': first, 'pool_stats_hourly': None})
    monkeypatch.setattr(stats_rollup.psycopg2, 'connect', lambda url: conn)
    stats_rollup.prune_raw('db', now=NOW)
    deletes = [(s.split()[2], p) for s, p in conn.sql if s.startswith('DELETE')]
    cutoff = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
    assert deletes == [('device_mining_stats', (first, cutoff)), ('device_hardware_stats', (first, cutoff))]
