"""f2pool statistics collector.

f2pool has no public endpoint: every hashrate query needs an account API token
sent as the F2P-API-SECRET header (https://api.f2pool.com/v2/doc/en.html).
The response shape is not fully documented, so field names are resolved
defensively and the raw payload is kept in details for inspection.
"""
import logging
from datetime import datetime, timezone

import requests

from collectors.normalized import NormalizedPoolSnapshot, PoolDataWriter

logger = logging.getLogger(__name__)

API_URL = 'https://api.f2pool.com/v2/hash_rate/info'
WORKER_URL = 'https://api.f2pool.com/v2/hash_rate/worker/list'
BALANCE_URL = 'https://api.f2pool.com/v2/assets/balance'


def _first_number(payload, keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, str):
            try:
                number = float(value)
            except ValueError:
                continue
            if number > 0:
                return number
    return None


def _to_ghs(hashes_per_second):
    if not hashes_per_second:
        return None
    return float(hashes_per_second) / 1e9


def _format_ghs(value_ghs):
    if not value_ghs:
        return None
    if value_ghs >= 1e6:
        return f"{value_ghs / 1e6:.2f} EH/s"
    if value_ghs >= 1e3:
        return f"{value_ghs / 1e3:.2f} TH/s"
    return f"{value_ghs:.2f} GH/s"


class F2PoolCollector:
    def __init__(self, db_connection, account, api_token, currency='bitcoin', address=None):
        self.db = db_connection
        self.account = account
        self.api_token = api_token
        self.currency = currency or 'bitcoin'
        self.address = address

    def _headers(self):
        return {'F2P-API-SECRET': self.api_token, 'Content-Type': 'application/json'}

    def _body(self):
        # The v2 API always keys on mining_user_name; 'address' is not accepted here.
        return {'currency': self.currency, 'mining_user_name': self.account}

    def fetch(self):
        response = requests.post(API_URL, json=self._body(), headers=self._headers(), timeout=15)
        response.raise_for_status()
        return response.json()

    def fetch_balance(self):
        try:
            response = requests.post(
                BALANCE_URL, json=self._body(), headers=self._headers(), timeout=20
            )
            response.raise_for_status()
            return response.json().get('balance_info') or {}
        except Exception as exc:
            logger.warning("Could not read f2pool balance for %s: %s", self.account, exc)
            return {}

    def fetch_workers(self):
        try:
            response = requests.post(
                WORKER_URL, json=self._body(), headers=self._headers(), timeout=20
            )
            response.raise_for_status()
            return response.json().get('workers') or []
        except Exception as exc:
            logger.warning("Could not read f2pool worker list for %s: %s", self.account, exc)
            return []

    def normalize(self, payload, workers_list=None, balance=None):
        info = payload.get('info') or payload
        if isinstance(info, dict) and isinstance(info.get('info'), dict):
            info = info['info']

        hashrate_now = _first_number(info, ['hash_rate', 'hashrate', 'hash_rate_now', 'current_hash_rate'])
        hashrate_1h = _first_number(info, ['h1_hash_rate', 'hash_rate_1h', 'hour_hash_rate'])
        hashrate_1d = _first_number(info, ['h24_hash_rate', 'hash_rate_24h', 'day_hash_rate'])
        workers_list = workers_list or []
        online = [w for w in workers_list if (w.get('status') == 0)]
        workers = len(online) if workers_list else (
            info.get('worker_length_online') or info.get('workers_online') or 0
        )
        last_share = max(
            [w.get('last_share_at') or 0 for w in workers_list] or [0]
        ) or None
        stale_1h = info.get('h1_stale_hash_rate')

        now_ghs = _to_ghs(hashrate_now)
        return NormalizedPoolSnapshot(
            pool_type='f2pool',
            pool_address=self.address or self.account,
            pool_url=API_URL,
            recorded_at=datetime.now(timezone.utc),
            hashrate_1m_ghs=now_ghs,
            hashrate_5m_ghs=None,
            hashrate_1h_ghs=_to_ghs(hashrate_1h),
            hashrate_1d_ghs=_to_ghs(hashrate_1d),
            hashrate_7d_ghs=None,
            hashrate_1m_display=_format_ghs(now_ghs),
            hashrate_5m_display=None,
            hashrate_1h_display=_format_ghs(_to_ghs(hashrate_1h)),
            hashrate_1d_display=_format_ghs(_to_ghs(hashrate_1d)),
            hashrate_7d_display=None,
            workers=int(workers or 0),
            shares=None,
            best_share=None,
            best_ever=None,
            last_share_unix=last_share,
            authorised_unix=None,
            pool_total_miners=None,
            pool_total_hashrate_ghs=None,
            details={
                'source': 'f2pool',
                'currency': self.currency,
                'stale_hash_rate_1h': stale_1h,
                'workers': [
                    {
                        'name': (w.get('hash_rate_info') or {}).get('name'),
                        'hash_rate_ghs': _to_ghs((w.get('hash_rate_info') or {}).get('hash_rate')),
                        'status': w.get('status'),
                        'last_share_at': w.get('last_share_at'),
                    }
                    for w in workers_list
                ],
                'balance': balance or {},
                'raw': info,
            },
        )

    def collect(self):
        payload = self.fetch()
        snapshot = self.normalize(payload, self.fetch_workers(), self.fetch_balance())
        if snapshot.hashrate_1m_ghs is None and snapshot.hashrate_1d_ghs is None:
            logger.warning(
                "f2pool returned no recognised hashrate field for %s; raw keys: %s",
                self.account,
                list((payload.get('info') or payload).keys()),
            )
        PoolDataWriter(connection=self.db).write_snapshot(snapshot)
        logger.info(
            "Stored f2pool stats for %s: %s (%s workers)",
            self.account,
            snapshot.hashrate_1m_display or 'n/a',
            snapshot.workers,
        )


def collect_f2pool_data(db_connection, account, api_token, currency='bitcoin', address=None):
    """Collect f2pool statistics for one account (or anonymous address)."""
    if not api_token:
        logger.warning("Skipping f2pool for %s - no API token configured", account)
        return
    F2PoolCollector(
        db_connection, account=account, api_token=api_token, currency=currency, address=address
    ).collect()
