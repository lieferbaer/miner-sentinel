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
    assert miner.sets == [330.0]                 # protection: 3 degC over 84 -> 20 MHz + 10 MHz, then 3 min wait
    assert ac.writes == [6]                      # > 84 degC: fan +1 (protection) as well


def test_cool_raises_clock_up_to_stock(miner):
    miner.tmax = 76.0
    miner.points = [300, 300, 300]
    ac = FakeAC(level=5)
    st, _ = run({}, miner, ac, 15 * 60)
    assert miner.sets[0] == 305.0 and max(miner.sets) <= 360          # 76 degC: +5 MHz (fine from below)


def test_protection_88_and_90(miner):
    ac = FakeAC(level=5)
    miner.tmax = 86.0
    st, now = run({}, miner, ac, 30)
    assert ac.writes[-1] == 6                                    # > 84 degC: +1 level
    miner.tmax = 88.5
    st, now = run(st, miner, ac, 60, start=now)
    assert ac.writes[-1] == 7 and miner.sets[-1] == miner.sets[-2] - 30   # >= 88: +2 levels, -30 MHz
    miner.tmax = 90.5
    st, now = run(st, miner, ac, 60, start=now)
    assert ac.writes[-1] == 8                                    # >= 90: +3 levels
    miner.tmax = 83.0
    st, now = run(st, miner, ac, 90, start=now)
    assert ac.writes[-1] == 8                                    # still above 80: protection stays
    miner.tmax = 79.0
    st, now = run(st, miner, ac, 60, start=now)
    assert ac.writes[-1] == 5                                    # <= 80 degC: back to the chosen level


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


def _throttled_row(st, level=5):
    r = row(st, level=level)
    r['connection_config']['throttle']['active'] = {'clock_mhz': 300.0}
    return r


def test_manual_clock_fan_holds_80(miner):
    ac = FakeAC(level=5)
    miner.tmax = 83.0                                 # higher manual clock: warmer
    st, now = {}, T0
    for i in range(30):
        st = fa.step(_throttled_row(st), now, ac)
        now += timedelta(seconds=5)
    assert miner.sets == [] and st['throttled'] is True and st['phase'] == 'manuell'
    assert ac.writes[:2] == [6, 7]                    # fan up (> 81 degC), 60 s apart
    miner.tmax = 76.0                                 # lower manual clock: the fan may go below the chosen level
    for i in range(60):
        st = fa.step(_throttled_row(st), now, ac)
        now += timedelta(seconds=5)
    assert ac.writes[-1] < 5


def test_lifting_manual_clock_returns_to_chosen_level(miner):
    ac = FakeAC(level=5)
    miner.tmax = 83.0
    st, now = {}, T0
    for i in range(20):
        st = fa.step(_throttled_row(st), now, ac)
        now += timedelta(seconds=5)
    assert ac.writes[-1] >= 6                         # fan above the chosen level
    miner.tmax = 79.0
    for i in range(int((fa.WAIT_S + 60) / 5)):        # throttle lifted: back to level 5 (clock first, fan later)
        st = fa.step(row(st), now, ac)
        now += timedelta(seconds=5)
    assert ac.writes[-1] == 5 and st['phase'] == 'normal'


def test_protection_uses_latest_reading(miner):
    ac = FakeAC(level=5)
    miner.tmax = 82.0
    st, now = run({}, miner, ac, 60)
    miner.tmax = 89.0                                # one hot reading is enough for the protection
    st = fa.step(row(st), now, ac)
    assert st['t_now'] == 89.0 and st['boost'] == 2 and ac.writes[-1] == 7


def test_learns_clock_after_10_quiet_minutes(miner):
    ac = FakeAC(level=3)
    miner.tmax = 82.0
    miner.points = [234, 234, 234]
    st, _ = run({}, miner, ac, 11 * 60, level=3)
    assert st['learned']['2/L4']['3'] == {'mhz': 234.0, 'at': st['learned']['2/L4']['3']['at'], 'source': 'laut Miner'}
    assert miner.sets == []                           # not set by the automation: the miner's value


def test_learns_the_set_clock_not_the_drifted_one(miner):
    ac = FakeAC(level=3)
    miner.tmax = 86.0
    st, now = run({}, miner, ac, 20, level=3)
    assert miner.sets == [330.0]                      # automation set 330 MHz (protection: 20 + 10 MHz)
    miner.points = [336, 336, 336]                    # the firmware drifts the clock up
    miner.tmax = 80.0                                 # <= 80 ends the protection, 80-84 is quiet
    st, _ = run(st, miner, ac, 11 * 60, level=3, start=now)
    assert st['learned']['2/L4']['3']['mhz'] == 330.0 and st['learned']['2/L4']['3']['source'] == 'gesetzt'


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


