"""Temperatur-Automatik (A15 + AC Infinity fan): clock keeps TMax 80-84 degC, fan fixed at the chosen level."""
from datetime import datetime, timedelta, timezone

import pytest

import calibration
import fan_automation as fa

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
STOCK = [360, 360, 360]


class FakeAC:
    class ACError(Exception):
        pass

    def __init__(self, level=5):
        self.level = level
        self.writes = []

    def speed_of(self, dev_id, port):
        return self.level

    def set_fan_level(self, dev_id, port, level):
        self.writes.append(level)
        self.level = level


class Miner:
    def __init__(self, tmax=82.0, points=None):
        self.tmax = tmax
        self.points = list(points or STOCK)
        self.sets = []

    def read(self, ip, port, fresh=False):
        return {'fields': {'TMax': str(self.tmax)}, 'points': self.points, 'mode_key': '2/L4',
                'status': 'Work: In Work', 'elapsed': 1000}

    def set_frequency(self, ip, port, points):
        self.sets.append(sum(points) / len(points))
        self.points = list(points)


@pytest.fixture
def miner(monkeypatch):
    mn = Miner()
    monkeypatch.setattr(calibration, 'read_miner', mn.read)
    monkeypatch.setattr(calibration, 'set_frequency', mn.set_frequency)
    return mn


def row(state=None, level=5, enabled=True, cal=False):
    return {'id': 1, 'enabled': enabled, 'ac_dev_id': 'dev', 'ac_port': 1, 'level': level, 'state': state or {},
            'ip_address': '10.0.0.15', 'port': 4028, 'cal_active': cal,
            'connection_config': {'throttle': {'profiles': {'2/L4': {'points': STOCK}}}}}


