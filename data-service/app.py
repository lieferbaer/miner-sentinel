"""
Data Collection Service for MinerSentinel Dashboard
Polls Bitaxe and Avalon devices and CKPool/PublicPool periodically and stores data in Postgres.
Settings are loaded from the database (collector_settings table).
"""

import json
import logging

import requests
import os
import time
from datetime import datetime, timedelta

import psycopg2
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from collectors.avalon_collector import AvalonCollector
from collectors.bitaxe_collector import BitAxeCollector
from collectors.btcpowlab_collector import collect_btcpowlab_data
from collectors.ckpool_collector import collect_ckpool_data
from collectors.f2pool_collector import collect_f2pool_data
from collectors.nerdnos_collector import NerdNOSCollector
from collectors.nmaxe_collector import NMAxeCollector
from collectors.parasite_collector import collect_parasite_data
from collectors.publicpool_collector import collect_publicpool_data
import miner_cache
from throttle_controller import run_throttle_controller
from calibration import run_calibration
from live_reader import READ_S as LIVE_READ_S, run_live_cycle
import automation_journal
import stats_rollup
from decouple import config
from flask import Flask, jsonify, request
from notifications.emitter import emit_alert, resolve_open_alerts
from notifications.health_checks import check_pool_staleness
from notifications.push_notifier import push_notifier_from_settings
from psycopg2.extras import RealDictCursor

from control.capabilities import capabilities_for_make
from control.dispatcher import execute_control
from control.discovery import discover_lan

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Database Configuration
POSTGRES_DB = config('POSTGRES_DB', default='minersentinel')
POSTGRES_USER = config('POSTGRES_USER', default='minersentinel')
POSTGRES_PASSWORD = config('POSTGRES_PASSWORD', default='changeme')
POSTGRES_HOST = config('POSTGRES_HOST', default='postgres')
POSTGRES_PORT = config('POSTGRES_PORT', default='5432')

# Build DATABASE_URL from components
DATABASE_URL = f'postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}'

# Global settings cache (loaded from database)
collector_settings = {
    'polling_interval_minutes': 2,
    'device_check_interval_minutes': 5,
    'pool_type': 'ckpool',
    'ckpool_address': '',
    'ckpool_url': 'https://eusolo.ckpool.org',
    'publicpool_address': '',
    'publicpool_url': 'http://localhost:3334',
    'btcpowlab_address': '',
    'btcpowlab_url': 'https://btcpowlab-pool.com/public/v1',
    'parasite_address': '',
    'parasite_url': 'https://parasite.space/api',
    'telegram_enabled': False,
    'telegram_bot_token': '',
    'telegram_chat_id': '',
    'discord_enabled': False,
    'discord_webhook_url': '',
    'ntfy_enabled': False,
    'ntfy_url': '',
    'ntfy_token': '',
    'gotify_enabled': False,
    'gotify_url': '',
    'gotify_token': '',
    'webhook_enabled': False,
    'webhook_url': '',
    'quiet_hours_enabled': False,
    'quiet_hours_start': '22:00',
    'quiet_hours_end': '07:00',
    'alert_repeat_minutes': 60,
    'metrics_retention_days': 90,
    'alert_retention_days': 0,
}

# Runtime collector health (honest status for Settings / Overview)
collection_health = {
    'last_success_at': None,
    'last_error': None,
    'last_error_at': None,
    'last_cycle_at': None,
    'last_cycle_ok': None,
}

# Initialize collectors (will load devices from database)
bitaxe_collector = BitAxeCollector(DATABASE_URL)
avalon_collector = AvalonCollector(DATABASE_URL)
nmaxe_collector = NMAxeCollector(DATABASE_URL)
nerdnos_collector = NerdNOSCollector(DATABASE_URL)

# Scheduler
scheduler = BackgroundScheduler()


def _alert_preferences_from_settings(settings: dict) -> dict:
    return {
        'quiet_hours_enabled': bool(settings.get('quiet_hours_enabled', False)),
        'quiet_hours_start': settings.get('quiet_hours_start') or '22:00',
        'quiet_hours_end': settings.get('quiet_hours_end') or '07:00',
        'alert_repeat_minutes': int(settings.get('alert_repeat_minutes') or 60),
    }


