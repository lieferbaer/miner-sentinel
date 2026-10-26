"""Stundenwerte (Nutzer 26.10.): device_stats_hourly / pool_stats_hourly aus den Rohwerten (alle 10 s).

The Analytics page reads them for periods over 24 h; the raw values are kept 30 days, the hourly values for good.
  * rollup: every 5 min, from the last stored hour on (on the first run: all raw values = the one-time back-fill),
  * prune_raw: every night, raw values older than 30 days – only hours that are in the hourly tables are deleted.
"""
import logging
from datetime import datetime, timedelta, timezone

import psycopg2

logger = logging.getLogger(__name__)

RAW_KEEP_DAYS = 30
BATCH = 5000

DEVICE_SQL = """
INSERT INTO device_stats_hourly (device_id, hour, mining_samples, first_at, last_at, hashrate_avg, hashrate_max,
    hashrate_min, best_difficulty, shares_accepted_min, shares_accepted_max, shares_rejected_min, shares_rejected_max,
    hardware_samples, temperature_avg, temperature_max, temperature_n, chip_temperature_avg, chip_temperature_max,
    chip_temperature_n, power_avg, power_max, power_n, efficiency_avg, efficiency_n)
SELECT COALESCE(m.device_id, h.device_id), COALESCE(m.hour, h.hour), COALESCE(m.n, 0), m.first_at, m.last_at,
    m.hr_avg, m.hr_max, m.hr_min, m.best, m.acc_min, m.acc_max, m.rej_min, m.rej_max,
    COALESCE(h.n, 0), h.t_avg, h.t_max, COALESCE(h.t_n, 0), h.c_avg, h.c_max, COALESCE(h.c_n, 0), h.p_avg, h.p_max,
    COALESCE(h.p_n, 0), h.e_avg, COALESCE(h.e_n, 0)
FROM (
    SELECT device_id, date_trunc('hour', recorded_at) AS hour, count(*) AS n, min(recorded_at) AS first_at,
        max(recorded_at) AS last_at, avg(hashrate_ghs) AS hr_avg, max(hashrate_ghs) AS hr_max,
        min(hashrate_ghs) AS hr_min, max(best_difficulty) AS best, min(shares_accepted) AS acc_min,
        max(shares_accepted) AS acc_max, min(shares_rejected) AS rej_min, max(shares_rejected) AS rej_max
    FROM device_mining_stats WHERE recorded_at >= %(since)s AND recorded_at < %(until)s
    GROUP BY 1, 2
) m
FULL OUTER JOIN (
    SELECT device_id, date_trunc('hour', recorded_at) AS hour, count(*) AS n, avg(temperature_c) AS t_avg,
        max(temperature_c) AS t_max, count(temperature_c) AS t_n, avg(temperature_chip_c) AS c_avg,
        max(temperature_chip_c) AS c_max, count(temperature_chip_c) AS c_n, avg(power_watts) AS p_avg,
        max(power_watts) AS p_max, count(power_watts) AS p_n, avg(efficiency_j_per_th) AS e_avg,
        count(efficiency_j_per_th) AS e_n
    FROM device_hardware_stats WHERE recorded_at >= %(since)s AND recorded_at < %(until)s
    GROUP BY 1, 2
) h ON h.device_id = m.device_id AND h.hour = m.hour
ON CONFLICT (device_id, hour) DO UPDATE SET
    mining_samples = EXCLUDED.mining_samples, first_at = EXCLUDED.first_at, last_at = EXCLUDED.last_at,
    hashrate_avg = EXCLUDED.hashrate_avg, hashrate_max = EXCLUDED.hashrate_max, hashrate_min = EXCLUDED.hashrate_min,
    best_difficulty = EXCLUDED.best_difficulty, shares_accepted_min = EXCLUDED.shares_accepted_min,
    shares_accepted_max = EXCLUDED.shares_accepted_max, shares_rejected_min = EXCLUDED.shares_rejected_min,
    shares_rejected_max = EXCLUDED.shares_rejected_max, hardware_samples = EXCLUDED.hardware_samples,
    temperature_avg = EXCLUDED.temperature_avg, temperature_max = EXCLUDED.temperature_max,
    temperature_n = EXCLUDED.temperature_n, chip_temperature_avg = EXCLUDED.chip_temperature_avg,
    chip_temperature_max = EXCLUDED.chip_temperature_max, chip_temperature_n = EXCLUDED.chip_temperature_n,
    power_avg = EXCLUDED.power_avg, power_max = EXCLUDED.power_max, power_n = EXCLUDED.power_n,
    efficiency_avg = EXCLUDED.efficiency_avg, efficiency_n = EXCLUDED.efficiency_n
"""

