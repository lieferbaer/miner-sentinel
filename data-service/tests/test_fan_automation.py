"""Temperatur-Automatik nach dem Plan vom 24.10.2026: Basisstufe fest, Takt regelt 80-84 °C, Protokoll je Zyklus."""
from datetime import datetime, timedelta, timezone

import pytest

import automation_journal
import calibration
import fan_automation as fa
import miner_cache

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
STOCK = {'2/L4': [330, 349, 369, 390], '2/L3': [351, 370, 390, 411], '2/L2': [371, 390, 410, 431],
         '2/L1': [391, 410, 430, 451], '2/L0': [411, 430, 450, 471]}
PROFILES = {k: {'points': STOCK[k]} for k in ('2/L4', '2/L3', '2/L0')}     # 01 und 02 kennt nur der Miner


class FakeAC:
    class ACError(Exception):
        pass

    def __init__(self, level=5):
        self.level = level
        self.writes = []
        self.power = []
        self.fail = False

    def speed_of(self, dev_id, port):
        return self.level

    def set_fan_level(self, dev_id, port, level):
        if self.fail:
            raise self.ACError('Cloud nicht erreichbar')
        self.writes.append(level)
        self.level = level
        return {'code': 200}

    def set_fan_power(self, dev_id, port, on, level=None):
        self.power.append(('an' if on else 'aus', level))
        if on and level is not None:
            self.level = level


class Miner:
    """The A15 as the automation sees it: TMax, the four clock points (SF0), the power level, uptime."""

    def __init__(self, tmax=82, level=4, points=None):
        self.tmax = tmax
        self.level = level
        self.points = list(points or STOCK['2/L4'])
        self.clock_sets, self.mode_sets = [], []
        self.elapsed = 1000
        self.status = 'Work: In Work'
        self.offline = False
        self.ignore_clock = False

    def read(self, ip, port, fresh=False):
        if self.offline:
            raise OSError('timed out')
        self.elapsed += 5
        return {'fields': {'TMax': str(self.tmax), 'MTmax': '%s 70 69' % self.tmax}, 'points': list(self.points),
                'mode_key': '2/L%d' % self.level, 'status': self.status, 'elapsed': self.elapsed,
                'conf': {'mode': 2, 'power_level': self.level}, 'power': 2000.0, 'ghsspd': 150000.0, 'dh': 1.2}

    def set_frequency(self, ip, port, points):
        self.clock_sets.append(sum(points) / len(points))
        if not self.ignore_clock:
            self.points = list(points)
        return 'STATUS=S,Msg=ASC 0 set OK'

    def set_mode(self, ip, conf, mode, level):
        self.mode_sets.append(level)
        self.level = level
        self.points = list(STOCK['2/L%d' % level])        # der Moduswechsel stellt den Standardtakt ein

    def clock(self):
        return round(sum(self.points) / len(self.points))


@pytest.fixture
def miner(monkeypatch):
    mn = Miner()
    monkeypatch.setattr(calibration, 'read_miner', mn.read)
    monkeypatch.setattr(calibration, 'set_frequency', mn.set_frequency)
    monkeypatch.setattr(calibration, 'a15_set_mode', mn.set_mode)
    return mn


def row(state=None, level=5, enabled=True, cal=False, level_at=None, maintenance_at=None, temp_test_at=None):
    return {'id': 1, 'device_id': 3, 'enabled': enabled, 'ac_dev_id': 'dev', 'ac_port': 1, 'level': level,
            'level_at': level_at, 'maintenance_at': maintenance_at, 'temp_test_at': temp_test_at, 'state': state or {},
            'ip_address': '10.0.0.15', 'port': 4028, 'cal_active': cal,
            'connection_config': {'throttle': {'profiles': PROFILES}}}