def _apply_preferences_to_collectors(prefs: dict) -> None:
    bitaxe_collector.update_alert_preferences(prefs)
    avalon_collector.update_alert_preferences(prefs)
    nmaxe_collector.update_alert_preferences(prefs)
    nerdnos_collector.update_alert_preferences(prefs)


def load_settings_from_database():
    """Load collector settings from the database."""
    global collector_settings
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT polling_interval_minutes, device_check_interval_minutes,
                   pool_type, ckpool_address, ckpool_url,
                   publicpool_address, publicpool_url,
                   btcpowlab_address, btcpowlab_url,
                   parasite_address, parasite_url,
                   telegram_enabled, telegram_bot_token, telegram_chat_id,
                   discord_enabled, discord_webhook_url,
                   ntfy_enabled, ntfy_url, ntfy_token,
                   gotify_enabled, gotify_url, gotify_token,
                   webhook_enabled, webhook_url,
                   notification_rules,
                   quiet_hours_enabled, quiet_hours_start, quiet_hours_end,
                   alert_repeat_minutes,
                   metrics_retention_days, alert_retention_days
            FROM collector_settings
            WHERE id = 1
        """)
        row = cursor.fetchone()

        if row:
            collector_settings = dict(row)
            logger.info(f"Loaded settings from database: polling={collector_settings['polling_interval_minutes']}min, "
                       f"device_check={collector_settings['device_check_interval_minutes']}min, "
                       f"pool_type={collector_settings.get('pool_type', 'ckpool')}")

            # Update telegram settings in collectors
            telegram_enabled = collector_settings.get('telegram_enabled', False)
            telegram_bot_token = collector_settings.get('telegram_bot_token', '')
            telegram_chat_id = collector_settings.get('telegram_chat_id', '')

            bitaxe_collector.update_telegram_settings(telegram_enabled, telegram_bot_token, telegram_chat_id)
            avalon_collector.update_telegram_settings(telegram_enabled, telegram_bot_token, telegram_chat_id)
            nmaxe_collector.update_telegram_settings(telegram_enabled, telegram_bot_token, telegram_chat_id)
            nerdnos_collector.update_telegram_settings(telegram_enabled, telegram_bot_token, telegram_chat_id)

            logger.info(f"Telegram notifications: {'enabled' if telegram_enabled else 'disabled'}")

            # Update Discord settings in collectors
            discord_enabled = collector_settings.get('discord_enabled', False)
            discord_webhook_url = collector_settings.get('discord_webhook_url', '')

            bitaxe_collector.update_discord_settings(discord_enabled, discord_webhook_url)
            avalon_collector.update_discord_settings(discord_enabled, discord_webhook_url)
            nmaxe_collector.update_discord_settings(discord_enabled, discord_webhook_url)
            nerdnos_collector.update_discord_settings(discord_enabled, discord_webhook_url)

            logger.info(f"Discord notifications: {'enabled' if discord_enabled else 'disabled'}")

            # ntfy / Gotify / generic webhook
            bitaxe_collector.update_push_settings(collector_settings)
            avalon_collector.update_push_settings(collector_settings)
            nmaxe_collector.update_push_settings(collector_settings)
            nerdnos_collector.update_push_settings(collector_settings)
            push = push_notifier_from_settings(collector_settings)
            logger.info(f"Push notifications: {'enabled' if push.enabled else 'disabled'}")

            # Per-event alert rules (toggles + thresholds)
            rules = collector_settings.get('notification_rules') or {}
            bitaxe_collector.update_notification_rules(rules)
            avalon_collector.update_notification_rules(rules)
            nmaxe_collector.update_notification_rules(rules)
            nerdnos_collector.update_notification_rules(rules)

            prefs = _alert_preferences_from_settings(collector_settings)
            _apply_preferences_to_collectors(prefs)
        else:
            logger.warning("No settings found in database, using defaults")

        cursor.close()
        conn.close()
        return collector_settings
    except Exception as e:
        # Older DBs may not have push/retention columns yet — fall back without them
        logger.error(f"Error loading settings from database: {e}", exc_info=True)
        return collector_settings


def load_active_devices():
    """Load active devices from unified devices table and dispatch by make."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        by_make = {
            'bitaxe': [],
            'avalon': [],
            'nmaxe': [],
            'nerdnos': [],
        }

        cursor.execute("""
            SELECT device_id, name AS device_name, ip_address, make, protocol, port
            FROM devices
            WHERE is_active = TRUE
        """)
        for row in cursor.fetchall():
            entry = {
                'device_id': row['device_id'],
                'device_name': row['device_name'],
                'ip_address': row['ip_address'],
                'port': row.get('port'),
            }
            make = row['make']
            if make in by_make:
                by_make[make].append(entry)
            else:
                logger.warning(f"No collector for make={make} device={row['device_id']} — skipped")

        logger.info(
            "Loaded devices: "
            f"bitaxe={len(by_make['bitaxe'])} avalon={len(by_make['avalon'])} "
            f"nmaxe={len(by_make['nmaxe'])} nerdnos={len(by_make['nerdnos'])}"
        )

        bitaxe_collector.update_devices(by_make['bitaxe'])
        avalon_collector.update_devices(by_make['avalon'])
        nmaxe_collector.update_devices(by_make['nmaxe'])
        nerdnos_collector.update_devices(by_make['nerdnos'])

        cursor.close()
        conn.close()

        return by_make
    except Exception as e:
        logger.error(f"Error loading devices from database: {e}", exc_info=True)
        return {'bitaxe': [], 'avalon': [], 'nmaxe': [], 'nerdnos': []}