def run(st, miner, ac, seconds, level=5, cal=False, step_s=fa.RUN_S, start=T0):
    """Run rounds every step_s seconds; returns the state and the time after the last round."""
    now = start
    for _ in range(int(seconds // step_s)):
        st = fa.step(row(st, level=level, cal=cal), now, ac)
        now += timedelta(seconds=step_s)
    return st, now


def test_in_band_nothing_changes(miner):
    ac = FakeAC(level=5)
    st, _ = run({}, miner, ac, 600)
    assert miner.sets == [] and ac.writes == [] and st['phase'] == 'normal'
    assert st['clock_now'] == 360.0             # the clock the miner really runs (shown on the page)


def test_too_hot_lowers_clock_then_waits(miner):
    miner.tmax = 87.0
    ac = FakeAC(level=5)
    st, _ = run({}, miner, ac, 170)
    assert miner.sets == [340.0]                 # 3 degC over 84 -> 20 MHz, then 3 min wait
    assert ac.writes == []                       # fan stays at the chosen level


def test_cool_raises_clock_up_to_stock(miner):
    miner.tmax = 76.0
    miner.points = [300, 300, 300]
    ac = FakeAC(level=5)
    st, _ = run({}, miner, ac, 15 * 60)
    assert miner.sets[0] == 310.0 and max(miner.sets) <= 360


def test_protection_88_and_90(miner):
    ac = FakeAC(level=5)
    miner.tmax = 88.5
    st, now = run({}, miner, ac, 60)
    assert ac.writes[-1] == 6 and miner.sets[0] == 330.0       # +1 level, -30 MHz
    miner.tmax = 90.5
    st, now = run(st, miner, ac, 60, start=now)
    assert ac.writes[-1] == 7                                    # +2 levels
    miner.tmax = 83.0
    st, now = run(st, miner, ac, 90, start=now)
    assert ac.writes[-1] == 5                                    # back to the chosen level


def test_lower_fan_only_after_clock_and_wait(miner):
    ac = FakeAC(level=7)
    miner.tmax = 83.0
    st, now = run({}, miner, ac, 60, level=7)
    assert st['base'] == 7
    st = fa.step(row(st, level=4), now, ac)
    assert st['phase'] == 'senken' and ac.writes == []
    # clock goes down until TMax <= 80, the fan stays at 7 meanwhile
    for i in range(1, 40):
        if miner.sets:
            miner.tmax = 79.5
        st = fa.step(row(st, level=4), now + timedelta(seconds=15 * i), ac)
        if ac.writes:
            break
    assert miner.sets and miner.sets[0] < 360
    assert ac.writes == [4]
    assert (15 * i) >= fa.WAIT_S                                 # applied only after the wait


def test_higher_fan_at_once(miner):
    ac = FakeAC(level=4)
    st, now = run({}, miner, ac, 60, level=4)
    st = fa.step(row(st, level=8), now, ac)
    assert ac.writes == [8]


def test_calibration_run_no_clocks_fan_holds_80(miner):
    ac = FakeAC(level=5)
    miner.tmax = 83.0
    st, now = run({}, miner, ac, 400, cal=True)
    assert miner.sets == []                      # no clock changes during a run
    assert ac.writes and ac.writes[0] == 6       # fan up to bring TMax to 80
    miner.tmax = 77.0
    st, _ = run(st, miner, ac, 400, cal=True, start=now)
    assert ac.writes[-1] < 6


def test_disable_gives_clocks_back(miner):
    miner.tmax = 87.0
    ac = FakeAC(level=5)
    st, now = run({}, miner, ac, 60)
    assert st['clock_set']
    st = fa.step(row(st, enabled=False), now, ac)
    assert miner.sets[-1] == 360.0 and st['off'] is True


def test_ac_error_keeps_clock_regulation(miner):
    class BrokenAC(FakeAC):
        def speed_of(self, dev_id, port):
            raise self.ACError('Cloud nicht erreichbar')

    miner.tmax = 87.0
    st = fa.step(row({'base': 5, 'seen_at': 'x', 'fan_set': 5}), T0, BrokenAC())
    for i in range(1, 4):
        st = fa.step(row(st), T0 + timedelta(seconds=15 * i), BrokenAC())
    assert miner.sets and 'Cloud' in st['ac_error']


def test_manual_throttle_has_priority(miner):
    miner.tmax = 87.0
    ac = FakeAC(level=5)
    st = {}
    for i in range(12):
        r = row(st)
        r['connection_config']['throttle']['active'] = {'clock_mhz': 300.0}
        st = fa.step(r, T0 + timedelta(seconds=15 * i), ac)
    assert miner.sets == [] and st['throttled'] is True


def test_protection_uses_latest_reading(miner):
    ac = FakeAC(level=5)
    miner.tmax = 82.0
    st, now = run({}, miner, ac, 60)
    miner.tmax = 89.0                                # one hot reading is enough for the protection
    st = fa.step(row(st), now, ac)
    assert st['t_now'] == 89.0 and st['boost'] == 1 and ac.writes[-1] == 6


def test_learns_clock_after_10_quiet_minutes(miner):
    ac = FakeAC(level=3)
    miner.tmax = 82.0
    miner.points = [234, 234, 234]
    st, _ = run({}, miner, ac, 11 * 60, level=3)
    assert st['learned']['2/L4']['3']['mhz'] == 234.0
    assert miner.sets == []


def test_lowering_uses_learned_clock(miner):
    ac = FakeAC(level=5)
    miner.tmax = 82.0
    learned = {'2/L4': {'3': {'mhz': 234.0, 'at': 'x'}}}
    st, now = run({'learned': learned}, miner, ac, 60, level=5)
    st['learned'] = learned
    st = fa.step(row(st, level=3), now, ac)
    assert miner.sets == [234.0] and ac.writes == []           # clock straight to the learned value
    st, now = run(st, miner, ac, fa.WAIT_S + 10, level=3, start=now + timedelta(seconds=5))
    assert ac.writes == [3]                                     # fan follows after 3 min


def test_raising_uses_learned_clock_after_fan(miner):
    ac = FakeAC(level=3)
    miner.tmax = 82.0
    miner.points = [234, 234, 234]
    learned = {'2/L4': {'5': {'mhz': 300.0, 'at': 'x'}}}
    st, now = run({'learned': learned}, miner, ac, 60, level=3)
    st['learned'] = learned
    st = fa.step(row(st, level=5), now, ac)
    assert ac.writes == [5] and miner.sets == []               # fan at once
    st, now = run(st, miner, ac, fa.RAISE_DELAY_S + 10, level=5, start=now + timedelta(seconds=5))
    assert miner.sets == [300.0]                               # learned clock 60 s later


def test_fan_read_back_after_10_s_no_double_write(miner):
    class LaggingAC(FakeAC):
        """The controller reports a new level only after > 5 s."""
        def __init__(self, level):
            super().__init__(level)
            self.reported = level
            self.changed = None

        def set_fan_level(self, dev_id, port, level):
            super().set_fan_level(dev_id, port, level)
            self.changed = self.now

        def speed_of(self, dev_id, port):
            if self.changed is not None and (self.now - self.changed).total_seconds() > 5:
                self.reported = self.level
            return self.reported

    ac = LaggingAC(level=3)
    miner.tmax = 82.0
    st, now = run({}, miner, ac, 30, level=3)
    for i in range(30):                                       # level 5 chosen, 150 s of rounds
        ac.now = now + timedelta(seconds=5 * i)
        st = fa.step(row(st, level=5), ac.now, ac)
    assert ac.writes == [5]                                   # no second write because of a stale read
    assert st['fan_read'] == 5
