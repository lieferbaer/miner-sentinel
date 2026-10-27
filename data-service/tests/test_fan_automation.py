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
        self.web_down = False                                    # nur die Einstellungsseite antwortet nicht
        self.never_read = False                                  # ... und wurde seit dem Start nie gelesen

    def read(self, ip, port, fresh=False):
        if self.offline:
            raise OSError('timed out')
        self.elapsed += 5
        conf = {'mode': 2, 'power_level': self.level}
        unknown = self.web_down and self.never_read
        return {'fields': {'TMax': str(self.tmax), 'MTmax': '%s 70 69' % self.tmax}, 'points': list(self.points),
                'mode_key': None if unknown else '2/L%d' % self.level, 'status': self.status, 'elapsed': self.elapsed,
                'conf': None if unknown else dict(conf, _stale=True) if self.web_down else conf,
                'conf_fresh': not self.web_down,
                'web_error': 'A15-Einstellungsseite lesen: timed out' if self.web_down else None,
                'power': 2000.0, 'ghsspd': 150000.0, 'dh': 1.2}

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


def row(state=None, level=5, enabled=True, cal=False, level_at=None, maintenance_at=None, temp_test_at=None,
        restart_req_at=None, target_low=79, target_high=84):
    return {'id': 1, 'device_id': 3, 'enabled': enabled, 'ac_dev_id': 'dev', 'ac_port': 1, 'level': level,
            'level_at': level_at, 'maintenance_at': maintenance_at, 'temp_test_at': temp_test_at, 'state': state or {},
            'restart_req_at': restart_req_at, 'target_low': target_low, 'target_high': target_high,
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


def started(miner, ac, level=5, tmax=82, learned=None, zb=None):
    """Automatik ein (Basisstufe `level`, Zielbereich `zb`, sonst 79–84 °C) and the start clock taken over."""
    r = Run(miner, ac, level)
    if zb:
        r.kw.update(target_low=zb[0], target_high=zb[1])
    if learned:
        # Lernwerte des eingestellten Bereichs (ohne gemerkten Bereich gehörten sie zum Startwert 82–85)
        r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': learned, 'level_seen': 'x',
                'zb': list(zb or (79, 84))}
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
    r = started(miner, ac, level=5, learned={'5': {'mhz': 390, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.level == 2 and miner.clock() == 390
    miner.clock_sets.clear()
    r.go(15, temp=88)                                             # 390 − 30 = 360 → mode 03 (Spielraum 20: bei 360 runter)
    assert miner.mode_sets[-1] == 3 and miner.level == 3
    assert miner.clock_sets[0] == pytest.approx(360, abs=1) and miner.clock() == 360
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


def test_return_at_once_below_the_target_range(miner):
    """Nutzer 27.10.: Rückkehrbereich = ganzer Zielbereich (hier 79–84 °C) – sofort erst unter 79 °C (vorher 80 °C)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(5, temp=85)
    r.go(5, temp=79)
    assert r.st['phase'] == 'korrektur'                           # 79 °C: im Rückkehrbereich, 1 min warten
    r.go(5, temp=78)
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

def test_mode_up_at_361_and_down_only_at_340(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert miner.level == 4
    r.go(40, temp=78)                                            # +5 → 363 → mode 03 zuerst
    assert miner.level == 3 and miner.clock() == 363
    st = r.st
    st['clock_set'] = 352
    assert fa.required_level(341, 3) == 3 and fa.required_level(340, 3) == 4     # Spielraum 20 MHz (Nutzer 26.10.)
    assert fa.required_level(361, 4) == 3 and fa.required_level(360, 4) == 4
    assert fa.required_level(360, 2) == 3 and fa.required_level(361, 2) == 2
    assert fa.required_level(400, 0) == 1 and fa.required_level(401, 0) == 0
    assert fa.table_level(360) == 4 and fa.table_level(361) == 3 and fa.table_level(440) == 0


def test_clock_capped_at_440(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=55, learned={'5': {'mhz': 430, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(40, temp=55)
    assert r.st['clock_set'] == 440 and miner.level == 0


# ---------------------------------------------------------------- Schritt 5: Ausnahme

def test_exception_lowers_fan_stepwise_after_3_minutes(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=82, learned={'5': {'mhz': 440, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(170, temp=60)                                           # warm gestartet (kein Heizmodus), dann kühl
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
    band = [c for c in r.j.cycles[-360:] if 79 <= (c['chip_temp'] or 0) <= 84]
    assert len(band) > 300                                       # letzte 30 min fast nur im Zielbereich (79–84 °C)
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
    r.go(185, temp=77)                                            # 75–78 °C: 3 min nach der letzten Erhöhung
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
    r.go(4 * 60, temp=82)                                         # 4 min in 79–84 °C (Grenze 5 min, Nutzer 26.10.)
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 10 and ac.level == 6
    takt = r.events('takt')[-1]
    assert takt['trigger'] == 'Chiptemperatur 85 °C nach 4 min in 79–84 °C: Takt −10 MHz'
    assert r.events('hohe_temperatur')[-1]['details']['aufenthalt_s'] == 240


def test_85_and_86_after_long_stay_minus_5_for_the_whole_correction_phase(miner):
    """Aufenthalt ab 5 min → −5; gilt für jede Absenkung bei 85/86 °C bis zum Ende der Korrekturphase, auch nach
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

def test_start_hold_after_restart_until_79(miner):
    """25.10. live: nach dem Neustart meldete der Miner 26 °C, die Regelung setzte +40 MHz → 85 °C; danach wurde er
    bei 253 MHz nur ~1 °C/min wärmer. Startpause bis 79 °C, höchstens 10 min (Nutzer 26.10.)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10                                            # Neustart
    start = r.now
    r.go(5, temp=26)
    ev = r.events('startpause')
    until = start + timedelta(minutes=10)
    assert len(ev) == 1 and ev[0]['trigger'] == ('Startpause: keine Takterhöhung bis 79 °C, spätestens %s'
                                                  % until.astimezone(fa.LOCAL_TZ).strftime('%H:%M'))
    assert ev[0]['details']['grund'] == 'Neustart des Miners'
    r.go(60, temp=26)
    r.go(6 * 60, temp=77)
    assert r.st['clock_set'] == 300                               # langsam warm: keine Takterhöhung
    r.go(5, temp=79)
    assert not r.st.get('start_hold')                             # 79 °C erreicht: Pause vorbei
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 79 °C erreicht (nach 7 min)'
    r.go(40, temp=78)
    assert r.st['clock_set'] == 305                               # danach wie immer


def test_start_hold_ends_after_10_min_at_the_latest(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.go(10 * 60 - 5, temp=70)
    assert r.st['clock_set'] == 300
    r.go(10, temp=70)
    assert r.st['clock_set'] == 305 and not r.st.get('start_hold')
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 10 min vorbei (Chiptemperatur 70 °C)'


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


def test_start_hold_also_when_another_start_reason_follows_in_the_same_round(miner):
    """Review 25.10.: Neustart erkannt und in derselben Runde Basisstufe gewählt (z. B. während das MinerGerät
    offline war) – die Chips sind trotzdem kalt: Startpause."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.kw['level_at'] = 'neu'
    r.go(5, temp=26)
    assert r.events('start')[-1]['trigger'].startswith('Basisstufe gewählt') and r.events('startpause')
    r.go(60, temp=26)
    assert r.st['clock_set'] == 300


# ---------------------------------------------------------------- Neustart sauber anzeigen (Nutzer 25.10.)

def test_restart_by_button_offline_is_expected(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()                    # Knopf „A15 neu starten“
    r.go(5, temp=82)
    assert r.st['restart_req']
    miner.offline = True
    r.go(30)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline')
    assert r.events('neustart_offline')[0]['trigger'] == 'A15 startet neu – nicht erreichbar (erwartet)'
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    assert r.events('neustart_online')[0]['trigger'] == 'A15 nach Neustart wieder erreichbar nach 30 s'
    assert r.events('start')[-1]['trigger'] == 'Neustart des Miners' and not r.st.get('restart_req')


def test_restart_another_way_relabels_offline_of_the_last_3_min(miner):
    """Neustart über die Einstellungsseite oder Strom: erst an der Laufzeit erkannt, dann werden „antwortet nicht/
    wieder“ der 3 min davor zu Neustart-Einträgen (nicht mehr rot)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline = False
    r.go(15, temp=82)
    assert r.events('miner_offline') and r.events('miner_online')
    miner.elapsed = 10
    r.go(5, temp=26)
    assert not r.events('miner_offline') and not r.events('miner_online')
    assert r.events('neustart_offline')[0]['ok'] is None
    assert r.events('neustart_online')[0]['trigger'] == 'A15 nach Neustart wieder erreichbar nach 30 s'
    miner.offline = True                                          # während der Startpause: Teil des Neustarts
    r.go(20)
    assert len(r.events('neustart_offline')) == 2 and not r.events('miner_offline')


def test_offline_without_restart_stays_an_error(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline = False
    r.go(4 * 60, temp=82)                                         # kein Neustart in den 3 min danach
    miner.elapsed = 10
    r.go(5, temp=26)
    assert r.events('miner_offline') and r.events('miner_offline')[0]['ok'] is False


def test_firmware_resend_waits_60_s_while_the_firmware_ramps(miner, monkeypatch):
    """26.10. live: nach dem Hochfahren setzte die Firmware 360 MHz und senkte dann schrittweise; die Regelung sandte
    in 10 s dreimal neu. Nach einem erneuten Senden frühestens nach 60 s wieder."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 253, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(70, temp=82)

    def ramping(ip, port, points):                                # Firmware stellt den Takt nur schrittweise ein
        miner.clock_sets.append(sum(points) / len(points))
        miner.points = [p + 60 for p in points]
        return 'STATUS=S,Msg=ASC 0 set OK'
    monkeypatch.setattr(calibration, 'set_frequency', ramping)
    miner.points = list(STOCK['2/L4'])                            # Firmware setzt den Standardtakt
    r.go(5, temp=82)
    assert len(r.events('firmware_takt')) == 1
    r.go(50, temp=82)
    assert len(r.events('firmware_takt')) == 1                    # Takt laut Miner noch hoch: aber erst nach 60 s
    r.go(15, temp=82)
    assert len(r.events('firmware_takt')) == 2


def test_restart_entries_carry_no_red_error_text(miner):
    """Review 26.10.: die Protokoll-Seite zeigt error immer rot – bei Neustart-Einträgen steht die Meldung in details."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    off = r.events('neustart_offline')[0]
    assert not off.get('error') and off['details']['meldung'] == 'timed out'
    miner.offline = True                                          # während der Startpause
    r.go(20)
    off = r.events('neustart_offline')[-1]
    assert not off.get('error') and off['details']['meldung'] == 'timed out'


def test_expected_offline_becomes_an_error_after_5_min(miner):
    """Review 26.10.: Knopf-Neustart, die A15 kommt nicht zurück – nach 5 min wie bisher „Miner antwortet nicht“."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()
    r.go(5, temp=82)
    miner.offline = True
    r.go(4 * 60)
    assert r.st['offline'] == 'neustart' and not r.events('miner_offline')
    r.go(60)
    assert r.st['offline'] is True and r.events('miner_offline')[0]['ok'] is False


def test_button_restart_does_not_relabel_entries_before_the_click(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True                                          # Zeitüberschreitung ohne Neustart
    r.go(10)
    miner.offline = False
    r.go(60, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()                    # dann der Knopf
    r.go(5, temp=82)
    miner.offline = True
    r.go(30)
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    assert len(r.events('miner_offline')) == 1 and r.events('miner_offline')[0]['ok'] is False
    assert len(r.events('neustart_offline')) == 1


def test_no_start_hold_after_takt_messung_with_mode_switch_restart(miner):
    """Review 26.10.: Moduswechsel der Takt-Messung setzen die Laufzeit zurück – danach keine Startpause (Chips warm)."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['cal'] = True
    r.go(10, temp=83)
    miner.elapsed = 10
    r.go(60, temp=83)
    r.kw['cal'] = False
    r.go(5, temp=75)
    assert r.events('start')[-1]['trigger'] == 'nach Takt-Messung' and not r.events('startpause')


def test_automatik_off_on_does_not_revive_an_old_button_restart(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    r.kw['restart_req_at'] = r.now.isoformat()
    r.go(5, temp=82)
    miner.elapsed = 10
    r.go(5, temp=26)
    assert not r.st.get('restart_req')
    r.kw['enabled'] = False
    r.go(10)
    r.kw['enabled'] = True
    r.go(10, temp=40)
    assert not r.st.get('restart_req')


def test_long_outage_pair_is_relabeled_together(miner):
    """Nutzer 26.10.: Strom 6 min aus – „antwortet nicht“ (älter als 3 min) und „wieder“ gemeinsam umbenennen."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.offline = True
    r.go(6 * 60)
    miner.offline, miner.elapsed = False, 10
    r.go(5, temp=26)
    assert not r.events('miner_offline') and not r.events('miner_online')
    assert r.events('neustart_offline')[0]['details']['meldung'] == 'timed out'
    assert r.events('neustart_online')[0]['trigger'] == 'A15 nach Neustart wieder erreichbar nach 360 s'


# ---------------------------------------------------------------- Einstellungsseite der A15 (Nutzer 26.10.)

def test_settings_page_down_regulation_goes_on(miner):
    """26.10. 04:31: 9,5 min antwortete nur die Einstellungsseite nicht – die Automatik war blind. Jetzt regelt sie
    weiter, kein „Miner antwortet nicht“; Eintrag erst nach 2 min, beim Zurückkommen die Dauer."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(60, temp=82)
    assert not r.events('einstellungsseite_aus') and not r.events('miner_offline')
    r.go(5, temp=85)
    assert r.st['clock_set'] == 290 and r.st['phase'] == 'korrektur'   # regelt weiter (Absenkung ab 85 °C)
    r.go(60, temp=82)
    ev = r.events('einstellungsseite_aus')
    assert len(ev) == 1 and ev[0]['ok'] is None and ev[0]['trigger'] == ('A15-Einstellungsseite antwortet nicht – '
                                                                       'Mining-Schnittstelle ok')
    r.go(60, temp=82)
    miner.web_down = False
    r.go(5, temp=82)
    assert r.events('einstellungsseite_an')[0]['trigger'] == 'A15-Einstellungsseite wieder erreichbar nach 3 min'
    assert not r.events('miner_offline')


def test_short_settings_page_drop_no_entry(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(100, temp=82)
    miner.web_down = False
    r.go(10, temp=82)
    assert not r.events('einstellungsseite_aus') and not r.events('einstellungsseite_an')


def test_mode_switch_waits_for_the_settings_page(miner):
    """Ein Moduswechsel braucht die Pool-Felder der Einstellungsseite: nie mit alten Daten, er wartet."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.web_down = True
    r.go(40, temp=78)                                             # +5 → 363 → mode 03 nötig
    assert miner.mode_sets == [] and len(r.events('modus_wartet')) == 1
    assert r.st['clock_set'] == 360 and not r.st['queue']         # Takt bis zum Standardtakt von mode 04
    r.go(30, temp=78)
    assert miner.mode_sets == [] and len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(185, temp=78)                                            # Seite wieder da: nächster Plus-Schritt (nach 3 min) mit Modus
    assert miner.mode_sets == [3] and miner.clock() == r.st['clock_set'] > 360 and r.st.get('mode_wait') is None


def test_waiting_mode_switch_does_not_block_further_reductions(miner):
    """Review 26.10.: wartete der Moduswechsel in der Befehlsfolge, kam ab 85 °C nach dem ersten Schritt keine weitere
    Absenkung mehr (busy). Jetzt wartet er außerhalb, die Absenkungen laufen weiter."""
    ac = FakeAC(level=5)
    miner.level, miner.points = 3, list(STOCK['2/L3'])
    r = started(miner, ac, level=5, learned={'5': {'mhz': 370, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(5, temp=88)                                              # 370 − 30 = 340 → mode 04 nötig
    r.go(5, temp=88)
    assert r.st['clock_set'] == 340 and miner.mode_sets == [] and r.events('modus_wartet')
    r.go(35, temp=88)
    assert r.st['clock_set'] == 310                               # weitere Absenkung trotz wartendem Moduswechsel
    r.go(40, temp=92)
    assert r.st['clock_set'] == 230


def test_second_outage_logs_mode_wait_again(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 358, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    miner.web_down = True
    r.go(40, temp=78)
    assert len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(60, temp=82)
    miner.web_down = True
    r.go(10, temp=82)
    r.go(40, temp=88)                                             # Absenkung über die Modusgrenze: wartet wieder
    r.go(40, temp=78)
    assert len(r.events('modus_wartet')) >= 1


def test_settings_never_read_keeps_the_last_known_mode(miner):
    """Review 26.10.: nach einem Data-Service-Neustart ohne gelesene Einstellungen kein rotes „antwortet nicht“ –
    die Automatik regelt mit dem zuletzt bekannten Modus weiter."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = miner.never_read = True
    r.go(5, temp=85)
    assert not r.events('miner_offline') and r.st['mode_key'] == '2/L4' and r.st['clock_set'] == 290


def test_waiting_mode_switch_caps_the_clock_at_the_current_mode(miner):
    """Review 26.10.: wartete der Moduswechsel, stieg der gesetzte Takt bis 440 MHz, obwohl der Miner beim Standardtakt
    von mode 04 blieb (Ausnahme, falscher Lernwert, am Ende ein Sprung). Jetzt höchstens der Standardtakt des Modus."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 350, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.web_down = True
    r.go(6 * 60, temp=65)
    assert r.st['clock_set'] == 360 and miner.mode_sets == [] and not r.events('ausnahme')
    assert len(r.events('modus_wartet')) == 1
    miner.web_down = False
    r.go(10, temp=65)
    assert r.st['clock_set'] <= 380                               # danach wieder in Schritten, kein Sprung auf 440


# ---------------------------------------------------------------- 3 min zwischen Takterhöhungen bei 75–78 °C (Nutzer 26.10.)

def test_plus_steps_at_75_to_78_wait_3_min(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=76, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(35, temp=76)
    assert r.st['clock_set'] == 255
    r.go(170, temp=76)
    assert r.st['clock_set'] == 255                               # 3 min nach der letzten Erhöhung noch nicht vorbei
    r.go(15, temp=76)
    assert r.st['clock_set'] == 260


def test_wait_counts_from_any_increase_but_not_below_75(miner):
    """Meine Lesart (freigegeben): die 3 min zählen ab der letzten Takterhöhung, egal bei welcher Temperatur."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=65, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(35, temp=65)
    assert r.st['clock_set'] == 270                               # 60–69 °C: +20
    r.go(40, temp=72)
    assert r.st['clock_set'] == 275                               # unter 75 °C: nach dem 30-s-Fenster wie bisher
    r.go(40, temp=76)
    assert r.st['clock_set'] == 275                               # 75–78 °C: erst 3 min nach dem +5
    r.go(150, temp=76)
    assert r.st['clock_set'] == 280


def test_dwell_limit_is_5_min(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(60, temp=75)
    clock = r.st['clock_set']
    r.go(5 * 60, temp=82)                                         # 5 min in 79–84 °C
    r.go(5, temp=85)
    assert r.st['clock_set'] == clock - 5


def test_plus_wait_counts_from_the_takeover_and_only_real_increases(miner):
    """Review 26.10.: die 3 min zählen ab der Übernahme (nicht ab der Entscheidung); ein auf den gleichen Takt gekappter
    Schritt (Moduswechsel wartet) ist keine Takterhöhung."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=76, learned={'5': {'mhz': 360, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(10, temp=82)
    miner.web_down = True
    r.go(35, temp=76)                                             # 360 → 365 bräuchte mode 03: gekappt auf 360
    assert r.st['clock_set'] == 360 and not r.st.get('last_plus_at')
    miner.web_down = False
    r.go(45, temp=76)                                             # Seite wieder da: der Plus-Schritt kommt gleich
    assert miner.mode_sets == [3] and r.st['clock_set'] == 365 and r.st.get('last_plus_at')


# ---------------------------------------------------------------- Heizmodus beim Start (Nutzer 26.10.)

def test_heat_mode_after_a_cold_start_fan_1_until_79(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    assert ac.level == 3
    miner.elapsed = 10                                            # Neustart: Chips kalt
    r.go(10, temp=26)
    assert ac.level == 1 and r.st['heat']
    assert r.events('heizmodus')[-1]['trigger'] == 'Heizmodus: Lüfter Stufe 1 bis 79 °C'
    r.go(120, temp=78)
    assert ac.level == 1
    r.go(15, temp=79)
    assert ac.level == 3 and not r.st['heat']
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: 79 °C erreicht, Lüfter Stufe 3'


def test_no_heat_mode_when_already_warm(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.kw['level_at'] = 'neu'                                      # Basisstufe gewählt bei 82 °C
    r.go(15, temp=82)
    assert ac.level == 3 and not r.st['heat'] and not r.events('heizmodus')


def test_heat_mode_ends_when_a_temperature_test_begins(miner):
    """Review 26.10.: der Temperatur-Test stellt den Lüfter auf die Basisstufe – damit endet der Heizmodus."""
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2, tmax=40, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert r.st['heat'] and ac.level == 1
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(10, temp=60)
    assert r.st['test'] and not r.st['heat'] and ac.level == 2
    ev = r.events('heizmodus_ende')[-1]
    assert ev['trigger'] == 'Heizmodus beendet: Temperatur-Test, Lüfter Stufe 2' and ev['fan_old'] == 1


def test_no_heat_mode_for_a_start_while_a_temperature_test_runs(miner):
    """Review 26.10.: Neustart und Test-Beginn in derselben Runde – der Start schaltet den Heizmodus nicht wieder ein."""
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed = 10
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(5, temp=30)
    assert r.st['test'] and not r.st['heat'] and ac.level == 3
    r.go(300, temp=69)
    assert r.st['test'] and not r.st['heat'] and ac.level == 3


def test_heat_mode_ends_with_a_start_during_takt_messung(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=40, learned={'5': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert r.st['heat'] and ac.level == 1
    r.kw['cal'] = True
    r.go(10, temp=60)
    miner.elapsed = 10                                            # Moduswechsel der Messung: Laufzeit zurückgesetzt
    r.go(10, temp=60)
    assert not r.st['heat'] and ac.level == 5
    r.go(30, temp=76)
    assert len(r.events('heizmodus_ende')) == 1


def _heating(miner, ac):
    r = started(miner, ac, level=3, tmax=40, learned={'3': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    assert r.st['heat'] and ac.level == 1
    return r


def test_heat_mode_ends_at_once_when_automation_is_turned_off(miner):
    """Nutzer 26.10.: ohne Regelung kein Lüfter auf Stufe 1 – Automatik aus, Miner antwortet nicht, Over Hot."""
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    ac.fail = True
    r.go(5, temp=60, enabled=False)
    assert not r.st['heat'] and ac.level == 1 and r.st.get('heat_fan_due')    # Befehl fehlgeschlagen: wiederholen
    ac.fail = False
    r.go(60, temp=60)
    assert ac.level == 3 and not r.st.get('heat_fan_due') and r.st['phase'] == 'aus'
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: Automatik aus, Lüfter Stufe 3'
    assert r.events('luefter')[-1]['takeover_state'] == 'nicht gemessen' and not r.st['fan_pending']
    writes = list(ac.writes)
    r.go(60, temp=60)
    assert ac.writes == writes                                    # danach kein Lüfterbefehl mehr bei Automatik aus


def test_heat_mode_ends_at_once_when_the_miner_does_not_answer(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    miner.offline = True
    r.go(5, temp=60)
    assert not r.st['heat'] and ac.level == 3
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: Miner antwortet nicht, Lüfter Stufe 3'
    assert r.j.cycles[-1]['decision'] == 'Miner antwortet nicht'


def test_heat_mode_stays_while_the_a15_restarts_by_button(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    r.kw['restart_req_at'] = r.now.isoformat()
    miner.offline = True
    r.go(30, temp=60)
    assert r.st['offline'] == 'neustart' and r.st['heat'] and ac.level == 1


def test_heat_mode_ends_at_once_on_over_hot(miner):
    ac = FakeAC(level=3)
    r = _heating(miner, ac)
    miner.status = 'Over Hot'
    r.go(5, temp=60)
    assert not r.st['heat'] and ac.level == 3
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: Over Hot, Lüfter Stufe 3'


def test_heat_mode_also_when_the_temperature_is_unknown_at_the_start(miner):
    ac = FakeAC(level=4)
    r = started(miner, ac, level=4, learned={'4': {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}})
    r.go(30, temp=82)
    miner.elapsed, miner.status = 10, 'Over Hot'                 # Over Hot: keine gültige Temperatur
    r.go(10, temp=82)
    miner.status = 'Work: In Work'
    r.go(5, temp=40)
    assert r.st['heat'] and ac.level == 1
    r.go(30, temp=80)
    assert not r.st['heat'] and ac.level == 4


# ---------------------------------------------------------------- Zielbereich wählbar (Nutzer 27.10.)

LW = {'mhz': 300, 'temp_avg': 82, 'at': 'x', 'count': 1}


def test_high_table_follows_the_upper_edge_capped_at_96():
    assert fa.high_rule(84, 84) is None and fa.high_rule(85, 84) == (85, 1, None)       # 79–84 °C wie bisher
    assert fa.high_rule(88, 84) == (88, 2, 30) and fa.high_rule(94, 84) == (94, 2, 100)
    assert fa.high_rule(85, 85) is None and fa.high_rule(86, 85) == (86, 1, None)
    assert fa.high_rule(87, 85) == (87, 1, None) and fa.high_rule(89, 85) == (89, 2, 30)
    assert fa.high_rule(91, 85) == (91, 2, 50) and fa.high_rule(93, 85) == (93, 2, 80)
    assert fa.high_rule(95, 85) == (95, 2, 100)
    assert fa.high_rule(92, 92) is None and fa.high_rule(93, 92) == (93, 1, None)       # 89–92 °C: Deckel 96 °C
    assert fa.high_rule(95, 92) == (94, 1, None) and fa.high_rule(96, 92) == (96, 2, 100)
    assert fa.high_rule(95, 89) == (95, 2, 50) and fa.high_rule(96, 89) == (96, 2, 100)
    assert fa.high_rule(78, 77) == (78, 1, None) and fa.high_rule(81, 77) == (81, 2, 30)


def test_plus_size_follows_the_lower_edge():
    assert [fa.plus_size(t, 82) for t in (81, 73, 72, 63, 62)] == [5, 5, 20, 20, 40]
    assert [fa.plus_size(t, 76) for t in (75, 67, 66, 57, 56)] == [5, 5, 20, 20, 40]


def test_range_82_85_no_clock_change_inside_learning_until_85(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=(82, 85))
    sets = list(miner.clock_sets)
    for temp in (82, 85, 83, 85, 82):
        r.go(60, temp=temp)
    assert miner.clock_sets == sets and r.st['phase'] == 'normal' and ac.level == 5
    assert r.st['learn_since'] and r.events('lernen_start')[0]['trigger'] == 'Chiptemperatur 82 °C in 82–85 °C'
    r.go(5, temp=86)                                              # ab U+1: hohe Chiptemperatur, Lernen bricht ab
    assert r.st['phase'] == 'korrektur' and ac.level == 6 and r.st['clock_set'] == 295 and not r.st['learn_since']
    assert r.events('takt')[-1]['trigger'] == 'Chiptemperatur 86 °C nach 5 min in 82–85 °C: Takt −5 MHz'


def test_range_82_85_plus_steps(miner):
    """+5 ab L−9 (73 °C), +20 ab L−19 (63 °C), darunter +40."""
    for temp, step in ((82, 0), (81, 5), (73, 5), (72, 20), (63, 20), (62, 40)):
        miner.clock_sets.clear()
        ac = FakeAC(level=5)
        r = started(miner, ac, level=5, tmax=temp, learned={'5': {'mhz': 200, 'temp_avg': 82, 'at': 'x', 'count': 1}},
                    zb=(82, 85))
        r.go(40, temp=temp)
        assert miner.clock() == 200 + step, temp


def test_range_82_85_wait_3_min_from_78(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=78, learned={'5': {'mhz': 250, 'temp_avg': 82, 'at': 'x', 'count': 1}},
                zb=(82, 85))
    r.go(35, temp=78)
    assert r.st['clock_set'] == 255
    r.go(170, temp=78)
    assert r.st['clock_set'] == 255                               # ab L−4 = 78 °C: 3 min nach der letzten Erhöhung
    r.go(15, temp=78)
    assert r.st['clock_set'] == 260
    r.go(40, temp=77)
    assert r.st['clock_set'] == 265                               # unter 78 °C: nach dem 30-s-Fenster


def test_range_82_85_return_band(miner):
    """Rückkehr nach 1 min stabil in 82–85 °C, sofort unter 82 °C."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW}, zb=(82, 85))
    r.go(5, temp=86)
    assert ac.level == 6
    r.go(55, temp=85)
    assert ac.level == 6 and r.st['phase'] == 'korrektur'
    assert r.events('stabilitaet_start')[-1]['trigger'] == 'Rückkehr: Chiptemperatur 85 °C in 82–85 °C'
    r.go(15, temp=85)
    assert ac.level == 5 and r.st['phase'] == 'normal'
    assert r.events('rueckkehr')[-1]['trigger'] == '1 min stabil in 82–85 °C'
    r.go(5, temp=86)
    r.go(5, temp=81)
    assert r.st['phase'] == 'normal' and r.events('rueckkehr')[-1]['trigger'] == 'Chiptemperatur 81 °C unter 82 °C'


def test_range_89_92_high_from_93_and_96_cap(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=90, learned={'5': LW}, zb=(89, 92))
    r.go(60, temp=92)
    assert r.st['clock_set'] == 300 and r.st['phase'] == 'normal'
    r.go(5, temp=93)
    assert ac.level == 6 and r.st['clock_set'] == 290             # U+1: Lüfter +1, kurzer Aufenthalt −10
    r.go(40, temp=96)
    assert ac.level == 7 and r.st['clock_set'] == 190             # ab 96 °C: Lüfter +2, Takt −100
    assert r.events('takt')[-1]['trigger'].startswith('Chiptemperatur 96 °C (ab 96 °C): Takt −100 MHz')


def test_narrow_range_76_77(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=77, learned={'5': LW}, zb=(76, 77))
    sets = list(miner.clock_sets)
    r.go(60, temp=76)
    r.go(60, temp=77)
    assert miner.clock_sets == sets and r.st['phase'] == 'normal'
    r.go(5, temp=78)                                              # ab 78 °C hoch
    assert ac.level == 6 and r.st['clock_set'] == 290
    r.go(5, temp=77)
    assert r.st['phase'] == 'korrektur'                           # in 76–77 °C: 1 min warten
    r.go(5, temp=75)
    assert r.st['phase'] == 'normal' and ac.level == 5            # unter 76 °C sofort zurück


def test_narrow_range_76_77_plus_steps_and_wait_from_72(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=75, learned={'5': LW}, zb=(76, 77))
    r.go(40, temp=75)
    assert r.st['clock_set'] == 305
    r.go(60, temp=72)
    assert r.st['clock_set'] == 305                               # ab L−4 = 72 °C: 3 min warten
    r.go(5, temp=71)
    assert r.st['clock_set'] == 310


def test_heat_mode_and_start_hold_end_at_the_lower_edge(miner):
    ac = FakeAC(level=3)
    r = started(miner, ac, level=3, learned={'3': LW}, zb=(82, 85))
    r.go(30, temp=84)
    miner.elapsed = 10                                            # Neustart: Chips kalt
    r.go(10, temp=26)
    assert ac.level == 1 and r.st['heat'] and r.st.get('start_hold')
    assert r.events('heizmodus')[-1]['trigger'] == 'Heizmodus: Lüfter Stufe 1 bis 82 °C'
    assert r.events('startpause')[-1]['trigger'].startswith('Startpause: keine Takterhöhung bis 82 °C, spätestens ')
    r.go(120, temp=81)
    assert ac.level == 1 and r.st['heat'] and r.st.get('start_hold') and r.st['clock_set'] == 300
    r.go(15, temp=82)
    assert ac.level == 3 and not r.st['heat'] and not r.st.get('start_hold')
    assert r.events('heizmodus_ende')[-1]['trigger'] == 'Heizmodus beendet: 82 °C erreicht, Lüfter Stufe 3'
    assert r.events('startpause_ende')[-1]['trigger'] == 'Startpause beendet: 82 °C erreicht (nach 2 min)'


def test_exception_below_l_minus_9(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': {'mhz': 440, 'temp_avg': 82, 'at': 'x', 'count': 1}}, zb=(82, 85))
    r.go(185, temp=73)
    assert ac.level == 5 and not r.events('ausnahme')             # 73 °C ist nicht unter L−9
    r.go(185, temp=72)
    assert ac.level == 4 and r.st['phase'] == 'ausnahme'
    assert r.events('ausnahme')[-1]['trigger'] == 'Takt 440 MHz, 3 min stabil unter 73 °C'
    r.go(10, temp=74)
    assert r.st['phase'] == 'normal' and r.events('ausnahme_ende')[-1]['trigger'] == 'Chiptemperatur 74 °C über 73 °C'


def test_temp_test_begins_at_l_plus_1(miner):
    ac = FakeAC(level=2)
    r = started(miner, ac, level=2, learned={'2': {'mhz': 319.2, 'temp_avg': 82, 'at': 'x', 'count': 1}}, zb=(82, 85))
    r.kw['temp_test_at'] = r.now.isoformat()
    r.go(70, temp=84)
    assert r.st['test']['step'] == 0 and r.st['test']['phase'] == 'beginn'
    assert any(n[1].endswith('dann warten bis ≤ 83 °C.') for n in r.st['log'])
    r.go(5, temp=83)
    assert r.st['test']['step'] == 1 and r.st['clock_set'] == 349


def test_takt_messung_keeps_the_fixed_protection_with_a_high_range(miner):
    """Takt-Messung: Lüfterschutz wie bisher ab 85 °C, Rückkehr nach 1 min in 80–84 °C – unabhängig vom Zielbereich."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=90, learned={'5': LW}, zb=(89, 92))
    r.go(10, temp=84, cal=True)
    sends = len(miner.clock_sets)
    assert not r.st.get('corr')
    r.go(5, temp=86)                                              # unter dem Zielbereich, in der Messung aber hoch
    assert r.st['corr'] and ac.level == 6 and len(miner.clock_sets) == sends
    r.go(5, temp=88)
    assert ac.level == 7
    r.go(30, temp=83)
    assert r.st['corr']                                           # 83 °C: im Rückkehrbereich 80–84 °C, nicht sofort
    r.go(35, temp=83)
    assert not r.st['corr'] and ac.level == 5


def test_takt_messung_keeps_the_fixed_protection_with_a_low_range(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, tmax=77, learned={'5': LW}, zb=(76, 77))
    r.go(10, temp=77, cal=True)
    r.go(30, temp=84)
    assert not r.st.get('corr') and ac.level == 5                 # ohne Takt-Messung wäre 84 °C hoch
    r.go(5, temp=85)
    assert r.st['corr'] and ac.level == 6
    r.go(5, temp=79)
    assert not r.st['corr']                                       # unter 80 °C sofort zurück


def _without_range(miner, ac, lo, hi, tmax):
    """Zustand vor dem 27.10.: Lernwerte, aber kein gemerkter Bereich."""
    r = Run(miner, ac, 5)
    r.st = {'v': fa.STATE_VERSION, 'seen_at': T0.isoformat(), 'learned': {'5': LW}, 'level_seen': 'x'}
    r.kw.update(level_at='klick', target_low=lo, target_high=hi)
    miner.tmax = tmax
    return r.go(10)


def test_first_round_without_a_range_keeps_the_learned_values(miner):
    """Stand vor dem 27.10. (kein gemerkter Bereich): die Lernwerte gehören zum Startwert 82–85, kein Eintrag."""
    ac = FakeAC(level=5)
    r = _without_range(miner, ac, 82, 85, 83)
    assert r.st['zb'] == [82, 85] and r.st['learned'] == {'5': LW} and 'learned_zb' not in r.st
    assert not [n for n in r.st['log'] if 'Zielbereich' in n[1]]
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert' and r.st['clock_set'] == 300


def test_first_round_without_a_range_but_another_range_chosen(miner):
    """Review 27.10.: vor der ersten Runde schon ein anderer Bereich gewählt (z. B. bei Automatik aus) – die Lernwerte
    bleiben beim Startwert 82–85, der neue Bereich beginnt mit dem Initialwert."""
    ac = FakeAC(level=5)
    r = _without_range(miner, ac, 76, 77, 70)
    assert r.st['zb'] == [76, 77] and r.st['learned_zb'] == {'82-85': {'5': LW}}
    assert r.events('start')[-1]['details']['quelle'] == 'Initialwert'


def test_range_change_leaves_the_saved_state_untouched(miner):
    """Review 27.10.: scheitert die Runde nach dem Wechsel, speichert run_fan_automation den alten Zustand – dessen
    Archiv bleibt vollständig."""
    other = {'mhz': 340, 'temp_avg': 84, 'at': 'x', 'count': 1}
    state = {'v': fa.STATE_VERSION, 'zb': [82, 85], 'learned': {'5': other}, 'learned_zb': {'79-84': {'5': LW}}}
    rnd = fa.Round(row(state, level=5, target_low=79, target_high=84), T0, FakeAC(level=5),
                   automation_journal.MemoryJournal())
    rnd.target_range()
    assert rnd.st['learned'] == {'5': LW} and rnd.st['learned_zb'] == {'82-85': {'5': other}}
    assert state['learned_zb'] == {'79-84': {'5': LW}}


def test_range_change_takes_the_new_limits_at_once_without_a_start(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(60, temp=83)
    assert r.st['zb'] == [79, 84] and r.st['learn_since']
    sets, starts = list(miner.clock_sets), len(r.events('start'))
    r.kw.update(target_low=82, target_high=85)
    r.go(5, temp=83)
    assert r.st['zb'] == [82, 85] and r.st['learned'] == {} and r.st['learned_zb'] == {'79-84': {'5': LW}}
    assert r.events('lernen_ende')[-1]['details']['grund'] == 'Zielbereich geändert'
    assert r.st['band_in'] == (r.now - timedelta(seconds=5)).isoformat()    # Aufenthalt neu gemessen
    assert any(n[1] == 'Zielbereich 82–85 °C übernommen.' for n in r.st['log'])
    assert len(r.events('start')) == starts and miner.clock_sets == sets and not r.st.get('heat') and ac.level == 5
    r.go(40, temp=81)                                             # 81 °C liegt jetzt unter dem Zielbereich: +5
    assert r.st['clock_set'] == 305


def test_learned_values_are_kept_per_range(miner):
    """Nutzer 27.10.: zurück im alten Bereich gelten wieder seine Lernwerte; ein neuer Bereich beginnt mit dem
    Initialwert."""
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=83)
    r.kw.update(target_low=82, target_high=85, level_at='neu')
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['quelle'] == 'Initialwert' and r.st['clock_set'] == 140
    r.go(15 * 60 + 30, temp=83)
    assert r.st['learned']['5']['mhz'] == 140
    r.kw.update(target_low=79, target_high=84)
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': LW} and r.st['learned_zb']['82-85']['5']['mhz'] == 140
    r.kw['level_at'] = 'neu2'
    r.go(10, temp=83)
    assert r.events('start')[-1]['details']['quelle'] == 'Lernwert' and r.st['clock_set'] == 300


def test_automatik_off_on_keeps_the_range_and_the_learned_values_per_range(miner):
    ac = FakeAC(level=5)
    r = started(miner, ac, level=5, learned={'5': LW})
    r.go(30, temp=83)
    r.kw.update(target_low=82, target_high=85)
    r.go(5, temp=83)
    r.kw['enabled'] = False
    r.go(10)
    r.kw['enabled'] = True
    r.go(10, temp=83)
    assert r.st['zb'] == [82, 85] and r.st['learned_zb'] == {'79-84': {'5': LW}}
    assert len([n for n in r.st['log'] if 'Zielbereich' in n[1]]) == 1
    r.kw.update(target_low=79, target_high=84)
    r.go(5, temp=83)
    assert r.st['learned'] == {'5': LW}


def test_select_reads_the_target_range(monkeypatch):
    seen = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args=None):
            seen.append(sql)

        def fetchall(self):
            return []

    class Conn:
        def cursor(self, cursor_factory=None):
            return Cur()

        def close(self):
            pass

    monkeypatch.setenv('MINERSENTINEL_ENV', 'Live')
    monkeypatch.setattr(fa.psycopg2, 'connect', lambda url: Conn())
    fa.run_fan_automation('postgresql://x', ac=FakeAC())
    assert 'f.target_low' in seen[0] and 'f.target_high' in seen[0]