def update_network_data():
    """Refresh BTC price (in the configured currency) and network stats."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute('SELECT energy_currency FROM collector_settings LIMIT 1')
        row = cursor.fetchone()
        currency = (row[0] if row and row[0] else 'EUR').lower()

        price = None
        try:
            response = requests.get(
                'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,eur',
                timeout=15,
            )
            response.raise_for_status()
            prices = response.json().get('bitcoin', {})
            price = prices.get(currency) or prices.get('eur') or prices.get('usd')
        except Exception as exc:
            logger.warning(f"BTC price refresh failed: {exc}")

        hashrate_ehs = None
        difficulty = None
        try:
            response = requests.get('https://mempool.space/api/v1/mining/hashrate/3d', timeout=15)
            response.raise_for_status()
            payload = response.json()
            if payload.get('currentHashrate'):
                hashrate_ehs = float(payload['currentHashrate']) / 1e18
            if payload.get('currentDifficulty'):
                difficulty = float(payload['currentDifficulty'])
        except Exception as exc:
            logger.warning(f"Network stats refresh failed: {exc}")

        sets = []
        params = []
        if price:
            sets.append('cached_btc_price = %s')
            params.append(price)
        if hashrate_ehs:
            sets.append('cached_network_hashrate = %s')
            params.append(hashrate_ehs)
        if difficulty:
            sets.append('cached_network_difficulty = %s')
            params.append(difficulty)
        if sets:
            sets.append('network_data_updated_at = NOW()')
            cursor.execute('UPDATE collector_settings SET ' + ', '.join(sets), params)
            conn.commit()
            logger.info(
                f"Network data updated: BTC {price} {currency.upper()}, "
                f"network {hashrate_ehs:.0f} EH/s" if hashrate_ehs else f"BTC {price} {currency.upper()}"
            )
        cursor.close()
        conn.close()
    except Exception as exc:
        logger.error(f"update_network_data failed: {exc}")


def get_pool_targets():
    """Collect pool-statistics targets: per-device configuration plus the global fallback.

    A device carries its own target in connection_config['pool_stats'] as
    {'type': 'ckpool'|'publicpool'|'f2pool'|'btcpowlab'|'parasite', 'address': '<btc address>', 'url': '<pool url>'}.
    """
    default_urls = {
        'ckpool': collector_settings.get('ckpool_url', 'https://eusolo.ckpool.org'),
        'publicpool': collector_settings.get('publicpool_url', 'http://localhost:3334'),
        'btcpowlab': collector_settings.get('btcpowlab_url', 'https://btcpowlab-pool.com/public/v1'),
        'parasite': collector_settings.get('parasite_url', 'https://parasite.space/api'),
    }
    targets = []
    seen = set()

    def add(pool_type, address, url, token=''):
        pool_type = (pool_type or '').strip()
        address = (address or '').strip()
        if pool_type not in ('ckpool', 'publicpool', 'f2pool', 'btcpowlab', 'parasite') or not address:
            return
        url = (url or '').strip() or default_urls.get(pool_type, '')
        key = (pool_type, address, url, (token or '').strip())
        if key in seen:
            return
        seen.add(key)
        targets.append(key)

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT device_id, connection_config FROM devices WHERE is_active = true")
        for device_id, config in cursor.fetchall():
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except ValueError:
                    config = {}
            stats = (config or {}).get('pool_stats') or {}
            add(stats.get('type'), stats.get('address'), stats.get('url'), stats.get('token'))
        cursor.close()
        conn.close()
    except Exception as exc:
        logger.error(f"Could not read per-device pool settings: {exc}")

    global_type = collector_settings.get('pool_type', 'ckpool')
    add(global_type, collector_settings.get(f'{global_type}_address', ''), default_urls.get(global_type, ''))
    return targets


def poll_all_sources():
    """Poll all data sources and store in database."""
    global collection_health
    logger.info("Starting data collection cycle")
    cycle_errors = []
    cycle_started = datetime.utcnow()

    # Reload settings from database (in case they changed)
    load_settings_from_database()

    # Reload devices from database before each collection
    load_active_devices()

    try:
        logger.info("Polling Bitaxe devices...")
        bitaxe_collector.collect_all_devices()
        logger.info("Bitaxe polling completed")
    except Exception as e:
        logger.error(f"Error polling Bitaxe devices: {e}", exc_info=True)
        cycle_errors.append(f"bitaxe: {e}")

    try:
        logger.info("Polling Avalon devices...")
        avalon_collector.collect_all_devices()
        logger.info("Avalon polling completed")
    except Exception as e:
        logger.error(f"Error polling Avalon devices: {e}", exc_info=True)
        cycle_errors.append(f"avalon: {e}")

    try:
        logger.info("Polling NMAxe devices...")
        nmaxe_collector.collect_all_devices()
        logger.info("NMAxe polling completed")
    except Exception as e:
        logger.error(f"Error polling NMAxe devices: {e}", exc_info=True)
        cycle_errors.append(f"nmaxe: {e}")

    try:
        logger.info("Polling NerdNOS devices...")
        nerdnos_collector.collect_all_devices()
        logger.info("NerdNOS polling completed")
    except Exception as e:
        logger.error(f"Error polling NerdNOS devices: {e}", exc_info=True)
        cycle_errors.append(f"nerdnos: {e}")

    # Poll pool statistics - one target per device plus the global fallback
    targets = get_pool_targets()
    pool_configured = bool(targets)
    pool_failed = False
    pool_error = ''
    for pool_type, address, url, token in targets:
        if pool_type == 'f2pool':
            continue                    # own job every 5 min (poll_f2pool, Nutzer 26.10.)
        try:
            logger.info(f"Polling {pool_type} statistics for account {address[:10]}...")
            conn = psycopg2.connect(DATABASE_URL)
            if pool_type == 'publicpool':
                collect_publicpool_data(conn, address, url)
            elif pool_type == 'f2pool':
                collect_f2pool_data(conn, address, token)
            elif pool_type == 'btcpowlab':
                collect_btcpowlab_data(conn, address, url)
            elif pool_type == 'parasite':
                collect_parasite_data(conn, address, url)
            else:
                collect_ckpool_data(conn, address, url)
            conn.close()
        except Exception as e:
            logger.error(f"Error polling {pool_type} for {address[:10]}: {e}", exc_info=True)
            pool_failed = True
            pool_error = str(e)
            cycle_errors.append(f"pool {pool_type}: {e}")
    # staleness check per pool type only when all targets use the same pool
    pool_types = {t[0] for t in targets}
    pool_type = pool_types.pop() if len(pool_types) == 1 else ''

    prefs = _alert_preferences_from_settings(collector_settings)
    tg = bitaxe_collector.telegram_notifier
    dc = bitaxe_collector.discord_notifier
    push = bitaxe_collector.push_notifier
    rules = bitaxe_collector.notification_rules

    if pool_configured:
        check_pool_staleness(
            database_url=DATABASE_URL,
            telegram=tg,
            discord=dc,
            push=push,
            rules=rules,
            preferences=prefs,
            pool_type=pool_type,
            force_failure=pool_failed,
            error_message=pool_error,
        )

    cycle_ok = len(cycle_errors) == 0
    collection_health['last_cycle_at'] = cycle_started.isoformat() + 'Z'
    collection_health['last_cycle_ok'] = cycle_ok
    if cycle_ok:
        collection_health['last_success_at'] = datetime.utcnow().isoformat() + 'Z'
        collection_health['last_error'] = None
        resolve_open_alerts(DATABASE_URL, 'collector_down')
    else:
        err_msg = '; '.join(cycle_errors)[:500]
        collection_health['last_error'] = err_msg
        collection_health['last_error_at'] = datetime.utcnow().isoformat() + 'Z'
        emit_alert(
            database_url=DATABASE_URL,
            event_key='collector_down',
            method_name='send_collector_down_alert',
            telegram=tg,
            discord=dc,
            push=push,
            rules=rules,
            args=(err_msg,),
            message=f'Collector cycle failed: {err_msg}',
            payload={'errors': cycle_errors},
            preferences=prefs,
        )

    logger.info("Data collection cycle completed")


F2POOL_EVERY_S = 300


def poll_f2pool():
    """f2pool every 5 min as its own job (Nutzer 26.10.): before, it ran in every device poll (10 s, 3 requests of ~1 s);
    f2pool's own values change only every few minutes. A failure is reported like before (pool_down)."""
    targets = [t for t in get_pool_targets() if t[0] == 'f2pool']
    failed, error = False, ''
    for _pool_type, address, _url, token in targets:
        try:
            logger.info(f"Polling f2pool statistics for account {address[:10]}...")
            conn = psycopg2.connect(DATABASE_URL)
            collect_f2pool_data(conn, address, token)
            conn.close()
        except Exception as e:
            logger.error(f"Error polling f2pool for {address[:10]}: {e}", exc_info=True)
            failed, error = True, str(e)
    if targets and failed:
        check_pool_staleness(
            database_url=DATABASE_URL,
            telegram=bitaxe_collector.telegram_notifier,
            discord=bitaxe_collector.discord_notifier,
            push=bitaxe_collector.push_notifier,
            rules=bitaxe_collector.notification_rules,
            preferences=_alert_preferences_from_settings(collector_settings),
            pool_type='f2pool',
            force_failure=True,
            error_message=error,
        )