def test_calibration_fan_down_at_78_after_60_s(miner):
    ac = FakeAC(level=3)
    miner.tmax = 83.0
    st, now = run({}, miner, ac, 30, cal=True)
    assert ac.writes == [4]                                   # > 81 degC: +1 at once
    miner.tmax = 78.0
    st, now = run(st, miner, ac, 30, cal=True, start=now)
    assert ac.writes == [4]                                   # within 60 s: no change
    st, now = run(st, miner, ac, 40, cal=True, start=now)
    assert ac.writes == [4, 3]                                # <= 78 degC after 60 s: -1
    miner.tmax = 80.0
    st, now = run(st, miner, ac, 300, cal=True, start=now)
    assert ac.writes == [4, 3]                                # 78-81 degC: no change


def test_after_run_learned_clock_at_once(miner):
    ac = FakeAC(level=3)
    miner.tmax = 79.0
    miner.points = [120, 120, 120]                            # run ended at a low clock
    learned = {'2/L4': {'3': {'mhz': 231.0, 'at': 'x'}}}
    st, now = run({}, miner, ac, 30, level=3, cal=True)
    st['learned'] = learned
    st, now = run(st, miner, ac, fa.RAISE_DELAY_S + 20, level=3, start=now)   # run over
    assert miner.sets and miner.sets[0] == 231.0              # learned clock, not 10-MHz steps


def test_calibration_protection_at_90_at_once(miner):
    ac = FakeAC(level=1)
    miner.tmax = 79.0
    st, now = run({}, miner, ac, 30, cal=True)
    miner.tmax = 95.0                                         # firmware stock clocks at a low fan level
    st = fa.step(row(st, cal=True), now, ac)
    st = fa.step(row(st, cal=True), now + timedelta(seconds=5), ac)
    assert ac.writes and ac.writes[-1] >= 3                   # +2 at once, no 60 s wait


def test_start_clock_bounds():
    learned = {'2/L4': {'3': {'mhz': 231.0}, '5': {'mhz': 300.0}}}
    assert fa._start_clock(learned, '2/L4', 2, 261.0)[0] == 231.0    # lower level: at most level 3's value
    assert fa._start_clock(learned, '2/L4', 2, 200.0)[0] is None     # already below: keep
    assert fa._start_clock(learned, '2/L4', 4, 200.0)[0] == 231.0    # higher level: at least level 3's value
    assert fa._start_clock(learned, '2/L4', 4, 260.0)[0] is None     # between 3 and 5: keep
    bad = {'2/L4': {'2': {'mhz': 280.0}, '3': {'mhz': 231.0}}}
    assert fa._start_clock(bad, '2/L4', 2, 250.0)[0] == 231.0        # own value above level 3's: capped


def test_lowering_without_own_value_caps_at_higher_level(miner):
    ac = FakeAC(level=3)
    miner.tmax = 82.0
    miner.points = [261, 261, 261]
    learned = {'2/L4': {'3': {'mhz': 231.0, 'at': 'x'}}}
    st, now = run({}, miner, ac, 30, level=3)
    st['learned'] = learned
    st = fa.step(row(st, level=2), now, ac)
    assert miner.sets == [231.0] and ac.writes == []          # clock first, never above level 3's value
    miner.tmax = 79.0
    st, now = run(st, miner, ac, fa.WAIT_S + 10, level=2, start=now + timedelta(seconds=5))
    assert ac.writes == [2]                                    # fan after 3 min


def test_no_clock_raise_right_after_fan_change():
    old = (T0 - timedelta(seconds=600)).isoformat()
    st = {'changed_at': old, 'fan_set_at': (T0 - timedelta(seconds=60)).isoformat()}
    assert fa._clock_step(st, T0, 78.0, 78.0, 300.0, 360.0, fa.T_HIGH, False) is None   # fan changed 60 s ago
    st['fan_set_at'] = old
    assert fa._clock_step(st, T0, 78.0, 78.0, 300.0, 360.0, fa.T_HIGH, False) == 302.0   # 78 degC: +2 MHz
    st['fan_set_at'] = (T0 - timedelta(seconds=60)).isoformat()
    assert fa._clock_step(st, T0, 86.0, 86.0, 300.0, 360.0, fa.T_HIGH, False) == 280.0  # lowering stays at once


