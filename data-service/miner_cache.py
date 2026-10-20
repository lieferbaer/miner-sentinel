"""Shared miner readings and one-request-at-a-time per miner (Avalon, A15 web interface).

The collector (every 10 s), the throttle controller and the Takt-Tabellen (every 15 s)
all read the same miner. They run as threads of this process, so:
  * every request to a miner goes through lock(ip): never two requests at the same time,
  * readings are kept here with their time (put/get); a job may use a reading that is
    at most max_age seconds old instead of asking the miner again,
  * every command that changes something on the miner clears its readings (clear),
    so the next check reads the miner again.
"""
import threading
import time
from contextlib import contextmanager

LOCK_TIMEOUT = 30          # s to wait for another request to the same miner

_guard = threading.Lock()
_locks = {}
_store = {}


def _lock_for(ip):
    with _guard:
        return _locks.setdefault(ip, threading.RLock())


@contextmanager
def lock(ip):
    """One request at a time per miner; raises OSError if the miner stays busy."""
    miner_lock = _lock_for(ip)
    if not miner_lock.acquire(timeout=LOCK_TIMEOUT):
        raise OSError('%s: andere Anfrage an den Miner läuft noch' % ip)
    try:
        yield
    finally:
        miner_lock.release()


def put(ip, key, value):
    with _guard:
        _store[(ip, key)] = (time.monotonic(), value)


def get(ip, key, max_age):
    """Stored reading if it is at most max_age seconds old, else None."""
    with _guard:
        entry = _store.get((ip, key))
    if entry is None or time.monotonic() - entry[0] > max_age:
        return None
    return entry[1]


def fetch(ip, key, max_age, read):
    """Stored reading if at most max_age seconds old, else read() (stored as well).
    max_age None: always read the miner. Read and store happen under the miner lock, so a
    command that clears the readings (also under the lock) is never followed by a reading
    from before it."""
    if max_age is not None:
        value = get(ip, key, max_age)
        if value is not None:
            return value
    with lock(ip):
        value = get(ip, key, max_age) if max_age is not None else None
        if value is None:
            value = read()
            if value:
                put(ip, key, value)
    return value


def clear(ip):
    """Forget all readings of this miner (after a mode switch, clock change, reboot ...)."""
    with _guard:
        for k in [k for k in _store if k[0] == ip]:
            del _store[k]


@contextmanager
def command(ip):
    """A command that changes something on the miner: under the lock, readings cleared afterwards
    (also if the command failed or the connection was dropped)."""
    with lock(ip):
        try:
            yield
        finally:
            clear(ip)