def prune_old_data():
    """Delete old metrics (and optionally alerts) per retention settings."""
    load_settings_from_database()
    metrics_days = int(collector_settings.get('metrics_retention_days') or 0)
    alert_days = int(collector_settings.get('alert_retention_days') or 0)
    deleted = {'mining': 0, 'hardware': 0, 'system': 0, 'pool': 0, 'alerts': 0}

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        if metrics_days > 0:
            cutoff = datetime.utcnow() - timedelta(days=metrics_days)
            for table, key in (
                ('device_mining_stats', 'mining'),
                ('device_hardware_stats', 'hardware'),
                ('device_system_info', 'system'),
                ('pool_stats', 'pool'),
            ):
                cursor.execute(
                    f"DELETE FROM {table} WHERE recorded_at < %s",
                    (cutoff,),
                )
                deleted[key] = cursor.rowcount
        if alert_days > 0:
            alert_cutoff = datetime.utcnow() - timedelta(days=alert_days)
            # Keep open (unresolved) alerts; prune old resolved/acked noise
            cursor.execute(
                """
                DELETE FROM alert_events
                WHERE created_at < %s
                  AND resolved_at IS NOT NULL
                """,
                (alert_cutoff,),
            )
            deleted['alerts'] = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        if any(deleted.values()):
            logger.info(f"Retention prune deleted: {deleted}")
        return deleted
    except Exception as e:
        logger.error(f"Retention prune failed: {e}", exc_info=True)
        return deleted