def test_drifting_firmware_protection_settles(miner):
    """21.10.: the firmware drifts a set clock up ~11 MHz; regulating from the reported clock ate every -10 MHz
    step, so the protection lifted the fan 40 times in 5 h. Now the steps add up and the fan stays at level 2."""
    ac = FakeAC(level=2)
    state = {'set': 225.0, 'rep': 225.0}

    def set_frequency(ip, port, points):
        miner.sets.append(sum(points) / len(points))
        state['set'] = miner.sets[-1]

    miner.set_frequency = set_frequency
    calibration.set_frequency = set_frequency
    st, now = {}, T0
    for i in range(int(2 * 3600 / 5)):
        state['rep'] = min(state['set'] + 11, state['rep'] + 0.2) if state['rep'] < state['set'] + 11 \
            else state['set'] + 11
        miner.points = [state['rep']] * 3
        miner.tmax = 82 + (state['rep'] - 210) * 0.15 - 3 * (ac.level - 2)
        st = fa.step(row(st, level=2), now, ac)
        now += timedelta(seconds=5)
    assert ac.writes.count(3) <= 2, (ac.writes, [round(x) for x in miner.sets], st['log'][-6:])
    assert ac.level == 2 and st['boost'] == 0, (ac.writes, [round(x) for x in miner.sets], st['log'][-6:])


def test_click_on_level_sets_learned_clock_at_once(miner):
    ac = FakeAC(level=1)
    miner.tmax = 82.0
    miner.points = [150, 150, 150]
    learned = {'2/L4': {'2': {'mhz': 196.0, 'at': 'x'}}}
    st, now = run({}, miner, ac, 30, level=1)
    st['learned'] = learned
    r = row(st, level=2)
    r['level_at'] = '2026-10-21T12:00:30+00:00'
    st = fa.step(r, now, ac)
    assert miner.sets == [196.0] and ac.writes == [2]          # higher level: clock and fan at once
    st = fa.step(dict(r, state=st), now + timedelta(seconds=5), ac)
    assert miner.sets == [196.0]                               # the same click only once
    miner.points = [207, 207, 207]                             # the firmware drifted
    st['clock_set'], st['clock_own'] = 215.0, True             # the automation had gone up meanwhile
    r2 = dict(r, state=st, level_at='2026-10-21T12:10:00+00:00')
    st = fa.step(r2, now + timedelta(seconds=10), ac)
    assert miner.sets[-1] == 196.0                             # clicking the same level again: back to 196


def test_raise_steps_fine_from_below():
    assert [fa._raise_step(t) for t in (79.5, 78.0, 77.0, 75.0, 74.0)] == [2, 2, 5, 5, 10]


def test_ceiling_blocks_raising_to_a_clock_that_needed_protection():
    old = (T0 - timedelta(seconds=1200)).isoformat()
    st = {'changed_at': old, 'fan_set_at': old,
          'ceiling': {'2/L4': {'2': {'mhz': 174.0, 'at': (T0 - timedelta(seconds=600)).isoformat()}}}}
    ceil = fa._ceiling(st, '2/L4', 2, T0)
    assert ceil == 174.0
    assert fa._clock_step(st, T0, 74.0, 74.0, 164.0, 360.0, fa.T_HIGH, False, ceil) == 172.0     # below 174
    assert fa._clock_step(st, T0, 74.0, 74.0, 172.0, 360.0, fa.T_HIGH, False, ceil) is None      # at the ceiling
    assert fa._ceiling(st, '2/L4', 2, T0 + timedelta(seconds=fa.CEILING_S)) is None              # expires


def _power_model_run(start_set, T_start, hours=3.0, seed=1):
    """Thermal model calibrated on 21.10.: set 164 MHz = 1774 W ~ 80 degC, +226 W jump at set ~174 MHz,
    0.03 degC/W, fan level ~3 degC, tau 95 s, firmware drift +11 MHz, TMax integer + noise."""
    import math
    import random
    random.seed(seed)
    ac = FakeAC(level=2)
    sim = {'set': start_set, 'rep': start_set + 11, 'T': T_start, 'age': 999.0, 'reading': round(T_start)}
    cuts = []

    def setf(ip, port, points):
        sim['set'], sim['age'] = sum(points) / len(points), 0.0

    def read(ip, port, fresh=False):
        return {'fields': {'TMax': str(sim['reading'])}, 'points': [sim['rep']] * 4, 'mode_key': '2/L4',
                'status': 'Work: In Work', 'elapsed': 1000}

    orig = (calibration.set_frequency, calibration.read_miner, fa._note)
    calibration.set_frequency, calibration.read_miner = setf, read
    fa._note = lambda st, now, text: (cuts.append(text) if 'dauerhaft' in text else None, orig[2](st, now, text))
    try:
        st, now, last_boost, protections, worst = {}, T0, 0, 0, 0
        for i in range(int(hours * 3600 / 5)):
            sim['age'] += 5
            sim['rep'] = sim['set'] + 11 * (1 - math.exp(-sim['age'] / 120))
            p = 1774 + 4.0 * (sim['rep'] - 175) + ((226 + 2.5 * (sim['rep'] - 182)) if sim['rep'] >= 182 else 0)
            T_eq = 80.0 + 0.03 * (p - 1774) - 3 * (ac.level - 2)
            sim['T'] += (T_eq - sim['T']) * (1 - math.exp(-5 / 95))
            if i % 2 == 0:
                sim['reading'] = round(sim['T'] + random.uniform(-0.7, 0.7))
            st = fa.step(row(st, level=2), now, ac)
            boost = int(st.get('boost') or 0)
            protections += 1 if boost and not last_boost else 0
            last_boost, worst = boost, max(worst, sim['reading'])
            now += timedelta(seconds=5)
        return protections, len(cuts), worst, ac.level
    finally:
        calibration.set_frequency, calibration.read_miner, fa._note = orig