POOL_SQL = """
INSERT INTO pool_stats_hourly (pool_type, pool_address, hour, samples, hashrate_1m_avg, hashrate_1h_avg,
    hashrate_1d_avg, workers_max)
SELECT pool_type, pool_address, date_trunc('hour', recorded_at), count(*), avg(hashrate_1m_ghs),
    avg(hashrate_1h_ghs), avg(hashrate_1d_ghs), max(workers)
FROM pool_stats WHERE recorded_at >= %(since)s AND recorded_at < %(until)s
GROUP BY 1, 2, 3
ON CONFLICT (pool_type, pool_address, hour) DO UPDATE SET
    samples = EXCLUDED.samples, hashrate_1m_avg = EXCLUDED.hashrate_1m_avg, hashrate_1h_avg = EXCLUDED.hashrate_1h_avg,
    hashrate_1d_avg = EXCLUDED.hashrate_1d_avg, workers_max = EXCLUDED.workers_max
"""

# (hourly table, its raw tables, the rollup statement)
TABLES = (
    ('device_stats_hourly', ('device_mining_stats', 'device_hardware_stats'), DEVICE_SQL),
    ('pool_stats_hourly', ('pool_stats',), POOL_SQL),
)


def _hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def rollup(conn, until=None):
    """Hourly values from one hour before the last stored hour on (late rows, incomplete hour) up to `until` (now, the
    current hour included). Without stored hours: from the oldest raw value on (back-fill). Returns {table: since}."""
    until = until or datetime.now(timezone.utc) + timedelta(hours=1)
    done = {}
    with conn.cursor() as cur:
        for hourly, raws, sql in TABLES:
            cur.execute('SELECT max(hour) FROM %s' % hourly)
            last = cur.fetchone()[0]
            if last is None:
                firsts = []
                for raw in raws:
                    cur.execute('SELECT min(recorded_at) FROM %s' % raw)
                    firsts.append(cur.fetchone()[0])
                firsts = [f for f in firsts if f is not None]
                if not firsts:
                    continue
                since = _hour(min(firsts))
            else:
                since = last - timedelta(hours=1)        # a row can arrive late for the hour before (Review 26.10.)
            cur.execute(sql, {'since': since, 'until': until})
            done[hourly] = since
    conn.commit()
    return done


def run_rollup(database_url):
    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.Error as exc:
        logger.warning('Stundenwerte: keine Datenbank: %s', exc)
        return {}
    try:
        done = rollup(conn)
        logger.info('Stundenwerte gerechnet ab %s', {k: v.isoformat() for k, v in done.items()})
        return done
    except psycopg2.Error as exc:
        conn.rollback()
        logger.error('Stundenwerte: Fehler: %s', exc)
        return {}
    finally:
        conn.close()


def prune_raw(database_url, keep_days=RAW_KEEP_DAYS, now=None):
    """Raw values older than `keep_days` – first the hourly values up to that point are brought up to date, and only
    raw values whose hour is in the hourly table are deleted (in batches, the collectors keep writing)."""
    cutoff = _hour((now or datetime.now(timezone.utc)) - timedelta(days=keep_days))
    deleted = {}
    try:
        conn = psycopg2.connect(database_url)
    except psycopg2.Error as exc:
        logger.warning('Rohwerte aufräumen: keine Datenbank: %s', exc)
        return deleted
    try:
        rollup(conn)
        with conn.cursor() as cur:
            for hourly, raws, _sql in TABLES:
                cur.execute('SELECT min(hour) FROM %s' % hourly)
                first = cur.fetchone()[0]
                if first is None or first >= cutoff:
                    continue                                 # nothing rolled up before the cutoff: delete nothing
                for raw in raws:
                    total = 0
                    while True:
                        cur.execute('DELETE FROM %s WHERE id IN (SELECT id FROM %s WHERE recorded_at >= %%s '
                                    'AND recorded_at < %%s LIMIT %d)' % (raw, raw, BATCH), (first, cutoff))
                        n = cur.rowcount
                        conn.commit()
                        total += n
                        if n < BATCH:
                            break
                    deleted[raw] = total
        if any(deleted.values()):
            logger.info('Rohwerte älter als %d Tage gelöscht (Stundenwerte bleiben): %s', keep_days, deleted)
        return deleted
    except psycopg2.Error as exc:
        conn.rollback()
        logger.error('Rohwerte aufräumen: Fehler: %s', exc)
        return deleted
    finally:
        conn.close()