def reschedule_jobs():
    """Reschedule jobs based on current settings from database."""
    global collector_settings

    # Remove existing jobs
    for job in scheduler.get_jobs():
        job.remove()

    polling_interval = collector_settings.get('polling_interval_minutes', 2)
    device_check_interval = collector_settings.get('device_check_interval_minutes', 5)

    # Schedule periodic polling
    polling_seconds = 0
    try:
        polling_seconds = int(os.environ.get('POLLING_INTERVAL_SECONDS', '0') or 0)
    except (TypeError, ValueError):
        polling_seconds = 0
    polling_trigger = (
        IntervalTrigger(seconds=polling_seconds)
        if polling_seconds > 0
        else IntervalTrigger(minutes=polling_interval)
    )

    scheduler.add_job(
        func=poll_all_sources,
        trigger=polling_trigger,
        id='poll_all_sources',
        name='Poll all data sources',
        replace_existing=True
    )

    # Schedule periodic device check (reload devices more frequently)
    scheduler.add_job(
        func=load_active_devices,
        trigger=IntervalTrigger(minutes=device_check_interval),
        id='check_devices',
        name='Check for new devices',
        replace_existing=True
    )

    scheduler.add_job(
        func=update_network_data,
        trigger=IntervalTrigger(minutes=30),
        id='network_data',
        name='Refresh BTC price and network stats',
        replace_existing=True,
        next_run_time=datetime.now(),
    )

    # central reading of the Avalon miners every 5 s; right after it the Temperatur-Automatik
    # (every reading) and the throttle controller + Takt-Tabellen (every 15 s) run on these values
    scheduler.add_job(
        func=lambda: run_live_cycle(DATABASE_URL, run_throttle_controller, run_calibration),
        trigger=IntervalTrigger(seconds=LIVE_READ_S),
        id='live_cycle',
        name='Zentrale Abfrage Avalon',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Schedule settings reload (check for settings changes every 2 minutes)
    scheduler.add_job(
        func=check_and_apply_settings_changes,
        trigger=IntervalTrigger(minutes=2),
        id='check_settings',
        name='Check for settings changes',
        replace_existing=True
    )

    # Protokoll der Temperatur-Automatik: Einträge älter als 360 Tage löschen, jede Nacht um 03:30
    scheduler.add_job(
        func=lambda: automation_journal.prune(DATABASE_URL),
        trigger=CronTrigger(hour=3, minute=30),
        id='prune_automation_journal',
        name='Protokoll der Automatik aufräumen (360 Tage)',
        replace_existing=True,
    )

    scheduler.add_job(
        func=poll_f2pool,
        trigger=IntervalTrigger(seconds=F2POOL_EVERY_S),
        id='poll_f2pool',
        name='f2pool abfragen (alle 5 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )

    # Stundenwerte (Nutzer 26.10.): alle 5 min nachrechnen (der erste Lauf rechnet alle Rohwerte nach); jede Nacht
    # um 03:40 Rohwerte älter als 30 Tage löschen, die Stundenwerte bleiben
    scheduler.add_job(
        func=lambda: stats_rollup.run_rollup(DATABASE_URL),
        trigger=IntervalTrigger(minutes=5),
        id='stats_rollup',
        name='Stundenwerte rechnen',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )
    scheduler.add_job(
        func=lambda: stats_rollup.prune_raw(DATABASE_URL),
        trigger=CronTrigger(hour=3, minute=40),
        id='prune_raw_stats',
        name='Rohwerte älter als 30 Tage löschen (Stundenwerte bleiben)',
        replace_existing=True,
        max_instances=1,
    )

    # Daily retention prune (metrics / optional alert history)
    scheduler.add_job(
        func=prune_old_data,
        trigger=IntervalTrigger(hours=24),
        id='prune_retention',
        name='Prune old metrics and alerts',
        replace_existing=True
    )

    if polling_seconds > 0:
        logger.info(f"Scheduled jobs: polling every {polling_seconds}s (env override), device check every {device_check_interval}min")
    else:
        logger.info(f"Scheduled jobs: polling every {polling_interval}min, device check every {device_check_interval}min")


def check_and_apply_settings_changes():
    """Check if settings have changed and reschedule if needed."""
    global collector_settings

    old_polling = collector_settings.get('polling_interval_minutes', 15)
    old_device_check = collector_settings.get('device_check_interval_minutes', 5)

    # Reload settings
    load_settings_from_database()

    new_polling = collector_settings.get('polling_interval_minutes', 15)
    new_device_check = collector_settings.get('device_check_interval_minutes', 5)

    # If intervals changed, reschedule jobs
    if old_polling != new_polling or old_device_check != new_device_check:
        logger.info(f"Settings changed: polling {old_polling}->{new_polling}min, device_check {old_device_check}->{new_device_check}min")
        reschedule_jobs()


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': 'data-collection',
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/poll', methods=['POST'])
def trigger_poll():
    """Manually trigger a poll cycle."""
    logger.info("Manual poll triggered via API")
    poll_all_sources()
    return jsonify({
        'status': 'success',
        'message': 'Poll cycle completed',
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/status', methods=['GET'])
def status():
    """Get service status and next run time."""
    jobs = scheduler.get_jobs()
    poll_job = next((j for j in jobs if j.id == 'poll_all_sources'), None)
    next_run = poll_job.next_run_time if poll_job else None

    healthy = collection_health.get('last_cycle_ok') is not False
    status_label = 'healthy' if healthy else 'degraded'
    if collection_health.get('last_success_at') is None and collection_health.get('last_cycle_at') is None:
        status_label = 'starting'

    # Get current device counts from database (prefer unified registry)
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        bitaxe_list = []
        avalon_list = []
        nmaxe_list = []
        nerdnos_list = []
        cursor.execute("""
            SELECT name AS device_name, ip_address, make, port
            FROM devices
            WHERE is_active = TRUE
        """)
        for row in cursor.fetchall():
            entry = {
                'name': row['device_name'],
                'ip': row['ip_address'],
                'port': row.get('port'),
            }
            if row['make'] == 'bitaxe':
                bitaxe_list.append(entry)
            elif row['make'] == 'avalon':
                avalon_list.append(entry)
            elif row['make'] == 'nmaxe':
                nmaxe_list.append(entry)
            elif row['make'] == 'nerdnos':
                nerdnos_list.append(entry)

        cursor.close()
        conn.close()

        return jsonify({
            'status': status_label,
            'reachable': True,
            'polling_interval_minutes': collector_settings.get('polling_interval_minutes', 2),
            'device_check_interval_minutes': collector_settings.get('device_check_interval_minutes', 5),
            'ckpool_address': collector_settings.get('ckpool_address', ''),
            'ckpool_url': collector_settings.get('ckpool_url', 'https://eusolo.ckpool.org'),
            'next_run': next_run.isoformat() if next_run else None,
            'last_success_at': collection_health.get('last_success_at'),
            'last_error': collection_health.get('last_error'),
            'last_error_at': collection_health.get('last_error_at'),
            'last_cycle_at': collection_health.get('last_cycle_at'),
            'last_cycle_ok': collection_health.get('last_cycle_ok'),
            'bitaxe_devices_count': len(bitaxe_list),
            'avalon_devices_count': len(avalon_list),
            'nmaxe_devices_count': len(nmaxe_list),
            'nerdnos_devices_count': len(nerdnos_list),
            'bitaxe_devices': bitaxe_list,
            'avalon_devices': avalon_list,
            'nmaxe_devices': nmaxe_list,
            'nerdnos_devices': nerdnos_list,
        })
    except Exception as e:
        logger.error(f"Error getting status: {e}", exc_info=True)
        return jsonify({
            'status': status_label,
            'reachable': True,
            'polling_interval_minutes': collector_settings.get('polling_interval_minutes', 2),
            'next_run': next_run.isoformat() if next_run else None,
            'last_success_at': collection_health.get('last_success_at'),
            'last_error': collection_health.get('last_error') or str(e),
            'last_error_at': collection_health.get('last_error_at'),
            'last_cycle_at': collection_health.get('last_cycle_at'),
            'last_cycle_ok': collection_health.get('last_cycle_ok'),
            'error': 'Could not fetch device information'
        })


@app.route('/settings/reload', methods=['POST'])
def reload_settings():
    """Manually reload settings from database and reschedule jobs."""
    logger.info("Manual settings reload triggered via API")
    load_settings_from_database()
    reschedule_jobs()
    return jsonify({
        'status': 'success',
        'message': 'Settings reloaded and jobs rescheduled',
        'settings': collector_settings,
        'timestamp': datetime.utcnow().isoformat()
    })


def _lookup_device(make: str, device_id: str):
    """Fetch active device connection info from unified devices table."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, make, device_id, name, ip_address, port, is_active, error_message, last_seen_at
            FROM devices
            WHERE make = %s AND device_id = %s
            LIMIT 1
            """,
            (make, device_id),
        )
        row = cursor.fetchone()
        cursor.close()
        return dict(row) if row else None
    finally:
        conn.close()


@app.route('/control/capabilities', methods=['GET'])
def control_capabilities():
    """Return capability map for a make (or all makes)."""
    make = (request.args.get('make') or '').strip().lower()
    if make:
        return jsonify({'make': make, 'capabilities': capabilities_for_make(make)})
    from control.capabilities import CAPABILITIES_BY_MAKE
    return jsonify({'capabilities': CAPABILITIES_BY_MAKE})


@app.route('/control', methods=['POST'])
def control_device():
    """
    Execute a remote device control action.

    Body: { make, device_id, action, params? }
    LAN credentials stay in this service; backend proxies here.
    """
    body = request.get_json(silent=True) or {}
    make = (body.get('make') or '').strip().lower()
    device_id = (body.get('device_id') or '').strip()
    action = (body.get('action') or '').strip().lower()
    params = body.get('params') if isinstance(body.get('params'), dict) else {}

    if not make or not device_id or not action:
        return jsonify({'ok': False, 'error': 'make, device_id, and action are required'}), 400

    try:
        device = _lookup_device(make, device_id)
    except Exception as e:
        logger.error('Control device lookup failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': 'Device lookup failed'}), 500

    if not device:
        return jsonify({'ok': False, 'error': 'Device not found'}), 404
    if not device.get('is_active'):
        return jsonify({'ok': False, 'error': 'Device is inactive'}), 400

    try:
        # one request at a time per miner; its stored readings are cleared afterwards
        with miner_cache.command(device['ip_address']):
            result = execute_control(
                make=make,
                ip_address=device['ip_address'],
                port=device.get('port'),
                action=action,
                params=params,
            )
        result['device_id'] = device_id
        result['device_name'] = device.get('name')
        result['ok'] = True
        logger.info(
            'Control success make=%s device=%s action=%s',
            make, device_id, action,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(
            'Control failed make=%s device=%s action=%s: %s',
            make, device_id, action, e, exc_info=True,
        )
        return jsonify({'ok': False, 'error': str(e), 'action': action}), 502


@app.route('/discover', methods=['POST'])
def discover_devices():
    """
    LAN scan for AxeOS HTTP + cgminer :4028.

    Body (optional): { cidr?, seed_ips? }
    Defaults to /24 of known devices or 192.168.1.0/24.
    """
    body = request.get_json(silent=True) or {}
    cidr = (body.get('cidr') or '').strip() or None
    seed_ips = body.get('seed_ips') if isinstance(body.get('seed_ips'), list) else None

    if not seed_ips:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT ip_address FROM devices WHERE is_active = TRUE')
            seed_ips = [r[0] for r in cursor.fetchall() if r[0]]
            cursor.close()
            conn.close()
        except Exception as e:
            logger.warning('Could not load seed IPs for discovery: %s', e)
            seed_ips = None

    # Lab defaults help smoke-test when registry is empty
    if not seed_ips:
        seed_ips = ['192.168.1.7', '192.168.1.11', '192.168.1.12']

    try:
        found = discover_lan(cidr=cidr, seed_ips=seed_ips)
        return jsonify({
            'ok': True,
            'count': len(found),
            'devices': found,
            'cidr': cidr,
            'seed_ips': seed_ips,
        })
    except Exception as e:
        logger.error('Discovery failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    logger.info("Starting Data Collection Service")

    # Wait for database to be ready
    logger.info("Waiting for database connection...")
    max_retries = 30
    for i in range(max_retries):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.close()
            logger.info("Database connection established")
            break
        except psycopg2.OperationalError:
            if i < max_retries - 1:
                logger.info(f"Database not ready, retrying in 2 seconds... ({i+1}/{max_retries})")
                time.sleep(2)
            else:
                logger.error("Could not connect to database after maximum retries")
                raise

    # Load settings from database
    logger.info("Loading settings from database...")
    load_settings_from_database()
    polling_interval = collector_settings.get('polling_interval_minutes', 15)
    device_check_interval = collector_settings.get('device_check_interval_minutes', 5)
    logger.info(f"Polling interval: {polling_interval} minutes")
    logger.info(f"Device check interval: {device_check_interval} minutes")

    # Load active devices from database
    logger.info("Loading active devices from database...")
    devices_by_make = load_active_devices()
    logger.info(
        f"Found devices: bitaxe={len(devices_by_make.get('bitaxe', []))} "
        f"avalon={len(devices_by_make.get('avalon', []))} "
        f"nmaxe={len(devices_by_make.get('nmaxe', []))} "
        f"nerdnos={len(devices_by_make.get('nerdnos', []))}"
    )

    # Run initial poll
    logger.info("Running initial data collection...")
    poll_all_sources()

    # Schedule jobs using settings from database
    reschedule_jobs()

    scheduler.start()
    logger.info("Scheduler started")

    # Run Flask app
    try:
        app.run(host='0.0.0.0', port=5000, debug=False)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down scheduler...")
        scheduler.shutdown()