def test_power_jump_no_protection_loop():
    """21.10. 09:24/09:45: 164 -> 174 MHz added 226 W, protection, back to 164, again and again."""
    protections, cuts, worst, level = _power_model_run(164, 80.0)
    assert protections <= 1 and cuts <= 1 and worst < 88 and level == 2


def test_protection_cuts_not_too_fast():
    """21.10. 08:14-08:35: five permanent cuts within 21 min (204 -> 164 MHz)."""
    protections, cuts, worst, level = _power_model_run(204, 84.0)
    assert cuts <= 2 and level == 2


def test_after_reboot_start_clock_at_once(miner):
    """21.10.: after switching the A15 off and on it ran its full stock clocks at fan level 2 (simulated: 93 degC)."""
    ac = FakeAC(level=2)
    miner.tmax = 82.0
    miner.points = [175, 175, 175]
    learned = {'2/L4': {'2': {'mhz': 164.0, 'at': 'x', 'source': 'gesetzt'}}}
    st, now = run({'learned': learned}, miner, ac, 30, level=2)
    st['learned'] = learned
    boot = {'fields': {'TMax': '-273'}, 'points': [], 'mode_key': '2/L4', 'status': 'Init', 'elapsed': 5}
    calibration.read_miner = lambda ip, port, fresh=False: boot              # booting: no clocks yet
    st = fa.step(row(st, level=2), now, ac)
    assert st.get('exit_learned') and miner.sets == []                       # kept until clocks are reported
    boot.update(points=[360, 360, 360], elapsed=40)                          # firmware stock clocks, TMax still -273
    st = fa.step(row(st, level=2), now + timedelta(seconds=5), ac)
    assert miner.sets == [164.0]                                             # start clock at once


def test_firmware_override_set_again(miner):
    ac = FakeAC(level=2)
    miner.tmax = 82.0
    st, now = run({}, miner, ac, 30, level=2)
    st.update(clock_set=164.0, clock_own=True, changed_at=(now - timedelta(seconds=120)).isoformat())
    miner.points = [360, 360, 360]                                           # firmware applied its stock clocks
    st = fa.step(row(st, level=2), now, ac)
    assert miner.sets[-1] == 164.0


class PowerAC(FakeAC):
    def __init__(self, level):
        super().__init__(level)
        self.events = []

    def set_fan_power(self, dev_id, port, on, level=None):
        self.events.append(('an' if on else 'aus', level))
        if on and level is not None:
            self.level = level


def test_maintenance_fan_off_and_back(miner):
    import miner_cache
    ip = '10.0.0.15'
    ac = PowerAC(level=2)
    miner.tmax = 82.0
    miner_cache.put(ip, 'estats', 'x')
    st, now = run({}, miner, ac, 30, level=2)
    maint = (now - timedelta(seconds=1)).isoformat()
    r = dict(row(st, level=2), maintenance_at=maint)
    st = fa.step(r, now, ac)
    assert ac.events == [] and st['maint']['since'] == maint          # still on: no fan command yet
    miner_cache.clear(ip)                                              # switched off: no central reading
    st = fa.step(dict(r, state=st), now + timedelta(seconds=20), ac)
    assert ac.events == [('aus', None)] and st['phase'] == 'wartung'
    st = fa.step(dict(r, state=st), now + timedelta(seconds=25), ac)
    assert ac.events == [('aus', None)]                                # only once
    miner_cache.put(ip, 'estats', 'x')                                 # on again
    st = fa.step(dict(r, state=st), now + timedelta(seconds=600), ac)
    assert ac.events[-1] == ('an', 2) and st.get('maint_end') and 'maint' not in st
    miner_cache.clear(ip)
