"""Stundenwerte (Nutzer 26.10.): rollup starts at the last stored hour (or the oldest raw value), prune deletes only
raw values that are in the hourly tables and older than 30 days. v2.2.9 (Nutzer 08.11.): balance_last /
hashrate_1d_last per hour and their one-time back-fill."""
import re
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
        elif 'SELECT EXISTS' in sql:
            self._one = (self.db.backfill_needed,)
        elif sql.startswith('DELETE'):
            self.rowcount = 0

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, max_hour=None, min_hour=None, oldest=None, backfill_needed=False):
        self.sql = []
        self.max_hour, self.min_hour, self.oldest = max_hour or {}, min_hour or {}, oldest or {}
        self.backfill_needed = backfill_needed
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

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


def test_pool_rollup_keeps_the_last_balance_and_24h_hashrate_of_the_hour():
    sql = ' '.join(stats_rollup.POOL_SQL.split())
    assert 'balance_last, hashrate_1d_last)' in sql
    assert ('(array_agg(balance ORDER BY recorded_at DESC) FILTER (WHERE balance IS NOT NULL))[1]' in sql)
    assert ('(array_agg(hashrate_1d_ghs ORDER BY recorded_at DESC) FILTER (WHERE hashrate_1d_ghs IS NOT NULL))[1]'
            in sql)
    assert "details->'balance'->>'balance'" in sql
    assert ('balance_last = EXCLUDED.balance_last, hashrate_1d_last = EXCLUDED.hashrate_1d_last' in sql)
    assert 'pool_url' not in sql and 'pool_user' not in sql


def test_balance_cast_takes_strings_and_numbers_and_nulls_everything_else():
    """A bad balance must never fail the whole rollup: only plain decimal numbers (string or JSON number, both arrive
    as text via ->>) are cast, within the range of a double."""
    sql = stats_rollup._balance_sql('details')
    pattern = re.search(r"~ '([^']+)'", sql).group(1)        # \t\n\r\f\v mean the same in Python re and Postgres
    assert '[[:space:]]' not in pattern and r'\s' not in pattern
    ok = ['0.00123456', '0', ' 12.5 ', '-0.1', '1.2e-05', '0.000012345678901234567', '3.', '42', '\t1\n']
    bad = ['', 'abc', '{"balance": 1}', '[1]', 'true', '1e999', '1e-400', 'NaN', 'Infinity', '0x10', '1,5',
           '1' * 21, '.5', '1.2.3',
           '　0.001', '0.001　', ' 0.001', ' 0.001']      # Unicode spaces: float8in skips only ASCII
    assert all(re.match(pattern, v) for v in ok)
    assert not any(re.match(pattern, v) for v in bad)
    assert '::double precision' in sql and sql.startswith('CASE WHEN')


def test_backfill_starts_at_the_first_full_hour_of_the_raw_pool_values():
    conn = FakeConn(oldest={'pool_stats': datetime(2026, 10, 9, 21, 55, tzinfo=timezone.utc)}, backfill_needed=True)
    since = stats_rollup.backfill_pool(conn, until=NOW)
    assert since == datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc)             # 21:xx may be cut by the prune
    checks = [p for s, p in conn.sql if 'SELECT EXISTS' in s]
    assert checks == [{'since': since}]
    inserts = [(s, p) for s, p in conn.sql if s.lstrip().startswith('INSERT')]
    assert inserts == [(stats_rollup.POOL_SQL, {'since': since, 'until': NOW})] and conn.commits == 1


def test_backfill_only_while_hours_still_lack_the_balance():
    conn = FakeConn(oldest={'pool_stats': datetime(2026, 10, 9, 21, 55, tzinfo=timezone.utc)}, backfill_needed=False)
    assert stats_rollup.backfill_pool(conn, until=NOW) is None
    assert not [s for s, p in conn.sql if s.lstrip().startswith('INSERT')]
    empty = FakeConn()                                                             # no raw pool values at all
    assert stats_rollup.backfill_pool(empty, until=NOW) is None
    assert not [s for s, p in empty.sql if 'SELECT EXISTS' in s or s.lstrip().startswith('INSERT')]


def test_backfill_check_looks_for_hours_without_balance_whose_raw_values_have_one():
    sql = ' '.join(stats_rollup.POOL_BACKFILL_NEEDED_SQL.split())
    assert 'h.hour >= %(since)s AND h.balance_last IS NULL' in sql
    assert "r.recorded_at >= h.hour AND r.recorded_at < h.hour + interval '1 hour'" in sql
    assert "r.details->'balance'->>'balance'" in sql and 'pool_url' not in sql


def test_run_pool_backfill_survives_a_database_error(monkeypatch):
    class Broken(FakeConn):
        def cursor(self):
            raise stats_rollup.psycopg2.Error('boom')
    monkeypatch.setattr(stats_rollup.psycopg2, 'connect', lambda url: Broken())
    assert stats_rollup.run_pool_backfill('db') is None
