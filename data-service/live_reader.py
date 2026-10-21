"""Central reading of the Avalon miners (cgminer API), every READ_S seconds.

One reading per miner: estats + summary (and for the A15 its web interface settings every WEB_EVERY_S).
They go into miner_cache; right after each reading
  * the Temperatur-Automatik runs (every reading),
  * the throttle controller and the Takt-Tabellen run every CONTROL_EVERY readings (15 s), in their own
    worker so a slow mode switch never delays the next reading.
The collector (every 10 s) stores these readings in the database instead of asking the miners again.
Nobody else reads the miners, except as fallback when the central reading is older than 15 s.
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg2

import calibration
import miner_cache
from fan_automation import run_fan_automation
from throttle_controller import family_of

logger = logging.getLogger(__name__)

READ_S = 5
CONTROL_EVERY = 3            # throttle controller + Takt-Tabellen every 3rd reading = 15 s
WEB_EVERY_S = 30             # A15 power level: changes rarely (own mode switches clear the cache at once)

_cycle = [0]
_control_busy = threading.Lock()
_control_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='control')


def _devices(database_url):
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, ip_address, port, name, model, device_id FROM devices "
                        "WHERE is_active AND protocol = 'cgminer_tcp'")
            cols = ('id', 'ip_address', 'port', 'device_name', 'model', 'device_id')
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def read_device(dev):
    """estats + summary (always), A15 web settings (every WEB_EVERY_S) into miner_cache."""
    ip, port = dev['ip_address'], dev['port']
    try:
        for command, timeout in (('estats', 8), ('summary', 6)):
            miner_cache.fetch(ip, command, None,
                              lambda c=command, t=timeout: calibration._cmd(ip, port, json.dumps({'command': c}), t))
        if family_of(dev) == 'a15':
            miner_cache.fetch(ip, 'web', WEB_EVERY_S, lambda: calibration.a15_conf(ip))
        return True
    except (OSError, ValueError) as exc:
        logger.debug('Central reading %s failed: %s', ip, exc)
        return False


def _control(database_url, run_throttle_controller, run_calibration):
    try:
        run_throttle_controller(database_url)
        run_calibration(database_url)
    finally:
        _control_busy.release()


def run_live_cycle(database_url, run_throttle_controller, run_calibration):
    try:
        devices = _devices(database_url)
    except psycopg2.Error as exc:
        logger.warning('Central reading: no database: %s', exc)
        return
    if devices:
        # every miner on its own, so a miner that is switched off does not delay the others
        with ThreadPoolExecutor(max_workers=len(devices), thread_name_prefix='read') as pool:
            list(pool.map(read_device, devices))
    run_fan_automation(database_url)
    _cycle[0] += 1
    if _cycle[0] % CONTROL_EVERY == 0 and _control_busy.acquire(blocking=False):
        _control_worker.submit(_control, database_url, run_throttle_controller, run_calibration)