class Run:
    """Rounds every 5 s with one journal; temperature can follow a function of the clock and fan level."""

    def __init__(self, miner, ac, level=5, state=None, journal=None):
        self.miner, self.ac, self.level = miner, ac, level
        self.st = state or {}
        self.now = T0
        self.j = journal or automation_journal.MemoryJournal()
        self.kw = {}

    def go(self, seconds, temp=None, **kw):
        self.kw.update(kw)
        for _ in range(int(seconds // 5)):
            if temp is not None:
                self.miner.tmax = temp(self) if callable(temp) else temp
            self.st = fa.step(row(self.st, level=self.level, **self.kw), self.now, self.ac, self.j)
            self.now += timedelta(seconds=5)
        return self

    def events(self, kind):
        return [e for e in self.j.events if e['kind'] == kind]


def started(miner, ac, level=5, tmax=82, learned=None):
    """Automatik ein (Basisstufe `level`) and the start clock taken over."""
    r = Run(miner, ac, level)
    if learned:
        r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': learned, 'level_seen': 'x'}
        r.kw['level_at'] = 'klick'
    miner.tmax = tmax
    r.go(10)
    return r


# ---------------------------------------------------------------- Start (Kapitel 4)

def test_start_without_learned_value_uses_initial_value(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    assert [round(x) for x in miner.clock_sets] == [140]                 # Stufe 5 = 140 MHz, Modus 04 passt schon
    assert miner.mode_sets == [] and r.st['clock_set'] == 140 and r.st['phase'] == 'normal'
    assert r.events('start')[0]['details']['quelle'] == 'Initialwert'
    assert r.events('takt')[0]['takeover_state'] == 'gemessen'


def test_start_uses_learned_value_of_the_base_level(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 250, 'temp_avg': 82.0, 'at': 'x', 'count': 2}})
    assert r.st['clock_set'] == 250 and miner.clock() == 250
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert'


def test_start_with_mode_change_sets_mode_first(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.mode_sets == [2]                      # 390 MHz → mode 02 (Tabelle ohne Spielraum)
    assert r.st['stock']['2/L2'] == STOCK['2/L2']      # Standardtakt von mode 02 beim Wechsel gemerkt
    assert miner.clock() == 390
    kinds = [k for k in r.j.kinds() if k in ('modus', 'takt')]
    assert kinds == ['modus', 'takt']


def test_no_clock_command_when_miner_already_runs_the_target(miner):
    ac = FakeAC(level=5)
    miner.points = fa.points_for(STOCK['2/L4'], 140)
    r = started(miner, ac, level=5)
    assert miner.clock_sets == []                      # Zieltakt = Takt laut Miner: kein Befehl
    assert r.events('takt')[0]['details']['kein_befehl']


def test_old_state_starts_without_learned_values(miner):
    ac = FakeAC(level=2)
    old = {'learned': {'2/L4': {'2': {'mhz': 241}}}, 'seen_at': 'x', 'phase': 'normal', 'clock_set': 231}
    r = Run(miner, ac, level=2, state=old).go(10)
    assert r.st['v'] == fa.STATE_VERSION and r.st['learned'] == {}
    assert [round(x) for x in miner.clock_sets] == [110]                 # neue Regelung startet ohne Lernwerte: Stufe 2 = 110


def test_click_on_level_starts_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.level = 3
    r.go(10, level_at='neu')
    assert miner.clock() == 120 and r.st['fan_target'] == 3 and ac.level == 3


# ---------------------------------------------------------------- Schritt 1: hohe Chiptemperatur

def test_high_temperature_fan_and_clock_at_once(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=88)
    assert ac.level == 7 and r.st['phase'] == 'korrektur'        # Basisstufe + 2
    assert miner.clock() == 270                                   # 300 − 30 sofort
    r.go(25, temp=88)
    assert miner.clock() == 270                                   # Beobachtungsfenster 30 s
    r.go(10, temp=88)
    assert miner.clock() == 240                                   # noch über der Schwelle, nicht fallend: erneut
    r.go(40, temp=lambda r: 88 if r.now < T0 + timedelta(seconds=105) else 87)
    assert miner.clock() in (240, 210)                            # fallend → keine weitere Korrektur in diesem Fenster


def test_fan_correction_relative_to_base_not_added(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=85)
    assert ac.level == 4
    r.go(5, temp=90)
    assert ac.level == 5                                          # Basisstufe + 2, nicht 4 + 2
    r.go(5, temp=94)
    assert ac.level == 5


def test_lower_correction_level_only_after_30_s(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=88)
    assert ac.level == 7
    r.go(25, temp=87)
    assert ac.level == 7                                          # 87 verlangt + 1: erst nach 30 s
    r.go(10, temp=87)
    assert ac.level == 6


def test_clock_below_100_is_an_error_and_not_sent(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 130, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    sends = len(miner.clock_sets)
    r.go(5, temp=88)                                              # 130 − 30 = 100: erlaubt
    assert miner.clock() == 100
    r.go(35, temp=89)                                             # 100 − 30 = 70: Fehler, nicht senden
    assert len(miner.clock_sets) == sends + 1 and r.st['clock_set'] == 100
    assert r.events('fehler')[0]['details']['grund'] == 'takt_unter_100'


def test_high_temperature_with_mode_change_clock_first_then_mode_then_clock(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 400, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.level == 2 and miner.clock() == 400
    miner.clock_sets.clear()
    r.go(15, temp=88)                                             # 400 − 30 = 370 → mode 03 (Spielraum: bei 370 runter)
    assert miner.mode_sets[-1] == 3 and miner.level == 3
    assert miner.clock_sets[0] == pytest.approx(370, abs=1) and miner.clock() == 370
    assert len(miner.clock_sets) == 2                             # Takt, Modus, Takt erneut


# ---------------------------------------------------------------- Schritt 2: Korrekturphase und Rückkehr

def test_return_after_one_minute_in_band(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=85)
    assert ac.level == 6
    r.go(55, temp=83)
    assert ac.level == 6
    r.go(15, temp=83)
    assert ac.level == 5 and r.st['phase'] == 'normal'
    assert r.events('rueckkehr')


def test_return_at_once_below_80(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=85)
    r.go(5, temp=79)
    assert r.st['phase'] == 'normal' and r.st['fan_target'] == 5


def test_no_plus_steps_in_correction_phase(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=86)
    clock = miner.clock()
    r.go(50, temp=82)                                             # in 80-84, noch keine Minute: keine Plus-Schritte
    assert miner.clock() == clock and r.st['phase'] == 'korrektur'


# ---------------------------------------------------------------- Schritt 3: Feintuning (Kapitel 6)

def test_no_clock_change_between_79_and_84(miner):
    """Nutzer 25.10.: zwischen 79 und 84 °C keine Taktänderung – auch nicht bei steigender Tendenz (82 → 83 → 84)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    sets, n = list(miner.clock_sets), len(r.events('takt'))
    for temp in (82, 83, 84, 83, 81, 80, 79, 80, 84):
        r.go(60, temp=temp)
    assert miner.clock_sets == sets and miner.clock() == 300 and len(r.events('takt')) == n

def test_fine_plus_steps_by_temperature(miner):
    """Kleinster Taktschritt 5 MHz (Nutzer 25.10.): unter 79 °C +5 (70–78), +20 (60–69), +40 (unter 60)."""
    for temp, step in ((79, 0), (78, 5), (76, 5), (72, 5), (65, 20), (55, 40)):
        miner.clock_sets.clear()
        ac = FakeAC(level=5)
        r = started(miner, ac, level=5, tmax=temp, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}})
        r.go(40, temp=temp)
        assert miner.clock() == 200 + step, temp

def test_no_plus_step_while_rising(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=70, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(40, temp=lambda r: 70 + int((r.now - T0).total_seconds() // 10))
    assert miner.clock() == 200


def test_plus_step_stays_5_after_strong_effect(miner):
    """Die Regel „eine Größe kleiner“ entfällt: kleiner als 5 MHz gibt es nicht mehr."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=72, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(35, temp=72)
    assert miner.clock() == 205
    r.go(30, temp=lambda r: 74 if r.now < T0 + timedelta(seconds=75) else 73)   # +2 °C nach dem Schritt
    r.go(10, temp=73)
    assert miner.clock() == 210

# ---------------------------------------------------------------- Modus (Kapitel 8)

def test_mode_up_at_361_and_down_only_at_350(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.level == 4
    r.go(40, temp=78)                                            # +5 → 363 → mode 03 zuerst
    assert miner.level == 3 and miner.clock() == 363
    st = r.st
    st['clock_set'] = 352
    assert fa.required_level(352, 3) == 3 and fa.required_level(350, 3) == 4
    assert fa.required_level(361, 4) == 3 and fa.required_level(360, 4) == 4
    assert fa.required_level(370, 2) == 3 and fa.required_level(371, 2) == 2
    assert fa.table_level(360) == 4 and fa.table_level(361) == 3 and fa.table_level(440) == 0


def test_clock_capped_at_440(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=55, learned={'5': {'mhz': 430, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(40, temp=55)
    assert r.st['clock_set'] == 440 and miner.level == 0


# ---------------------------------------------------------------- Schritt 5: Ausnahme

def test_exception_lowers_fan_stepwise_after_3_minutes(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=60, learned={'5': {'mhz': 440, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(170, temp=60)
    assert ac.level == 5
    r.go(15, temp=60)
    assert ac.level == 4 and r.st['phase'] == 'ausnahme'
    r.go(185, temp=60)
    assert ac.level == 3
    r.go(10, temp=72)                                            # über 70: Ende, Stufe bleibt reduziert
    assert r.st['phase'] == 'normal' and ac.level == 3 and r.events('ausnahme_ende')
    r.go(5, temp=85)                                             # zurück nur über „Hohe Chiptemperaturen“
    assert ac.level == 6


# ---------------------------------------------------------------- Lernen (Kapitel 7)

def test_learns_after_15_minutes_unchanged(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(15 * 60 + 30, temp=82)
    lw = r.st['learned']['4']
    assert lw['mhz'] == 280 and lw['count'] == 2 and lw['temp_avg'] == 82.0
    assert r.events('lernwert')


def test_learning_in_band_mean_clock_is_stored(miner):
    """15 min in 80–84 °C ohne Taktänderung: Lernwert = Mittel des gesetzten Takts."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(300, temp=82)
    r.go(625, temp=83)
    lw = r.st['learned']['4']
    assert lw['count'] == 2 and lw['mhz'] == 280

def test_short_dip_below_79_keeps_learning_long_one_restarts(miner):
    """Lernbereich 79–84 °C (Nutzer 25.10.): 79 °C gehört dazu, Ausflüge darunter bis 60 s brechen nicht ab."""
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    since = r.st['learn_since']
    r.go(120, temp=79)                                            # 79 °C: im Lernbereich
    r.go(30, temp=78)                                             # 30 s Ausflug: Lernperiode läuft weiter
    r.go(30, temp=82)
    assert r.st['learn_since'] == since
    r.go(70, temp=78)                                             # länger als 60 s: neu
    assert r.st['learn_since'] is None and r.events('lernen_ende')[-1]['details']['ergebnis'] == 'abgebrochen'


def test_fan_change_restarts_learning(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 280, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    r.go(5, temp=85)                                              # hohe Chiptemperatur → Lüfter + 1
    assert r.st['learn_since'] is None


# ---------------------------------------------------------------- Situationen

def test_firmware_clock_change_is_sent_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    miner.points = list(STOCK['2/L4'])                           # Miner fährt wieder seinen Standardtakt
    r.go(10, temp=82)
    assert miner.clock() == 250 and r.events('firmware_takt')


def test_restart_of_the_miner_starts_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=82)
    miner.elapsed = 10
    miner.points = list(STOCK['2/L4'])
    r.go(10, temp=82)
    assert r.events('neustart') and r.events('start')[-1]['trigger'] == 'Neustart des Miners'
    assert miner.clock() == 250


def test_restart_caused_by_own_mode_switch_is_not_a_restart(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert not r.events('neustart')


def test_calibration_run_no_clock_fan_only_for_high_temperature(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    sends = len(miner.clock_sets)
    r.go(60, temp=70, cal=True)
    assert len(miner.clock_sets) == sends and r.st['phase'] != 'korrektur'
    r.go(5, temp=88)
    assert ac.level == 7 and len(miner.clock_sets) == sends      # Lüfter reagiert, kein Takt
    r.go(10, temp=82, cal=False)
    assert r.events('messung_ende') and r.events('start')[-1]['trigger'] == 'nach Takt-Messung'


def test_over_hot_no_decision_then_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.status = 'Over Hot'
    sends = len(miner.clock_sets)
    r.go(30, temp=95)
    assert len(miner.clock_sets) == sends and r.events('over_hot')
    miner.status = 'Work: In Work'
    r.go(10, temp=80)
    assert r.events('start')[-1]['trigger'] == 'nach Over Hot'


def test_miner_offline_fan_unchanged(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    miner.offline = True
    writes = list(ac.writes)
    r.go(30)
    assert ac.writes == writes and len(r.events('miner_offline')) == 1
    assert r.j.cycles[-1]['decision'] == 'Miner antwortet nicht'


def test_automation_off_sends_no_clock(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    sends = len(miner.clock_sets)
    r.go(30, enabled=False)
    assert len(miner.clock_sets) == sends and r.st['phase'] == 'aus' and r.st['off']


def test_fan_error_retried_until_it_works(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    ac.fail = True
    r.go(5, temp=86)
    assert ac.level == 5 and r.st['ac_error']
    r.go(25, temp=86)
    ac.fail = False
    r.go(25, temp=86)
    assert ac.level == 6
    tries = r.events('luefter')
    assert [t['ok'] for t in tries][-2:] == [False, True] or tries[-1]['ok']


def test_maintenance_fan_off_and_back(miner):
    ip = '10.0.0.15'
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2)
    miner_cache.put(ip, 'estats', 'x')
    maint = (r.now - timedelta(seconds=1)).isoformat()
    r.go(5, maintenance_at=maint)
    assert ac.power == []
    miner_cache.clear(ip)
    r.go(10)
    assert ac.power == [('aus', None)] and r.st['phase'] == 'wartung'
    miner_cache.put(ip, 'estats', 'x')
    r.go(5)
    assert ac.power[-1] == ('an', 2) and r.st.get('maint_end') and r.st.get('maint_ended_at')
    miner_cache.clear(ip)


# ---------------------------------------------------------------- Protokoll

def test_every_round_is_recorded(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(60, temp=82)
    assert len(r.j.cycles) == 14
    c = r.j.cycles[-1]
    assert c['chip_temp'] == 82 and c['clock_set'] == 140 and c['base_level'] == 5 and c['mode_key'] == '2/L4'


def test_thermal_response_10_values_every_30_s(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(310, temp=82)
    takt = r.events('takt')[0]
    assert len(takt['thermal']) == 10 and takt['takeover_state'] == 'gemessen'
    assert takt['api_ms'] is not None and takt['request'].startswith('ascset 0,frequency,')


def test_mode_command_never_logs_pool_credentials(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    modus = r.events('modus')[0]
    assert 'passwd' not in modus['request'] and modus['request'].startswith('mode=2, power_level=02')


# ---------------------------------------------------------------- Simulation (Schätzmodell, keine Messwerte)

def _sim(level, room=0.0, hours=2.0, start_clock=200):
    """Annahmen: Gleichgewicht 60 °C + 2 °C je 10 MHz über 200 − 10 °C je Lüfterstufe über 5 + Raum,
    Zeitkonstante 120 s, Messwert ganzzahlig."""
    import math
    mn = Miner(tmax=70, points=fa.points_for(STOCK['2/L4'], start_clock))
    ac = FakeAC(level=level)
    calibration.read_miner, calibration.set_frequency, calibration.a15_set_mode = mn.read, mn.set_frequency, mn.set_mode
    r = Run(mn, ac, level)
    temp = [75.0]
    peak = [0]

    def model(run):
        eq = 60 + 2 * (mn.clock() - 200) / 10 - 10 * (ac.level - 5) + room(run) if callable(room) else \
            60 + 2 * (mn.clock() - 200) / 10 - 10 * (ac.level - 5) + room
        temp[0] += (eq - temp[0]) * (1 - math.exp(-5 / 120))
        peak[0] = max(peak[0], temp[0])
        return int(round(temp[0]))
    r.go(hours * 3600, temp=model)
    return r, peak[0], mn


def test_simulation_reaches_band_and_stays_safe(miner):
    r, peak, mn = _sim(5)
    assert peak < 86
    band = [c for c in r.j.cycles[-360:] if 80 <= (c['chip_temp'] or 0) <= 84]
    assert len(band) > 300                                       # letzte 30 min fast nur im Zielbereich
    assert r.st['learned'].get('5')


def test_simulation_room_plus_15_corrects(miner):
    r, peak, mn = _sim(5, room=lambda run: 15 if run.now >= T0 + timedelta(hours=1) else 0)
    assert peak < 88 and r.st['fan_target'] == 5


# ---------------------------------------------------------------- Befunde der Prüfung (25.10.)

def test_start_after_restart_even_with_invalid_temperature(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    miner.points = list(STOCK['2/L4'])
    r.go(5, temp=-273)                                            # booting: TMax noch ungültig
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and miner.clock() == 250


def test_alternating_84_85_corrects_at_most_once_per_window(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=lambda r: 85 if int((r.now - T0).total_seconds() // 5) % 2 else 84)
    assert miner.clock() == 290                                   # nur die erste Korrektur, danach Fenster/Tendenz


def test_entering_band_from_below_is_no_minus_step(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(35, temp=78)
    assert miner.clock() == 255
    r.go(40, temp=80)
    assert miner.clock() == 255                                   # 78 → 80 ist das Erreichen des Ziels, kein −

def test_phase_messung_during_calibration(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5)
    r.go(10, temp=82, cal=True)
    assert r.st['phase'] == 'messung' and r.j.cycles[-1]['phase'] == 'messung'


def test_superseded_command_is_not_measured(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.ignore_clock = True
    r.go(40, temp=82)
    r.go(5, temp=78)                                              # +5 gesendet, Miner übernimmt nicht
    r.go(25, temp=78)                                             # nach 20 s erneut gesendet
    takt = [e for e in r.events('takt') if e.get('clock_new') == 305]
    assert len(takt) >= 2 and takt[0]['takeover_state'] == 'nicht gemessen' and takt[-1]['attempt'] == 2


def test_switch_to_new_logic_keeps_maintenance_and_does_not_loop(miner):
    ip = '10.0.0.15'
    ac = FakeAC(level=2)
    maint = (T0 - timedelta(hours=1)).isoformat()
    old = {'maint': {'since': maint, 'seen_off': True, 'fan_off': True, 'fan_before': 2}, 'fan_set': 2,
           'phase': 'wartung', 'learned': {'2/L4': {'2': {'mhz': 241}}}}
    miner_cache.clear(ip)
    r = Run(miner, ac, level=2, state=old)
    r.go(30, maintenance_at=maint)                                # A15 aus
    assert ac.power == [] and r.st['v'] == fa.STATE_VERSION       # Lüfter war schon aus: kein neuer Befehl
    assert len([n for n in r.st['log'] if 'Automatik ein' in n[1]]) == 1
    miner_cache.put(ip, 'estats', 'x')
    r.go(5)
    assert ac.power[-1] == ('an', 2) and r.st.get('maint_end')
    miner_cache.clear(ip)


def test_missing_stock_clock_is_estimated_and_never_blocks(miner):
    ac = FakeAC(level=5)
    miner.level, miner.points = 1, list(STOCK['2/L1'])           # mode 01: kein Profil, kein gemerkter Standardtakt
    r = started(miner, ac, level=5, learned={'5': {'mhz': 410, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.clock() == 410
    r.go(5, temp=86)                                              # kurz in 79–84 °C: −10
    assert miner.clock() == 400 and r.events('fehler')[0]['details']['grund'] == 'standardtakt_fehlt'


def test_stock_is_taken_only_after_the_firmware_applied_it(miner, monkeypatch):
    ac = FakeAC(level=4)

    def slow_mode(ip, conf, mode, level):                         # Webseite meldet den Modus, SF0 noch alt
        miner.mode_sets.append(level)
        miner.level = level
    monkeypatch.setattr(calibration, 'a15_set_mode', slow_mode)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert '2/L2' not in (r.st.get('stock') or {}) and miner.clock_sets == []   # wartet auf den Standardtakt
    miner.points = list(STOCK['2/L2'])                            # jetzt hat die Firmware ihn eingestellt
    r.go(10, temp=82)
    assert r.st['stock']['2/L2'] == STOCK['2/L2'] and miner.clock() == 390


def test_fan_read_error_retried_only_after_60_s(miner):
    calls = []
    ac = FakeAC(level=5)

    def broken(dev_id, port):
        calls.append(1)
        raise ac.ACError('Timeout')
    ac.speed_of = broken
    r = started(miner, ac, level=5)
    r.go(55, temp=82)
    assert len(calls) == 1


def test_firmware_ramp_after_a_clock_change_is_no_override(miner, monkeypatch):
    """25.10. live: 183 → 100 MHz, die Firmware meldet erst 152, dann 123 – das ist die Rampe, kein Überschreiben."""
    ac = FakeAC(level=1)
    ramp = []

    def ramping(ip, port, points):
        miner.clock_sets.append(sum(points) / len(points))
        ramp.extend([[p + 52 for p in points], [p + 23 for p in points], list(points)])
        return 'set OK'
    monkeypatch.setattr(calibration, 'set_frequency', ramping)
    orig = miner.read

    def read(ip, port, fresh=False):
        if ramp:
            miner.points = ramp.pop(0)
        return orig(ip, port, fresh)
    monkeypatch.setattr(calibration, 'read_miner', read)
    miner.points = fa.points_for(STOCK['2/L4'], 183)
    r = started(miner, ac, level=1)
    r.go(30, temp=70)
    assert [round(x) for x in miner.clock_sets].count(100) == 1 and not r.events('firmware_takt')



# ---------------------------------------------------------------- Unruhe an der 80-°C-Grenze (25.10.)

def test_calm_5_min_after_a_hot_reduction(miner):
    """Nutzer 25.10.: letzte Taktänderung über 84 °C, dann unter 79 °C → 5 min keine Taktänderung (ab dem ersten
    Unterschreiten), danach wieder Plus-Schritte."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=85)
    assert r.st['clock_set'] == 290                               # ab 85 °C −10
    r.go(40, temp=82)
    since = r.now
    r.go(5, temp=78)
    ev = r.events('taktsperre')
    until = since + timedelta(minutes=5)
    assert len(ev) == 1 and ev[0]['trigger'].startswith('Taktsperre 5 min bis ' + until.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    assert r.st['calm']['until'] == until.isoformat()
    r.go(5 * 60 - 5, temp=lambda r: 74 if r.now < since + timedelta(minutes=2) else 78)
    assert r.st['clock_set'] == 290                               # 5 min keine Taktänderung, auch bei 74 °C
    r.go(5, temp=78)
    assert r.st['clock_set'] == 295 and not r.st.get('calm')      # danach +5


def test_calm_ends_above_84(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=85)
    r.go(40, temp=82)
    r.go(60, temp=78)
    assert r.st.get('calm')
    r.go(5, temp=85)
    assert not r.st.get('calm') and r.events('taktsperre_ende') and r.st['clock_set'] == 280   # wieder −10


def test_calm_only_once_per_hot_change_and_not_after_a_plus_step(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=85)
    r.go(40, temp=82)
    r.go(60, temp=78)
    r.go(5 * 60, temp=80)                                         # Sperre läuft im Bereich ab
    assert not r.st.get('calm') and r.st['clock_set'] == 290
    r.go(35, temp=78)                                             # wieder unter 79: keine neue Sperre, +5
    assert len(r.events('taktsperre')) == 1 and r.st['clock_set'] == 295
    r.go(35, temp=77)
    assert r.st['clock_set'] == 300 and len(r.events('taktsperre')) == 1   # letzte Änderung bei 78 °C: keine Sperre

# ---------------------------------------------------------------- Temperatur-Test (25.10., v2.0.6)

def _test_run(miner, ac):
    """Automatik auf Basisstufe 2 (Lernwert 319,2 MHz), dann Temperatur-Test gestartet."""
    r = started(miner, ac, level=2, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.kw['temp_test_at'] = r.now.isoformat()
    return r


def _measuring(r, step):
    """Bis Schritt `step` misst (Chiptemperatur 80 °C)."""
    for _ in range(2000):
        tt = r.st.get('test') or {}
        if tt.get('step') == step and tt.get('phase') == 'messen':
            return r
        r.go(5, temp=80)
    raise AssertionError('Schritt %d misst nicht' % step)


def test_temp_test_begins_at_learned_value_and_waits_for_80(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    miner.points = fa.points_for(STOCK['2/L4'], 300)             # Takt steht woanders
    r.go(10, temp=83)
    assert r.st['clock_set'] == 319 and miner.clock() == 319
    assert r.st['test']['step'] == 0 and r.st['test']['phase'] == 'beginn'
    r.go(60, temp=81)
    assert r.st['test']['step'] == 0 and miner.clock() == 319     # über 80 °C: warten, keine Taktänderung
    r.go(5, temp=80)
    assert r.st['test']['step'] == 1 and r.st['clock_set'] == 349
    assert r.events('test_schritt')[0]['trigger'] == 'Temperatur-Test Schritt 1/2: Takt 349 MHz (+30 MHz)'


def test_temp_test_waits_2_min_after_takeover_then_measures_20_min(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)                                             # Beginn (Lernwert steht schon), Schritt 1 übernommen
    tt = r.st['test']
    took = r.now - timedelta(seconds=5)
    assert tt['step'] == 1 and tt['phase'] == 'warten'
    assert tt['wait_ends'] == (took + timedelta(minutes=2)).isoformat()
    sets = list(miner.clock_sets)
    r.go(115, temp=80)
    assert r.st['test']['phase'] == 'warten'
    r.go(5, temp=80)
    tt = r.st['test']
    ends = took + timedelta(minutes=22)
    assert tt['phase'] == 'messen' and tt['measure_ends'] == ends.isoformat()
    assert r.events('test_messung')[-1]['trigger'] == ('Temperatur-Test Schritt 1/2: Messung läuft bis '
                                                       + ends.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    r.go(20 * 60 - 5, temp=84)                                    # 84 °C: keine Taktregelung im Test
    assert r.st['test']['step'] == 1 and miner.clock_sets == sets
    r.go(5, temp=84)
    assert r.st['test']['step'] == 2 and r.st['clock_set'] == 379


def test_temp_test_second_step_mode_first_then_clock_wait_after_takeover(miner):
    """379 MHz liegt in mode 03: erst Modus, dann Takt (allgemeine Regel); die 2 min beginnen nach der Übernahme des
    Takts nach dem Moduswechsel."""
    ac = FakeAC(level=2)
    r = _measuring(_test_run(miner, ac), 1)
    n = len(r.j.events)
    r.go(20 * 60 + 15, temp=80)
    kinds = [e['kind'] for e in r.j.events[n:] if e['kind'] in ('test_schritt', 'modus', 'takt')]
    assert kinds == ['test_schritt', 'modus', 'takt'] and miner.mode_sets == [3]
    r = _measuring(r, 2)
    takt = [e for e in r.j.events if e['kind'] == 'takt' and e.get('clock_new') == 379][-1]
    assert takt['takeover_state'] == 'gemessen'
    measure_from = datetime.fromisoformat(r.st['test']['measure_from'])
    assert measure_from - takt['takeover_at'] == timedelta(minutes=2)


def test_temp_test_finishes_then_start_with_learned_value(miner):
    ac = FakeAC(level=2)
    r = _measuring(_test_run(miner, ac), 2)
    r.go(20 * 60, temp=82)
    end = r.events('test_ende')[-1]
    assert end['details']['ergebnis'] == 'fertig' and 'test' not in r.st and r.st.get('test_clear')
    assert r.events('start')[-1]['trigger'] == 'nach Temperatur-Test' and r.st['clock_set'] == 319


def test_temp_test_aborts_at_96_then_high_temperature_rules(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    assert r.st['test']['step'] == 1
    r.go(5, temp=95)
    assert 'test' in r.st and ac.level == 2 and r.st['phase'] == 'normal'   # keine Lüfterkorrektur im Test
    r.go(5, temp=96)
    end = r.events('test_ende')[-1]
    assert end['details']['ergebnis'] == 'abgebrochen' and '96 °C' in end['trigger'] and 'test' not in r.st
    assert r.st['phase'] == 'korrektur' and r.st['fan_target'] == 4 and r.st['clock_set'] == 249


def test_temp_test_aborts_on_restart_of_the_miner(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    miner.elapsed = 10
    r.go(5, temp=80)
    assert r.events('test_ende')[-1]['details']['grund'] == 'Neustart des Miners' and 'test' not in r.st
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and r.st['clock_set'] == 319


def test_temp_test_aborts_on_new_base_level(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    r.kw['level_at'] = 'neu'
    r.go(5, temp=80)
    assert r.events('test_ende')[-1]['details']['grund'] == 'Basisstufe gewählt (Stufe 2)' and 'test' not in r.st


def test_temp_test_restart_or_base_level_before_the_test_began_does_not_abort(miner):
    """Review 25.10.: Neustart bzw. Basisstufe kurz vor dem Beginn (z. B. Test gestartet, während die A15 neu startet)
    bricht nicht ab – Start und Test-Beginn setzen denselben Lernwert."""
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    miner.elapsed = 10
    r.kw['level_at'] = 'neu'
    r.go(10, temp=82)
    assert not r.events('test_ende') and r.st['test']['step'] == 0 and r.st['clock_set'] == 319


def test_temp_test_abort_reason_wins_over_the_end(miner):
    ac = FakeAC(level=2)
    r = _measuring(_test_run(miner, ac), 2)
    r.go(20 * 60 - 5, temp=82)
    r.go(5, temp=96)                                              # 96 °C genau in der letzten Runde
    end = r.events('test_ende')[-1]
    assert end['details']['ergebnis'] == 'abgebrochen' and not [e for e in r.events('start')
                                                                if e['trigger'] == 'nach Temperatur-Test']


def test_temp_test_no_learning_fan_stays_on_base_level(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(20 * 60, temp=84)
    assert r.st['learned']['2']['count'] == 1 and not r.events('lernwert')
    assert r.st['test']['step'] == 0 and ac.level == 2


def test_temp_test_ended_by_user(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.go(15, temp=80)
    r.go(10, temp=86)
    assert r.st['phase'] == 'normal'
    r.kw['temp_test_at'] = None
    r.go(5, temp=86)
    assert 'test' not in r.st and r.st['phase'] == 'korrektur'    # 86 °C ist ohne Test wieder „hoch“


def test_temp_test_state_before_v206_begins_new(miner):
    ac = FakeAC(level=2)
    r = _test_run(miner, ac)
    r.st['test'] = {'started': r.kw['temp_test_at'], 'stage': 2, 'stages': 5, 'target': 86, 'band': [85, 87],
                    'reached_at': None, 'stage_ends': None, 'ends': None}
    r.go(10, temp=83)
    assert r.st['test']['step'] == 0 and r.st['test']['phase'] == 'beginn' and not r.st.get('last_error')


def test_no_calm_during_takt_messung(miner):
    """Review 25.10.: während der Takt-Messung ist die Taktregelung pausiert – keine Taktsperre."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=85)
    r.go(40, temp=82)
    r.kw['cal'] = True
    r.go(60, temp=72)
    assert not r.events('taktsperre') and not r.st.get('calm')


# ---------------------------------------------------------------- Absenkung nach Aufenthalt (Nutzer 25.10.)

def test_85_after_short_stay_in_band_minus_10(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=75)                                             # unter 79 °C
    clock = r.st['clock_set']
    r.go(6 * 60, temp=82)                                         # 6 min in 79–84 °C
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 10 and ac.level == 6
    takt = r.events('takt')[-1]
    assert takt['trigger'] == 'Chiptemperatur 85 °C nach 6 min in 79–84 °C: Takt −10 MHz'
    assert r.events('hohe_temperatur')[-1]['details']['aufenthalt_s'] == 360


def test_85_and_86_after_long_stay_minus_5_for_the_whole_correction_phase(miner):
    """Aufenthalt ab 10 min → −5; gilt für jede Absenkung bei 85/86 °C bis zum Ende der Korrekturphase, auch nach
    einem kurzen Rückgang auf 84 °C."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(11 * 60, temp=82)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 295                               # −5
    r.go(35, temp=85)
    assert r.st['clock_set'] == 290                               # nicht fallend: wieder −5
    r.go(10, temp=84)                                             # kurz darunter, Korrekturphase läuft weiter
    r.go(30, temp=86)
    assert r.st['clock_set'] == 285 and r.st['phase'] == 'korrektur'   # 86 °C: auch −5 (vorher −20)
    r.go(35, temp=88)
    assert r.st['clock_set'] == 255                               # ab 88 °C Tabelle −30


def test_jump_from_below_79_to_85_counts_as_no_stay(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=75)
    clock = r.st['clock_set']
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 10


def test_new_measurement_after_the_correction_phase(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(11 * 60, temp=82)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 295                               # 11 min → −5
    r.go(70, temp=82)                                             # 1 min stabil: Rückkehr, Korrekturphase endet
    assert r.st['phase'] == 'normal'
    r.go(2 * 60, temp=83)
    r.go(5, temp=85)                                              # jetzt nur ~3 min in 79–84 °C → −10
    assert r.st['clock_set'] == 285



# ---------------------------------------------------------------- Startpause (Nutzer 25.10.)

def test_start_hold_after_restart_no_plus_for_3_min(miner):
    """25.10. live: nach dem Neustart meldete der Miner 26 °C, die Regelung setzte +40 MHz → 85 °C."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10                                            # Neustart
    start = r.now
    r.go(5, temp=26)
    ev = r.events('startpause')
    until = start + timedelta(minutes=3)
    assert len(ev) == 1 and ev[0]['trigger'] == ('Startpause: keine Takterhöhung bis %s (Neustart des Miners)'
                                                  % until.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    r.go(170, temp=26)
    assert r.st['clock_set'] == 300                               # 3 min keine Takterhöhung, auch bei 26 °C
    r.go(10, temp=26)
    assert r.st['clock_set'] == 340                               # danach wie immer (+40 unter 60 °C)


def test_start_hold_keeps_high_temperature_rules(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.go(30, temp=70)
    r.go(5, temp=85)
    assert r.st['clock_set'] == 290 and r.st['phase'] == 'korrektur'   # Absenkung ab 85 °C gilt weiter


def test_start_hold_after_over_hot(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.status = 'Over Hot'
    r.go(10, temp=82)
    miner.status = 'Work: In Work'
    r.go(5, temp=40)
    assert r.events('startpause') and r.events('start')[-1]['trigger'] == 'nach Over Hot'
    r.go(60, temp=40)
    assert r.st['clock_set'] == 300


def test_no_start_hold_when_base_level_is_chosen(miner):
    """Chips warm: Basisstufe gewählt, Automatik ein, nach Takt-Messung – keine Startpause."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['level_at'] = 'neu'
    r.go(60, temp=72)
    assert not r.events('startpause') and r.st['clock_set'] > 300
